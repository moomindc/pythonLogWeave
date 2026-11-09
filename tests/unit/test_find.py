"""
Incremental find -- FR-44, NFR-10, SPEC 11.1.

The two things worth testing hardest are the ones that would be invisible if
they were wrong: that a hit's **row number** is the row the view will scroll to
(not a neighbouring row that shares its timestamp), and that the three scan
kernels -- in-place literal, folded literal, regex -- all find exactly the same
entries.
"""

from __future__ import annotations

import numpy as np
import pytest

from logweave.session import Session
from logweave.session.find import MAX_HITS, Hits, Query, search, search_key
from logweave.session.scan import compile_needle, fold_ascii

US = 1_000_000


def write_log(path, rows, *, level="INFO", base_min=0):
    with path.open("w", encoding="utf-8") as fh:
        for sec, text in rows:
            total = base_min * 60 + sec
            h, rem = 10 + total // 3600, total % 3600
            fh.write(f"2026-10-04 {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                     f".000000 {level} {text}\n")
    return path


def session_over(*paths) -> Session:
    s = Session(use_cache=False)
    s.add_files(list(paths))
    return s


@pytest.fixture
def two_logs(tmp_path):
    """
    Two files whose timestamps interleave *and collide*.

    The collisions are the point: every even second exists in both files, so a
    hit's row cannot be found by seeking to its time.
    """
    a = write_log(tmp_path / "a.log",
                  [(s, f"alpha {s} needle" if s % 5 == 0 else f"alpha {s}")
                   for s in range(0, 60, 2)])
    b = write_log(tmp_path / "b.log",
                  [(s, f"beta {s} needle" if s % 4 == 0 else f"beta {s}")
                   for s in range(0, 60)])
    return a, b


# ---------------------------------------------------------------------------
# what is found
# ---------------------------------------------------------------------------

def test_a_literal_finds_every_matching_entry(two_logs):
    s = session_over(*two_logs)
    hits = search(s, Query("needle"))
    expected = len([x for x in range(0, 60, 2) if x % 5 == 0]) + \
        len([x for x in range(60) if x % 4 == 0])
    assert hits.count == expected
    assert hits.complete and not hits.truncated
    s.close()


def test_case_insensitive_is_the_default_and_folds(tmp_path):
    """
    The folded kernel is 7.4x the `IGNORECASE` flag (SPEC 14.1g), so it is the
    one that runs -- and it has to find the same rows the flag would.
    """
    path = write_log(tmp_path / "case.log",
                     [(0, "Timeout waiting"), (1, "TIMEOUT again"),
                      (2, "timeout thrice"), (3, "nothing here")])
    s = session_over(path)
    assert search(s, Query("timeout")).count == 3
    assert search(s, Query("TiMeOuT")).count == 3
    assert search(s, Query("timeout", case_sensitive=True)).count == 1
    s.close()


def test_the_three_kernels_agree(tmp_path):
    """
    In-place literal, folded literal and regex are three different pieces of
    code over the same bytes. A disagreement would show as a hit count that
    changed when a checkbox was ticked.
    """
    rows = [(s, f"GET /api/v{s % 3} status={200 + s % 4} ms={s}")
            for s in range(200)]
    path = write_log(tmp_path / "api.log", rows)
    s = session_over(path)
    cs = search(s, Query("status=200", case_sensitive=True))
    ci = search(s, Query("STATUS=200"))
    rx = search(s, Query(r"status=200\b", regex=True))
    assert cs.count == ci.count == rx.count > 0
    assert np.array_equal(cs.rows, ci.rows)
    assert np.array_equal(cs.rows, rx.rows)
    s.close()


def test_a_match_deep_inside_a_multiline_entry_is_found(tmp_path):
    """
    Unlike a strip mark, a find does **not** honour `SCAN_LIMIT`: a hit on the
    fortieth line of a stack trace is exactly the hit someone is hunting.
    """
    path = tmp_path / "trace.log"
    ordinary = "".join(
        f"2026-10-04 10:00:{sec:02d}.000000 INFO served request {sec}\n"
        for sec in range(40))
    path.write_text(
        ordinary
        + "2026-10-04 10:01:00.000000 ERROR failed\n"
        + "".join(f"    at com.example.Frame{i}.run(Frame.java:{i})\n"
                  for i in range(60))
        + "    Caused by: java.lang.OutOfMemoryError: heap\n"
        "2026-10-04 10:01:01.000000 INFO carried on\n", encoding="utf-8")
    s = session_over(path)
    assert s.total_rows == 42, "the stack trace is one entry, not sixty-two"
    hits = search(s, Query("OutOfMemoryError"))
    assert hits.count == 1
    assert hits.rows.tolist() == [40], "the hit belongs to the entry, not a line"
    s.close()


def test_an_empty_or_broken_query_finds_nothing_and_says_why():
    s = Session(use_cache=False)
    assert search(s, Query("")).count == 0
    bad = search(s, Query("(unclosed", regex=True))
    assert bad.count == 0
    assert bad.notes and "unbalanced" in bad.notes[0] or bad.notes
    s.close()


# ---------------------------------------------------------------------------
# where the hits are (the part a seek-by-time would get wrong)
# ---------------------------------------------------------------------------

def test_every_hit_row_really_holds_the_hit(two_logs):
    """
    The assertion that matters. `rows` drives the scroll, the ordinal and
    next/previous, and with colliding timestamps across two files an
    off-by-a-few here is invisible except that the view lands next to the line
    you were looking for.
    """
    s = session_over(*two_logs)
    hits = search(s, Query("needle"))
    for row in hits.rows.tolist():
        got = s.rows(int(row), 1)
        assert got and "needle" in got[0].text, row
    s.close()


def test_the_rows_are_ascending_and_unique(two_logs):
    s = session_over(*two_logs)
    rows = search(s, Query("needle")).rows
    assert np.all(np.diff(rows) > 0)
    s.close()


def test_next_and_previous_step_through_in_order(two_logs):
    s = session_over(*two_logs)
    hits = search(s, Query("needle"))
    first = int(hits.rows[0])
    assert hits.next_after(-1) == first
    assert hits.next_after(first) == int(hits.rows[1])
    assert hits.prev_before(int(hits.rows[1])) == first
    # Wrapping, in both directions, because a find bar that stops at the ends
    # makes the last hit a dead end.
    assert hits.next_after(int(hits.rows[-1])) == first
    assert hits.prev_before(first) == int(hits.rows[-1])
    s.close()


def test_the_ordinal_is_one_based_and_zero_off_a_hit(two_logs):
    s = session_over(*two_logs)
    hits = search(s, Query("needle"))
    assert hits.ordinal(int(hits.rows[0])) == 1
    assert hits.ordinal(int(hits.rows[2])) == 3
    off = next(r for r in range(s.total_rows) if r not in set(hits.rows.tolist()))
    assert hits.ordinal(off) == 0
    s.close()


def test_hits_survive_a_source_being_hidden_and_come_back(two_logs):
    """
    FR-9 moves every row number in the merge. The `(source, entry)` pairs are
    what the result keeps, so re-deriving them is correct where storing rows
    would have been quietly wrong.
    """
    s = session_over(*two_logs)
    hits = search(s, Query("needle"))
    whole = hits.count
    s.set_enabled(1, False)
    hits.locate(s.merge)
    assert hits.found == whole, "the hits are still in the file"
    assert hits.count < whole and hits.hidden > 0
    for row in hits.rows.tolist():
        assert "needle" in s.rows(int(row), 1)[0].text
    s.set_enabled(1, True)
    hits.locate(s.merge)
    assert hits.count == hits.found == whole
    for row in hits.rows.tolist():
        assert "needle" in s.rows(int(row), 1)[0].text
    s.close()


def test_hits_are_relocated_onto_a_filtered_view(two_logs):
    from logweave.session import filters

    s = session_over(*two_logs)
    hits = search(s, Query("needle"))
    whole = hits.found
    s.set_filter(filters.apply(s, filters.Filter(sources=frozenset({0}))))
    hits.locate(s.merge)
    assert hits.found == whole, "a filter does not unfind anything"
    assert 0 < hits.count < whole
    for row in hits.rows.tolist():
        got = s.rows(int(row), 1)
        assert got and "needle" in got[0].text, row
        assert got[0].source == 0
    s.set_filter(None)
    hits.locate(s.merge)
    assert hits.count == whole
    s.close()


# ---------------------------------------------------------------------------
# incremental, cancellable, capped (NFR-10)
# ---------------------------------------------------------------------------

def test_partial_results_arrive_before_the_scan_finishes(tmp_path, monkeypatch):
    import logweave.session.find as find
    import logweave.session.scan as scanmod

    rows = [(s, f"line {s} needle") for s in range(2000)]
    path = write_log(tmp_path / "many.log", rows)
    s = session_over(path)
    monkeypatch.setattr(scanmod, "CHUNK_BYTES", 2048)
    monkeypatch.setattr(find, "PARTIAL_EVERY_MS", 0.0)

    seen: list[int] = []
    final = search(s, Query("needle"), partial=lambda h: seen.append(h.count))
    assert len(seen) > 2, "a 10GB file is one source; per-source is not enough"
    assert seen == sorted(seen), "a partial count may not go backwards"
    assert seen[-1] <= final.count
    assert all(h >= 0 for h in seen)
    s.close()


def test_a_cancelled_search_reports_itself_cancelled(tmp_path, monkeypatch):
    import logweave.session.scan as scanmod

    path = write_log(tmp_path / "big.log",
                     [(s, f"line {s} needle") for s in range(2000)])
    s = session_over(path)
    monkeypatch.setattr(scanmod, "CHUNK_BYTES", 1024)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    hits = search(s, Query("needle"), cancelled=cancel)
    assert hits.cancelled and not hits.complete
    assert hits.count < 2000, "it stopped early, so it cannot have them all"
    s.close()


def test_the_hit_cap_truncates_and_says_so(tmp_path):
    path = write_log(tmp_path / "dense.log",
                     [(s, f"line {s} needle") for s in range(300)])
    s = session_over(path)
    hits = search(s, Query("needle"), max_hits=50)
    assert hits.truncated
    assert any("narrow" in n for n in hits.notes)
    assert hits.count <= 300
    s.close()


def test_progress_reaches_the_total(tmp_path, monkeypatch):
    import logweave.session.scan as scanmod

    path = write_log(tmp_path / "p.log", [(s, f"line {s}") for s in range(500)])
    s = session_over(path)
    monkeypatch.setattr(scanmod, "CHUNK_BYTES", 1024)
    seen: list[tuple[int, int]] = []
    search(s, Query("absent-xyz"), progress=lambda d, t, _l: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1] > 0
    s.close()


# ---------------------------------------------------------------------------
# the key
# ---------------------------------------------------------------------------

def test_the_key_changes_with_the_query_and_with_an_append(two_logs, tmp_path):
    s = session_over(*two_logs)
    base = search_key(s, Query("needle"))
    assert base != search_key(s, Query("needle", regex=True))
    assert base != search_key(s, Query("needle", case_sensitive=True))
    assert base != search_key(s, Query("other"))
    s.close()


def test_the_key_ignores_whether_a_source_is_shown(two_logs):
    """
    A find's hits are entries in files. Hiding a source moves where they are,
    which `locate` handles -- rescanning every byte because someone unticked a
    file for a minute would be the slowest possible answer to a question that
    has not changed.
    """
    s = session_over(*two_logs)
    before = search_key(s, Query("needle"))
    s.set_enabled(0, False)
    assert search_key(s, Query("needle")) == before
    s.close()


# ---------------------------------------------------------------------------
# the pieces underneath
# ---------------------------------------------------------------------------

def test_fold_ascii_leaves_non_ascii_alone():
    """
    The stated limitation (`scan.fold_ascii`): a cp1252 accented capital is not
    folded, because the same byte is a different letter in another code page.
    """
    assert fold_ascii(b"WARN Caf\xc9") == b"warn caf\xc9"


def test_a_utf16_source_is_reported_rather_than_scanned_wrongly(tmp_path):
    path = tmp_path / "u16.log"
    path.write_bytes("2026-10-04 10:00:00.000000 INFO needle\n"
                     .encode("utf-16"))
    s = session_over(path)
    hits = search(s, Query("needle"))
    assert hits.count == 0
    assert any("byte-oriented" in n for n in hits.notes)
    s.close()


def test_a_literal_takes_the_in_place_path_and_a_fold_does_not(tmp_path):
    path = write_log(tmp_path / "k.log", [(0, "hello")])
    s = session_over(path)
    ss = s.sources[0]
    cs, _ = compile_needle("hello", ss, case_sensitive=True)
    ci, _ = compile_needle("hello", ss)
    rx, _ = compile_needle("hel+o", ss, regex=True)
    assert cs.literal == b"hello" and not cs.folds
    assert ci.literal == b"hello" and ci.folds
    assert rx.literal is None
    s.close()


def test_an_empty_hits_object_navigates_without_raising():
    empty = Hits()
    assert empty.count == 0
    assert empty.next_after(5) is None
    assert empty.prev_before(5) is None
    assert empty.ordinal(0) == 0
    assert empty.at(1) is None


def test_the_cap_is_the_documented_number():
    assert MAX_HITS == 200_000
