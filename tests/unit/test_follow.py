"""
The follow engine -- SPEC 7, FR-21 to FR-26, NFR-8, NFR-20, risk R5.

**Not a single test here sleeps.** Every one steps the poller by hand with
`poll_once` and then calls `apply`, so the assertions are about what the engine
did rather than about whether an interval had elapsed. A follow suite built on
`time.sleep` is slow when it passes and intermittent when it fails, which is
the worst pair of properties available for the one feature whose job is to
happen by itself.

The standard of correctness throughout is **a cold session over the same
bytes**. A file that was tailed and a file that was opened afterwards must give
identical rows, identical line numbers and identical histograms; anything less
means the log you read depends on when you opened it.

Risk R5 is rotation under load, and the matrix at the bottom is what retires
it: every row of SPEC 7.2's table, driven by a harness that does to the files
what a rotating writer does.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from logweave.index.appending import append_region, safe_end, trim_to_line
from logweave.index.indexer import build_index
from logweave.session.follow import FollowEngine
from logweave.session.session import Session
from logweave.source.file import FileSource
from logweave.source.poller import ChangeKind, Poller, Probe, classify, probe_path


def line(src: str, second: int, i: int) -> str:
    """
    One log line, `second` seconds past midnight.

    Carried out to hours rather than minutes because a test that writes four
    thousand lines runs off the end of an hour, and `12:66:39` is not a
    timestamp -- those lines would simply fail to parse, and the test would be
    asserting on a half-empty index while looking like it was asserting on a
    full one.
    """
    h, rest = divmod(int(second), 3600)
    m, sec = divmod(rest, 60)
    return (f"2026-11-08 {h:02d}:{m:02d}:{sec:02d}.000 "
            f"INFO  {src} line {i}\n")


def write(path, src, seconds) -> None:
    path.write_text("".join(line(src, s, s) for s in seconds), encoding="utf-8")


def add(path, src, seconds) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for s in seconds:
            handle.write(line(src, s, s))


@pytest.fixture
def poller():
    p = Poller(interval=3600.0)          # never fires on its own
    yield p
    p.stop()


@pytest.fixture
def followed(tmp_path, poller):
    """A two-source session with the engine running, driven by hand."""
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    write(a, "a", range(0, 20, 2))
    write(b, "b", range(1, 20, 2))
    session = Session(use_cache=False)
    session.add_files([a, b])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    yield session, engine, a, b
    engine.stop()
    session.close()


def step(poller, engine):
    """One poll pass and one apply -- what a tick is, with the timing removed."""
    poller.poll_once()
    return engine.apply()


def cold_rows(paths):
    session = Session(use_cache=False)
    try:
        session.add_files(list(paths))
        return [r.text for r in session.rows(0, session.total_rows)]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# the probe (SPEC 7.4)
# ---------------------------------------------------------------------------

def test_a_missing_path_probes_as_nothing_and_stays_equal_to_itself(tmp_path):
    """
    A vanished file must not re-fire every tick for as long as it is away.

    `Probe()` carries no size or identity precisely so that two probes of a
    missing path compare equal -- otherwise a deleted log would report a change
    several times a second until someone closed it.
    """
    gone = probe_path(tmp_path / "nope.log")
    assert not gone.exists
    assert gone == probe_path(tmp_path / "nope.log")
    assert classify(gone, gone) is ChangeKind.UNCHANGED


def test_the_probe_does_not_open_the_file(tmp_path):
    """
    NFR-20 and SPEC 7.1.1. `os.stat` reads directory metadata; `path_identity`
    opens a handle, which a log held exclusively by its writer would refuse --
    and polling it that way would mark a live source gone every tick.
    """
    path = tmp_path / "x.log"
    write(path, "x", [1])
    before = probe_path(path)
    assert before.exists and before.size > 0
    # The same stat on a source we hold mapped and open is still just a stat.
    with FileSource(path) as src:
        src.read_range(0, 10)
        assert probe_path(path) == before


@pytest.mark.parametrize(("before", "after", "want"), [
    (Probe(True, 1, 2, 100, 5), Probe(True, 1, 2, 200, 6), ChangeKind.APPENDED),
    (Probe(True, 1, 2, 200, 5), Probe(True, 1, 2, 100, 6), ChangeKind.TRUNCATED),
    (Probe(True, 1, 2, 100, 5), Probe(True, 1, 9, 100, 5), ChangeKind.ROTATED),
    (Probe(True, 1, 2, 100, 5), Probe(), ChangeKind.VANISHED),
    (Probe(), Probe(True, 1, 2, 100, 5), ChangeKind.ROTATED),
    (Probe(True, 1, 2, 100, 5), Probe(True, 1, 2, 100, 7), ChangeKind.APPENDED),
    (Probe(True, 1, 2, 100, 5), Probe(True, 1, 2, 100, 5), ChangeKind.UNCHANGED),
])
def test_the_probe_names_the_change(before, after, want):
    """
    Identity before size before mtime -- the order of certainty.

    A new file at the same path is a rotation whatever its size did, so testing
    size first would report a rotation into a bigger log as an ordinary append
    and resume indexing the new file at the old file's offset.
    """
    assert classify(before, after) is want


def test_a_callback_that_raises_does_not_stop_the_other_sources(tmp_path, poller):
    """NFR-21. A daemon thread has nowhere to report to, so it records and goes on."""
    from logweave.source.base import SourceId

    first, second = tmp_path / "1.log", tmp_path / "2.log"
    write(first, "1", [1])
    write(second, "2", [1])
    seen: list[str] = []
    poller.add(SourceId("boom"), first, lambda _e: (_ for _ in ()).throw(RuntimeError("x")))
    poller.add(SourceId("fine"), second, lambda e: seen.append(str(e.source_id)))

    add(first, "1", [2])
    add(second, "2", [2])
    assert poller.poll_once() == 2
    assert seen == ["fine"]
    assert len(poller.failures) == 1


def test_stopping_a_watcher_takes_it_out_of_the_pass(tmp_path, poller):
    from logweave.source.base import SourceId

    path = tmp_path / "w.log"
    write(path, "w", [1])
    got: list[object] = []
    watcher = poller.add(SourceId("w"), path, got.append)
    assert poller.watching == 1
    watcher.stop()
    assert poller.watching == 0
    add(path, "w", [2])
    assert poller.poll_once() == 0
    assert got == []


# ---------------------------------------------------------------------------
# SPEC 7.3: the partial-line rule
# ---------------------------------------------------------------------------

def test_an_unfinished_line_is_not_indexed_until_it_is_finished(tmp_path):
    """
    The failure this prevents is not a crash. `build_index` records its extent
    as the size it was given, so resuming there would index the *remainder* of a
    half-written line as an entry of its own -- the row would appear twice, once
    glued to its predecessor and once as its own tail.
    """
    path = tmp_path / "p.log"
    # Long enough that detection settles on a fixed layout rather than the
    # scalar fallback, which is the path a tail index can resume from.
    write(path, "p", range(40))
    with FileSource(path) as src:
        index = build_index(src, mtime_wall_us=0)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line("p", 41, 41))
            handle.write("2026-11-08 00:00:42.000 INFO  p par")

        result = append_region(src, index, mtime_wall_us=0)
        assert result.added == 1
        assert not result.needs_rebuild
        assert result.pending_bytes == 35
        assert result.index.n == 41

        with open(path, "a", encoding="utf-8") as handle:
            handle.write("tial\n")
        finished = append_region(src, result.index, mtime_wall_us=0)
        assert finished.added == 1
        assert finished.pending_bytes == 0
        lo, hi = finished.index.entry_range(finished.index.n - 1)
        # Both halves, as one entry: the line split across two appends must not
        # come back as two rows, and must not come back short.
        assert src.read_range(lo, hi).strip().endswith(b"p partial")


def test_an_append_that_is_all_unfinished_line_waits(tmp_path):
    path = tmp_path / "q.log"
    write(path, "q", range(20))
    with FileSource(path) as src:
        index = build_index(src, mtime_wall_us=0)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("2026-11-08 00:00:21.000 INFO  no terminator yet")
        result = append_region(src, index, mtime_wall_us=0)
        assert result.added == 0
        assert result.pending_bytes > 0
        assert "unfinished" in result.note


def test_safe_end_finds_no_cut_in_a_region_without_a_terminator(tmp_path):
    path = tmp_path / "r.log"
    path.write_bytes(b"no terminator at all")
    with FileSource(path) as src:
        assert safe_end(src, 0, src.size()) == 0


def test_following_gives_back_the_half_line_the_first_index_swallowed(tmp_path, poller):
    """
    The index a session starts with was built against a live file, so its last
    line is usually half written. `trim_to_line` hands those bytes back when
    following starts -- otherwise the first append splits that line in two.
    """
    path = tmp_path / "s.log"
    write(path, "s", range(30))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("2026-11-08 00:00:31.000 INFO  s hal")

    session = Session(use_cache=False)
    session.add_files([path])
    extent_before = session.sources[0].index.indexed_to
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        assert session.sources[0].index.indexed_to < extent_before
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("f\n")
        tick = step(poller, engine)
        assert tick.added_rows == 1
        rows = [r.text for r in session.rows(0, session.total_rows)]
        assert rows == cold_rows([path])
    finally:
        engine.stop()
        session.close()


def test_trimming_an_index_with_no_entries_does_nothing(tmp_path):
    path = tmp_path / "t.log"
    path.write_bytes(b"nothing parseable here")
    with FileSource(path) as src:
        index = build_index(src, mtime_wall_us=0)
        assert index.n == 0
        assert trim_to_line(src, index) == 0


# ---------------------------------------------------------------------------
# FR-21: appended lines appear, in the right place
# ---------------------------------------------------------------------------

def test_an_append_to_each_source_interleaves_correctly(followed, poller):
    session, engine, a, b = followed
    before = session.total_rows
    add(a, "a", [20, 22, 24])
    add(b, "b", [21, 23])
    tick = step(poller, engine)

    assert tick.added_rows == 5
    assert session.total_rows == before + 5
    assert sorted(tick.changed) == [0, 1]
    tail = [r.text for r in session.rows(before, 5)]
    assert [t.split("INFO  ")[1].strip() for t in tail] == [
        "a line 20", "b line 21", "a line 22", "b line 23", "a line 24"]


def test_a_followed_session_matches_one_opened_cold(followed, poller):
    """The headline invariant. Eleven uneven ticks, then compare every row."""
    session, engine, a, b = followed
    at = 20
    for tick in range(11):
        if tick % 3:
            add(a, "a", range(at, at + 4, 2))
        if tick % 2:
            add(b, "b", range(at + 1, at + 4, 2))
        at += 4
        step(poller, engine)

    assert [r.text for r in session.rows(0, session.total_rows)] == cold_rows([a, b])


def test_an_append_leaves_the_histogram_the_strips_read_correct(followed, poller):
    """
    Both overview strips are drawn from `FileIndex.hist` (SPEC 8.1), so an
    append that does not reach it shows as a timeline that stops growing while
    rows keep arriving.
    """
    session, engine, a, b = followed
    add(a, "a", range(20, 60, 2))
    step(poller, engine)

    for source in session.sources:
        assert source.index.hist is not None
        assert source.index.hist.total == source.index.n
    strip = session.overview.timeline(32)
    assert int(strip.counts.sum()) == session.total_rows


def test_a_quiet_tick_costs_nothing(followed, poller):
    session, engine, _a, _b = followed
    assert poller.poll_once() == 0
    assert not engine.has_pending
    tick = engine.apply()
    assert not tick.anything
    assert tick.added_rows == 0


def test_the_watcher_thread_does_no_work_of_its_own(followed, poller):
    """
    SPEC 7.4's threading rule. The callback records and returns; nothing is
    indexed, merged or repainted until the owner thread calls `apply`.
    """
    session, engine, a, _b = followed
    before = session.total_rows
    add(a, "a", [30, 32])
    poller.poll_once()
    assert engine.has_pending
    assert session.total_rows == before          # nothing happened yet
    engine.apply()
    assert session.total_rows == before + 2


def test_notify_is_called_on_the_poller_thread(tmp_path, poller):
    """What the UI hangs its thread hop on (see `ui.followui`)."""
    path = tmp_path / "n.log"
    write(path, "n", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    woken: list[int] = []
    engine = FollowEngine(session, poller=poller, notify=lambda: woken.append(1))
    engine.set_global_follow(True)
    try:
        add(path, "n", [2])
        poller.poll_once()
        assert woken == [1]
    finally:
        engine.stop()
        session.close()


# ---------------------------------------------------------------------------
# FR-24: what is followed
# ---------------------------------------------------------------------------

def test_follow_is_off_until_it_is_asked_for(tmp_path, poller):
    path = tmp_path / "o.log"
    write(path, "o", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    try:
        assert poller.watching == 0
        add(path, "o", [2])
        assert step(poller, engine).added_rows == 0
        assert session.total_rows == 1
    finally:
        engine.stop()
        session.close()


def test_turning_follow_off_and_on_keeps_the_per_file_choices(followed, poller):
    """
    The global toggle must not be destructive. A user who switched one noisy
    file off and then paused everything for a minute has not asked for that
    choice to be forgotten.
    """
    session, engine, _a, _b = followed
    engine.set_follow(1, False)
    assert poller.watching == 1

    engine.set_global_follow(False)
    assert poller.watching == 0
    engine.set_global_follow(True)

    assert poller.watching == 1
    assert session.sources[0].meta.follow is True
    assert session.sources[1].meta.follow is False


def test_a_file_with_follow_off_still_shows_what_it_had(followed, poller):
    session, engine, a, b = followed
    engine.set_follow(1, False)
    before = session.total_rows
    add(a, "a", [20])
    add(b, "b", [21])
    tick = step(poller, engine)

    assert tick.added_rows == 1
    assert tick.changed == [0]
    assert session.total_rows == before + 1


def test_a_source_that_cannot_be_followed_says_so_rather_than_pretending(
        tmp_path, poller, monkeypatch):
    """
    SPEC 7.1.1 rung 4: a VSS snapshot does not grow. Telling the user it is
    being followed would have them watching a frozen file for lines that can
    never arrive.
    """
    path = tmp_path / "v.log"
    write(path, "v", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    monkeypatch.setattr(type(session.sources[0].source), "watch",
                        lambda self, on_change: None)
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        state = engine.states[str(session.sources[0].source.id)]
        assert not state.following
        assert "cannot be followed" in state.note
    finally:
        engine.stop()
        session.close()


# ---------------------------------------------------------------------------
# FR-23 and FR-25: rotation, truncation, disappearance -- risk R5
# ---------------------------------------------------------------------------

def test_a_truncated_source_is_reread_from_the_start(followed, poller):
    session, engine, a, _b = followed
    add(a, "a", range(20, 30, 2))
    step(poller, engine)

    write(a, "a", [40])                        # truncate and rewrite in place
    tick = step(poller, engine)

    assert tick.truncated == [0]
    assert tick.rebuilt
    assert session.sources[0].index.n == 1
    state = engine.states[str(session.sources[0].source.id)]
    assert state.generation == 1
    assert state.truncations == 1
    assert "truncated" in state.note
    # The other source is untouched by its neighbour's accident.
    assert session.sources[1].index.n == 10


def test_truncation_keeps_following_the_file_afterwards(followed, poller):
    session, engine, a, _b = followed
    write(a, "a", [40])
    step(poller, engine)
    add(a, "a", [41, 42])
    tick = step(poller, engine)

    assert tick.added_rows == 2
    assert session.sources[0].index.n == 3


def test_a_rotation_freezes_the_old_file_and_opens_the_new_one(tmp_path, poller):
    """
    SPEC 7.2 and PRD Q7. The open handle still serves the renamed file, so the
    frozen source stays fully readable -- that is what "without losing
    already-read history" means for a rotation (FR-23).
    """
    path = tmp_path / "rot.log"
    write(path, "rot", range(1, 10, 2))
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        old_rows = [r.text for r in session.rows(0, session.total_rows)]
        os.replace(path, tmp_path / "rot.log.1")
        write(path, "rot", [50])
        tick = step(poller, engine)

        assert tick.rotated == [0]
        assert tick.went_stale == [0]
        assert len(tick.successors) == 1
        assert len(session.sources) == 2
        # Every row the old file had is still there, plus the new file's.
        now = [r.text for r in session.rows(0, session.total_rows)]
        assert now[:len(old_rows)] == old_rows
        assert "rot line 50" in now[-1]
    finally:
        engine.stop()
        session.close()


def test_the_rotated_file_and_its_successor_get_tellable_names(tmp_path, poller):
    """
    FR-29 cannot distinguish them: both have the same origin path, so the
    shortest-unique rule produces `rot` and `rot~2` and leaves the reader to
    guess which is yesterday's. Both names are therefore assigned outright.
    """
    path = tmp_path / "rot.log"
    write(path, "rot", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        os.replace(path, tmp_path / "rot.log.1")
        write(path, "rot", [50])
        step(poller, engine)
        assert [s.alias for s in session.sources] == ["rot (rotated)", "rot"]
    finally:
        engine.stop()
        session.close()


def test_the_successor_of_a_rotation_is_followed_too(tmp_path, poller):
    """
    The one that would be easy to miss: the successor is added by the engine
    itself, so nothing outside it knows to start watching the new file.
    """
    path = tmp_path / "rot.log"
    write(path, "rot", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        os.replace(path, tmp_path / "rot.log.1")
        write(path, "rot", [50])
        step(poller, engine)
        assert poller.watching == 1          # the old one has stopped

        add(path, "rot", [51])
        tick = step(poller, engine)
        assert tick.added_rows == 1
        assert "rot line 51" in session.rows(session.total_rows - 1, 1)[0].text
    finally:
        engine.stop()
        session.close()


def test_lines_written_just_before_a_rotation_are_not_lost(tmp_path, poller):
    """
    A writer that appends and then immediately renames gives one tick to see
    both, and the append has to be read from the old handle before the source
    is frozen -- otherwise the last lines of every rotated log go missing.
    """
    path = tmp_path / "rot.log"
    write(path, "rot", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        add(path, "rot", [2, 3])
        os.replace(path, tmp_path / "rot.log.1")
        write(path, "rot", [50])
        step(poller, engine)

        texts = [r.text for r in session.rows(0, session.total_rows)]
        assert any("rot line 2" in t for t in texts)
        assert any("rot line 3" in t for t in texts)
        assert any("rot line 50" in t for t in texts)
    finally:
        engine.stop()
        session.close()


def test_a_deleted_file_is_marked_stale_and_stays_readable(tmp_path, poller):
    """FR-25. The content already read is no worse for the file being gone."""
    path = tmp_path / "del.log"
    write(path, "del", [1, 2, 3])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        before = [r.text for r in session.rows(0, session.total_rows)]
        os.remove(path)
        tick = step(poller, engine)

        assert tick.went_stale == [0]
        state = engine.states[str(session.sources[0].source.id)]
        assert state.stale and not state.live
        assert "gone" in state.stale_reason
        assert [r.text for r in session.rows(0, session.total_rows)] == before
        assert poller.watching == 0            # and it stops being polled
    finally:
        engine.stop()
        session.close()


def test_a_stale_source_is_not_picked_back_up_by_a_sync(tmp_path, poller):
    path = tmp_path / "del.log"
    write(path, "del", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        os.remove(path)
        step(poller, engine)
        engine.sync()
        assert poller.watching == 0
        assert engine.states[str(session.sources[0].source.id)].stale
    finally:
        engine.stop()
        session.close()


ROTATIONS = [
    pytest.param("rename", id="rename-then-recreate"),
    pytest.param("delete", id="delete-then-recreate"),
    pytest.param("copytruncate", id="copy-then-truncate-in-place"),
]


@pytest.mark.parametrize("style", ROTATIONS)
def test_every_rotation_style_leaves_a_session_that_agrees_with_a_cold_one(
        tmp_path, poller, style):
    """
    Risk R5, driven by what rotating writers actually do.

    `rename` is logrotate's default and Windows' own; `delete` is logrotate with
    `nocreate`, or a service that unlinks its log on restart; `copytruncate` is
    logrotate's option for a writer that will not reopen, and is the case that
    empties the file under us.

    The assertion is the same for all three: whatever the engine ended up with
    must be reachable by opening the files that now exist.
    """
    path = tmp_path / "svc.log"
    write(path, "svc", [1, 2, 3])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        add(path, "svc", [4])
        if style == "rename":
            os.replace(path, tmp_path / "svc.log.1")
            write(path, "svc", [10, 11])
        elif style == "delete":
            os.remove(path)
            write(path, "svc", [10, 11])
        else:
            (tmp_path / "svc.log.1").write_bytes(path.read_bytes())
            write(path, "svc", [10, 11])

        step(poller, engine)
        texts = [r.text for r in session.rows(0, session.total_rows)]
        assert any("svc line 10" in t for t in texts)
        assert any("svc line 11" in t for t in texts)

        # Whatever survived, it is what the files on disk say.
        live = [str(p) for p in sorted(tmp_path.glob("svc.log"))]
        for row in cold_rows(live):
            assert row in texts
    finally:
        engine.stop()
        session.close()


def test_a_replace_over_an_open_path_is_blocked_by_windows_not_by_us(tmp_path):
    """
    NFR-20, and the one rotation style that cannot be permitted -- measured.

    `os.replace` is `MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`, and on
    Windows that fails with ERROR_ACCESS_DENIED whenever **any** reader holds
    the destination open. This test pins that it is not our share flags doing
    it: a plain `open(path, "rb")`, which requests nothing unusual, blocks it
    exactly the same way.

    The API Windows provides for swapping a file in place with readers attached
    is `ReplaceFileW`, and that succeeds against our handle. So every rotation
    style a writer can actually use is permitted -- rename away, delete,
    `ReplaceFileW`, copytruncate -- and the one that is not is a property of
    `MoveFileEx` rather than of anything LogWeave does.
    """
    target, incoming = tmp_path / "t.log", tmp_path / "i.log"
    write(target, "t", [1])
    write(incoming, "i", [2])

    with open(target, "rb"):
        with pytest.raises(OSError) as plain:
            os.replace(incoming, target)
    assert plain.value.winerror == 5

    with FileSource(target) as src:
        src.unmap()
        with pytest.raises(OSError) as ours:
            os.replace(incoming, target)
    assert ours.value.winerror == plain.value.winerror

    # And with nothing holding it, the very same call works.
    os.replace(incoming, target)
    assert target.read_text(encoding="utf-8").strip().endswith("i line 2")


def test_a_rotating_writer_can_rename_and_delete_a_file_we_are_following(
        tmp_path, poller):
    """
    NFR-20, positively. The two things a rotating writer does must both work
    while we hold the file open and are watching it.
    """
    path = tmp_path / "w.log"
    write(path, "w", range(5))
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        os.replace(path, tmp_path / "w.log.1")          # rename away
        os.remove(tmp_path / "w.log.1")                 # and delete
        assert not path.exists()
    finally:
        engine.stop()
        session.close()


def test_a_followed_file_can_be_reopened_with_truncation_by_its_writer(
        tmp_path, poller):
    """
    NFR-20, and the reason `FileSource.unmap` exists.

    Windows refuses a `CREATE_ALWAYS` open -- what `open(path, "w")` and
    `logging.FileHandler(mode="w")` do -- against a file with a live mapping,
    with ERROR_USER_MAPPED_FILE. An application restarting would find its own
    log unwritable purely because we were watching it, so following gives up
    the mapping.
    """
    path = tmp_path / "m.log"
    write(path, "m", range(5))
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        with open(path, "w", encoding="utf-8") as handle:     # must not raise
            handle.write(line("m", 99, 99))
        tick = step(poller, engine)
        assert tick.truncated == [0]
    finally:
        engine.stop()
        session.close()


# ---------------------------------------------------------------------------
# NFR-8: what a tick costs
# ---------------------------------------------------------------------------

def test_a_tick_is_cheap_enough_to_be_invisible(tmp_path, poller):
    """
    NFR-8 allows 200ms from a line being written to it being visible, of which
    the poll interval spends 150. This asserts the processing half has room,
    with a wide margin so it is a regression test and not a benchmark masquerading
    as one.
    """
    path = tmp_path / "big.log"
    write(path, "big", range(0, 4000))
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        add(path, "big", range(4000, 4020))
        tick = step(poller, engine)
        assert tick.added_rows == 20
        assert tick.elapsed_ms < 100.0
    finally:
        engine.stop()
        session.close()


def test_extending_the_merge_is_preferred_to_rebuilding_it(followed, poller):
    """
    An ordinary append must not rebuild the checkpoints. The distinction is
    invisible until the merge is large, which is exactly when it stops being
    affordable -- so it is asserted on a small one, where it is cheap to check.
    """
    session, engine, a, _b = followed
    add(a, "a", range(20, 40, 2))
    tick = step(poller, engine)
    assert not tick.rebuilt
    assert tick.first_changed_row >= 0


def test_a_short_log_that_has_no_layout_yet_is_re_indexed_rather_than_dropped(
        tmp_path, poller):
    """
    A file of a few lines is detected by the scalar pattern, not a fixed layout
    (SPEC 4.3), and without a layout there is nothing to resume a tail index
    from. Treating that as "no change" would silently lose every appended line
    until the file happened to grow enough -- so it falls back to a re-index.
    """
    path = tmp_path / "tiny.log"
    write(path, "tiny", [1])
    session = Session(use_cache=False)
    session.add_files([path])
    engine = FollowEngine(session, poller=poller)
    engine.set_global_follow(True)
    try:
        add(path, "tiny", [2])
        tick = step(poller, engine)
        assert tick.added_rows >= 1
        assert session.total_rows == 2
        assert [r.text for r in session.rows(0, 2)] == cold_rows([path])
    finally:
        engine.stop()
        session.close()


def test_the_merge_keeps_pointing_at_the_index_the_append_produced(followed, poller):
    """
    Regression, and a silent one. An append returns a *new* `FileIndex`, so
    assigning it to the session source alone leaves the merge reading the old
    object -- which looks exactly like a follow that works except that no row
    ever appears.
    """
    session, engine, a, _b = followed
    add(a, "a", [20])
    step(poller, engine)
    assert session.merge is not None
    assert session.merge.sources[0].index is session.sources[0].index
    assert session.merge.total == session.total_rows


def test_appended_rows_are_in_the_overview_strips_without_a_rebuild(followed, poller):
    session, engine, a, _b = followed
    first = session.overview.minimap(16)
    add(a, "a", range(20, 60, 2))
    step(poller, engine)
    second = session.overview.minimap(16)
    assert int(second.counts.sum()) == session.total_rows
    assert not np.array_equal(first.counts, second.counts)
