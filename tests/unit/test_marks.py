"""
The mark scan -- FR-31a, SPEC 8.4, 10.1.

The standard of correctness throughout is **the rows a reader would find by
eye**: a scan's hits must be exactly the entries whose text matches, no more
(a half-matched neighbour, an entry counted twice) and no fewer (the last row,
a hit on a chunk seam, a continuation line of a stack trace). Everything below
is one of those ways to be wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from logweave.highlight.rules import SCAN_LIMIT, HighlightRule
from logweave.index.severity import Severity
from logweave.session import Session
from logweave.session.marks import (
    MAX_HITS,
    MarkLayer,
    marking_rules,
    scan,
    scan_key,
)
from logweave.session.scan import chunks as _chunks

US = 1_000_000


def write_log(path, rows):
    """`rows` is (second_offset, text); the base is a fixed wall clock."""
    with path.open("w", encoding="utf-8") as fh:
        for sec, text in rows:
            h, rem = 10 + sec // 3600, sec % 3600
            fh.write(f"2026-10-04 {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                     f".000000 INFO {text}\n")
    return path


@pytest.fixture
def sparse(tmp_path):
    """Six hundred rows, of which every 97th mentions a timeout."""
    rows = [(s, f"served request {s}"
                + (" connection timeout after 30s" if s % 97 == 0 else ""))
            for s in range(600)]
    return write_log(tmp_path / "sparse.log", rows)


def session_over(*paths) -> Session:
    s = Session(use_cache=False)
    s.add_files(list(paths))
    return s


def rule(**kw) -> HighlightRule:
    kw.setdefault("name", "r")
    kw.setdefault("fg", "#ff4040")
    kw.setdefault("on_strips", True)
    return HighlightRule(**kw)


# ---------------------------------------------------------------------------
# which rules are scanned at all
# ---------------------------------------------------------------------------

def test_only_rules_that_asked_for_marks_are_scanned():
    """
    The opt-in is the cost control (see `HighlightRule.on_strips`), so it has
    to be the thing that decides, not the colour or the scope.
    """
    rules = [
        rule(name="marked", pattern="a"),
        rule(name="not-marked", pattern="b", on_strips=False),
        rule(name="off", pattern="c", enabled=False),
        rule(name="broken", pattern="(", is_regex=True),
    ]
    assert [r.name for r in marking_rules(rules)] == ["marked"]


def test_a_rule_that_will_not_compile_is_skipped_rather_than_raised():
    """
    Same posture as `Ruleset.rejected`: a user mid-edit must not be able to
    stop a scan, and a scan that raised would take a worker thread with it.
    """
    assert marking_rules([rule(pattern="[unclosed", is_regex=True)]) == []


# ---------------------------------------------------------------------------
# the hits themselves
# ---------------------------------------------------------------------------

def test_the_hits_are_exactly_the_matching_entries(sparse):
    s = session_over(sparse)
    got = scan(s, [rule(pattern="timeout")])
    assert got.notes == ()
    assert len(got.layers) == 1
    layer = got.layers[0]
    assert layer.rule == "r"
    assert layer.colour == "#ff4040"
    assert layer.hits == len([x for x in range(600) if x % 97 == 0])
    s.close()


def test_the_timestamps_are_the_matching_rows_own(sparse):
    """
    A layer is merged time, so every one of its values has to be a timestamp
    that a row actually has -- otherwise a mark lands between two rows and the
    strip points at nothing.
    """
    s = session_over(sparse)
    layer = scan(s, [rule(pattern="timeout")]).layers[0]
    every = {int(t) for t in s.sources[0].index.ts}
    assert {int(t) for t in layer.times} <= every
    assert list(layer.times) == sorted(layer.times), "a layer must be sorted"
    s.close()


def test_an_entry_matching_twice_is_still_one_mark(tmp_path):
    """
    `unique` over the entry indices, not over the match offsets. Without it a
    row saying "timeout timeout" would weight the strip twice and the hit
    count would not be a row count.
    """
    path = write_log(tmp_path / "twice.log",
                     [(0, "timeout then another timeout"), (1, "fine")])
    s = session_over(path)
    layer = scan(s, [rule(pattern="timeout")]).layers[0]
    assert layer.hits == 1
    s.close()


def test_nothing_matching_is_an_empty_layer_not_a_missing_one(sparse):
    """
    The layer still exists, because the strip reserves it a column: a rule the
    user marked and that happens to have no hits should read as "none here",
    not as a rule that was ignored.
    """
    s = session_over(sparse)
    got = scan(s, [rule(pattern="xyzzy-never-appears")])
    assert len(got.layers) == 1
    assert got.layers[0].hits == 0
    s.close()


def test_the_very_last_row_can_be_marked(tmp_path):
    """
    The off-by-one that `_aggregate` already has a paragraph about: a strip's
    last pixel edge sits exactly on the last entry's timestamp, so a half-open
    range drops a hit landing there. One missing mark in thousands looks
    exactly like a correct strip.
    """
    path = write_log(tmp_path / "last.log",
                     [(s, "ordinary" if s < 99 else "the needle")
                      for s in range(100)])
    s = session_over(path)
    layer = scan(s, [rule(pattern="needle")]).layers[0]
    assert layer.hits == 1
    strip = s.overview.minimap(40)
    assert int(layer.counts(strip.times).sum()) == 1, "the last hit must be drawn"
    assert layer.pixels(strip.times).tolist() == [39]
    s.close()


def test_a_hit_on_a_chunk_seam_is_found_once(tmp_path, monkeypatch):
    """
    Chunks are entry-aligned so a match cannot straddle one. Forced small
    enough here that the file is dozens of chunks, which is the only way to
    exercise a seam without writing a four-megabyte fixture.
    """
    import logweave.session.scan as scanmod

    rows = [(s, f"row {s} needle" if s % 7 == 0 else f"row {s}")
            for s in range(400)]
    path = write_log(tmp_path / "seams.log", rows)
    s = session_over(path)
    monkeypatch.setattr(scanmod, "CHUNK_BYTES", 256)
    chunks = list(_chunks(np.asarray(s.sources[0].index.off, dtype=np.int64)))
    assert len(chunks) > 10, "the point of the test is many seams"
    layer = scan(s, [rule(pattern="needle")]).layers[0]
    assert layer.hits == len([x for x in range(400) if x % 7 == 0])
    s.close()


def test_the_chunks_tile_the_file_exactly(sparse):
    """No gap and no overlap, or a hit is lost or counted twice."""
    s = session_over(sparse)
    off = np.asarray(s.sources[0].index.off, dtype=np.int64)
    chunks = list(_chunks(off))
    assert chunks[0][0] == 0
    assert chunks[-1][1] == int(off[-1])
    for (_, a), (b, _) in zip(chunks[:-1], chunks[1:], strict=True):
        assert a == b
    s.close()


def test_a_single_entry_larger_than_a_chunk_is_its_own_chunk(tmp_path, monkeypatch):
    """
    The corpus has a 1 MB single line. Collapsed cuts must leave it as one
    range rather than an empty one, which would read as a chunk of no bytes
    and skip the entry entirely.
    """
    import logweave.session.scan as scanmod

    path = tmp_path / "huge.log"
    path.write_text(
        "2026-10-04 10:00:00.000000 INFO small\n"
        "2026-10-04 10:00:01.000000 INFO " + ("x" * 4000) + " needle\n"
        "2026-10-04 10:00:02.000000 INFO small again\n", encoding="utf-8")
    s = session_over(path)
    monkeypatch.setattr(scanmod, "CHUNK_BYTES", 64)
    off = np.asarray(s.sources[0].index.off, dtype=np.int64)
    chunks = list(_chunks(off))
    assert all(hi > lo for lo, hi in chunks)
    assert chunks[-1][1] == int(off[-1])
    s.close()


# ---------------------------------------------------------------------------
# agreeing with the view
# ---------------------------------------------------------------------------

def test_a_match_past_the_scan_limit_does_not_mark(tmp_path):
    """
    The view bounds its scan to `SCAN_LIMIT` characters, so a strip marking a
    row the view then draws plain would send a reader to a row with nothing on
    it -- worse than no mark.
    """
    path = tmp_path / "long.log"
    filler = "y" * (SCAN_LIMIT + 200)
    path.write_text(
        f"2026-10-04 10:00:00.000000 INFO {filler} needle\n"
        "2026-10-04 10:00:01.000000 INFO needle early\n", encoding="utf-8")
    s = session_over(path)
    layer = scan(s, [rule(pattern="needle")]).layers[0]
    assert layer.hits == 1, "only the one inside the limit"
    assert int(layer.times[0]) == int(s.sources[0].index.ts[1])
    s.close()


def test_a_multiline_entry_marks_on_any_of_its_lines(tmp_path):
    """
    Deliberately *more* than the view sees. A collapsed row draws one physical
    line, so the view cannot colour an exception on the fourth line of a stack
    trace -- and telling a reader that entry is there is the whole job of a
    strip.
    """
    path = tmp_path / "trace.log"
    path.write_text(
        "2026-10-04 10:00:00.000000 INFO starting up\n"
        "2026-10-04 10:00:01.000000 INFO request failed\n"
        "\tat com.example.Thing.method(Thing.java:42)\n"
        "\tat com.example.Other.run(Other.java:7)  OutOfMemoryError\n"
        "2026-10-04 10:00:02.000000 INFO carrying on\n", encoding="utf-8")
    s = session_over(path)
    layer = scan(s, [rule(pattern="OutOfMemoryError")]).layers[0]
    assert layer.hits == 1
    assert int(layer.times[0]) == int(s.sources[0].index.ts[1]), (
        "the mark belongs to the entry, not to the physical line")
    s.close()


def test_a_caret_anchors_at_a_line_start_in_the_file(tmp_path):
    """
    MULTILINE, so `^` means a line of the file rather than the start of what
    the view happens to be drawing -- which moves when the timestamp column
    trims a duplicate. A mark that moved when a column was hidden would be a
    mark nobody could trust.
    """
    path = tmp_path / "frames.log"
    path.write_text(
        "2026-10-04 10:00:00.000000 INFO fine\n"
        "2026-10-04 10:00:01.000000 INFO failed\n"
        "Caused by: java.lang.IllegalStateException\n", encoding="utf-8")
    s = session_over(path)
    layer = scan(s, [rule(pattern=r"^Caused by:", is_regex=True)]).layers[0]
    assert layer.hits == 1
    s.close()


# ---------------------------------------------------------------------------
# several sources, skew, and what is switched on
# ---------------------------------------------------------------------------

def test_hits_from_several_sources_are_merged_in_time(tmp_path):
    a = write_log(tmp_path / "a.log", [(0, "needle"), (20, "no")])
    b = write_log(tmp_path / "b.log", [(10, "needle"), (30, "needle")])
    s = session_over(a, b)
    layer = scan(s, [rule(pattern="needle")]).layers[0]
    assert layer.hits == 3
    assert list(layer.times) == sorted(layer.times)
    s.close()


def test_a_skewed_source_marks_where_its_rows_actually_are(tmp_path):
    """
    D2: a skew shifts a source in merged time without touching its index, so
    the layer has to carry the shift -- or a corrected clock would move the
    rows and leave the marks behind.
    """
    path = write_log(tmp_path / "skewed.log", [(0, "needle")])
    s = session_over(path)
    before = int(scan(s, [rule(pattern="needle")]).layers[0].times[0])
    s.set_skew(0, 3600 * US)
    after = int(scan(s, [rule(pattern="needle")]).layers[0].times[0])
    assert after - before == 3600 * US
    s.close()


def test_a_disabled_source_contributes_no_marks(tmp_path):
    """FR-9: a source switched off is not in the overview it is a map of."""
    a = write_log(tmp_path / "a.log", [(0, "needle")])
    b = write_log(tmp_path / "b.log", [(10, "needle")])
    s = session_over(a, b)
    assert scan(s, [rule(pattern="needle")]).layers[0].hits == 2
    s.set_enabled(1, False)
    assert scan(s, [rule(pattern="needle")]).layers[0].hits == 1
    s.close()


# ---------------------------------------------------------------------------
# the cache key
# ---------------------------------------------------------------------------

def test_the_key_moves_when_the_answer_could_have(tmp_path):
    a = write_log(tmp_path / "a.log", [(0, "needle")])
    s = session_over(a)
    r = rule(pattern="needle")
    base = scan_key(s, [r])

    assert scan_key(s, [r]) == base, "the same question twice"
    assert scan_key(s, [rule(pattern="other")]) != base, "a different pattern"
    assert scan_key(s, [rule(pattern="needle", case_sensitive=True)]) != base, (
        "case sensitivity changes which rows match")
    s.set_skew(0, 60 * US)
    assert scan_key(s, [r]) != base, "a skew moves every mark"
    s.close()


def test_appending_to_a_source_moves_the_key(tmp_path):
    """
    SPEC 7.3: a followed file's append adds rows a mark could fall on, and the
    entry count is what notices -- which is why the key carries it rather than
    a content hash it would have to read the file to compute.
    """
    path = write_log(tmp_path / "grow.log", [(0, "needle")])
    s = session_over(path)
    before = scan_key(s, [rule(pattern="needle")])
    s.sources[0].index.ts = np.append(s.sources[0].index.ts,
                                      np.int64(s.sources[0].index.ts[-1] + US))
    assert scan_key(s, [rule(pattern="needle")]) != before
    s.close()


def test_an_unmarked_ruleset_has_a_key_of_its_own(sparse):
    """
    Nothing marked is a legitimate state with a legitimate answer -- no layers
    -- and it needs a key the scanner can short-circuit on, or it would rescan
    on every repaint.
    """
    s = session_over(sparse)
    assert scan_key(s, [rule(pattern="a", on_strips=False)]) == scan_key(s, [])
    s.close()


# ---------------------------------------------------------------------------
# cancellation and the cap
# ---------------------------------------------------------------------------

def test_a_cancelled_scan_says_so_and_keeps_no_half_layer(tmp_path):
    """
    Rule-outermost ordering exists for this: a layer missing is honest, a layer
    covering the first half of a file is a lie about where the hits are.
    """
    a = write_log(tmp_path / "a.log", [(s, "needle") for s in range(200)])
    b = write_log(tmp_path / "b.log", [(s, "needle") for s in range(200)])
    s = session_over(a, b)
    got = scan(s, [rule(pattern="needle")], cancelled=lambda: True)
    assert got.cancelled
    assert got.layers == ()
    s.close()


def test_progress_is_reported_and_reaches_the_total(sparse):
    seen = []
    s = session_over(sparse)
    scan(s, [rule(pattern="timeout")],
         progress=lambda done, total, name: seen.append((done, total, name)))
    assert seen, "a scan must report progress or a scan bar cannot move"
    assert all(name == "r" for _, _, name in seen)
    done, total, _ = seen[-1]
    assert done == total, "the last report accounts for every byte"
    s.close()


def test_a_pathologically_common_pattern_stops_at_the_cap(tmp_path, monkeypatch):
    """
    A rule matching everything marks every pixel, which says nothing, so the
    cap stops it and the layer admits it rather than the user waiting.
    """
    import logweave.session.marks as marks

    path = write_log(tmp_path / "everything.log",
                     [(s, f"INFO row {s}") for s in range(500)])
    s = session_over(path)
    monkeypatch.setattr(marks, "MAX_HITS", 10)
    got = marks.scan(s, [rule(pattern="row")])
    assert got.layers[0].truncated
    assert any("narrow the pattern" in n for n in got.notes)
    s.close()


def test_the_cap_is_high_enough_not_to_fire_on_a_real_rule():
    """A guard against someone tuning `MAX_HITS` down to where it bites."""
    assert MAX_HITS >= 100_000


# ---------------------------------------------------------------------------
# encodings
# ---------------------------------------------------------------------------

def test_a_utf16_source_is_reported_rather_than_scanned_wrongly(tmp_path):
    """
    Its bytes are not a byte-oriented encoding of the pattern, and scanning
    them anyway would find nothing while looking like it had looked. The note
    is what a user gets instead.
    """
    path = tmp_path / "wide.log"
    path.write_text(
        "2026-10-04 10:00:00.000000 INFO needle here\n"
        "2026-10-04 10:00:01.000000 INFO nothing\n", encoding="utf-16")
    s = session_over(path)
    got = scan(s, [rule(pattern="needle")])
    assert got.layers[0].hits == 0
    assert any("not byte-oriented" in n for n in got.notes)
    s.close()


# ---------------------------------------------------------------------------
# the layer as the strips use it
# ---------------------------------------------------------------------------

def test_a_layer_with_no_hits_draws_nothing():
    layer = MarkLayer(rule="r", colour="#ffffff",
                      times=np.empty(0, dtype=np.int64))
    edges = np.arange(0, 11, dtype=np.int64)
    assert layer.counts(edges).tolist() == [0] * 10
    assert layer.pixels(edges).size == 0


def test_counts_and_pixels_agree_about_presence():
    times = np.array([5, 5, 7, 90], dtype=np.int64)
    layer = MarkLayer(rule="r", colour="#ffffff", times=times)
    edges = np.array([0, 10, 50, 100], dtype=np.int64)
    assert layer.counts(edges).tolist() == [3, 0, 1]
    assert layer.pixels(edges).tolist() == [0, 2]


def test_degenerate_edges_do_not_raise():
    """A strip one pixel tall, or none at all, is a shape the window can ask for."""
    layer = MarkLayer(rule="r", colour="#ffffff",
                      times=np.array([1, 2], dtype=np.int64))
    assert layer.counts(np.array([], dtype=np.int64)).size == 0
    assert layer.counts(np.array([0], dtype=np.int64)).size == 0


def test_the_severity_rules_are_not_marked_by_default():
    """
    The measurement in the module docstring, as an assertion: the shipped
    thirty-branch ruleset must not be scannable by accident, because it is
    0.9 MB/s and 71 seconds on a 64 MB set.
    """
    from logweave.highlight import default_ruleset

    assert marking_rules(default_ruleset().rules) == []
    assert all(not r.on_strips for r in default_ruleset().rules)
    assert Severity.ERROR  # the shipped rules still carry their classes
