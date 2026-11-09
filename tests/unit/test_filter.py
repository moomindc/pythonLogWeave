"""
Filtering -- FR-45, FR-48, NFR-9, SPEC 11.2.

Two things are load-bearing and neither is visible from the UI:

* a filtered merge's checkpoints are **derived** from the unfiltered merge's
  rather than built, so the first half of this file holds the derived table
  against a from-scratch build over random masks. A merge whose row order
  depended on how its checkpoints were made would be the worst defect this
  design can have, and it would look exactly like a working filter.
* a filter must be **reversible for nothing** (FR-45): the unfiltered merge is
  retained, never rebuilt.
"""

from __future__ import annotations

import numpy as np
import pytest

from logweave.index.fileindex import FileIndex
from logweave.index.severity import Severity
from logweave.merge import MergeIndex, MergeSource
from logweave.merge.selection import BLOCK, Selection, sorted_mask
from logweave.session import Session, filters
from logweave.session.filters import ALL_SEVERITIES, Filter

US = 1_000_000


def fake_index(ts, order=None) -> FileIndex:
    ts = np.asarray(ts, dtype=np.int64)
    n = int(ts.size)
    return FileIndex(ts=ts, off=np.arange(n + 1, dtype=np.uint64) * 100,
                     flags=np.zeros(n, dtype=np.uint8),
                     line_no=np.arange(n, dtype=np.uint32) + 1, order=order)


def write_log(path, rows):
    """`rows` is (second offset, level, text)."""
    with path.open("w", encoding="utf-8") as fh:
        for sec, level, text in rows:
            h, rem = 10 + sec // 3600, sec % 3600
            fh.write(f"2026-10-04 {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                     f".000000 {level} {text}\n")
    return path


def session_over(*paths) -> Session:
    s = Session(use_cache=False)
    s.add_files(list(paths))
    return s


@pytest.fixture
def mixed(tmp_path):
    """One file, 120 rows, a quarter of them warnings or worse."""
    rows = []
    for sec in range(120):
        level = ("ERROR" if sec % 8 == 0 else
                 "WARN" if sec % 8 == 4 else
                 "DEBUG" if sec % 3 == 0 else "INFO")
        rows.append((sec, level, f"request {sec}"
                     + (" timeout" if sec % 10 == 0 else "")))
    return write_log(tmp_path / "mixed.log", rows)


# ---------------------------------------------------------------------------
# Selection: the mask arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n,p", [(0, 0.5), (1, 1.0), (7, 0.5), (BLOCK, 0.3),
                                 (BLOCK * 3 + 11, 0.1), (BLOCK * 2, 1.0),
                                 (BLOCK * 2, 0.0), (50_000, 0.02)])
def test_selection_agrees_with_flatnonzero(n, p):
    """
    `Selection` exists to answer rank and position without storing the
    positions. It has to give the same answers as storing them would.
    """
    rng = np.random.default_rng(n + 7)
    keep = rng.random(n) < p
    sel = Selection(keep)
    truth = np.flatnonzero(keep)
    assert sel.count == truth.size

    for pos in list(range(min(n, 40))) + [n // 2, n, max(0, n - 1)]:
        assert sel.rank(pos) == int(np.count_nonzero(keep[:pos]))
    for i in list(range(min(sel.count, 20))) + ([sel.count] if sel.count else []):
        assert sel.position(i) == (int(truth[i]) if i < sel.count else n)
    for pos in (0, n // 3, max(0, n - 1), n):
        for count in (1, 5, 100):
            want = truth[truth >= pos][:count]
            assert np.array_equal(sel.take(pos, count), want)

    asked = np.unique(rng.integers(0, n + 1, size=150)) if n else np.zeros(1, int)
    want = np.array([np.count_nonzero(keep[:q]) for q in asked])
    assert np.array_equal(sel.ranks_sorted(asked), want)


def test_ranks_sorted_survives_repeated_positions():
    """
    `np.add.reduceat` returns `keep[i]`, not zero, for a zero-width segment.
    A sparse filter's checkpoint table is full of repeated positions, and
    without the deduplication every repeat would add one phantom kept entry --
    enough to put a derived checkpoint one row out.
    """
    keep = np.ones(50, dtype=bool)
    sel = Selection(keep)
    asked = np.array([0, 0, 7, 7, 7, 20, 50, 50])
    assert sel.ranks_sorted(asked).tolist() == [0, 0, 7, 7, 7, 20, 50, 50]


def test_the_mask_is_permuted_once_for_a_file_that_was_out_of_order():
    """
    Every position in `Selection` is a *sorted* position. A file that needed
    reordering (SPEC 4.6) has a different mask in each space, and mixing them
    up keeps the wrong lines.
    """
    order = np.array([2, 0, 1])
    keep = np.array([True, False, False])          # entry 0 only
    assert sorted_mask(keep, order).tolist() == [False, True, False]
    assert sorted_mask(keep, None) is not None


def test_selection_costs_one_byte_an_entry():
    """
    The reason it is a mask and not a position list: 8 bytes a kept row would
    be 800 MB at NFR-9's scale, against NFR-7's 500 MB for everything.
    """
    sel = Selection(np.ones(1_000_000, dtype=bool))
    assert sel.nbytes < 1_100_000


# ---------------------------------------------------------------------------
# the derived checkpoint table (NFR-9)
# ---------------------------------------------------------------------------

def _random_case(rng):
    srcs, keeps = [], []
    for j in range(int(rng.integers(1, 4))):
        n = int(rng.integers(0, 9000))
        # A coarse clock, so timestamps repeat heavily within and across files
        # -- which is what makes the tie-breaking rules matter.
        ts = np.sort(rng.integers(0, max(1, n // 3 + 1), size=n).astype(np.int64))
        srcs.append(MergeSource(index=fake_index(ts),
                                skew_us=int(rng.integers(-3, 4)), name=f"s{j}"))
        keeps.append(rng.random(n) < float(rng.choice([0.0, 0.01, 0.3, 0.9, 1.0])))
    return srcs, keeps


@pytest.mark.parametrize("seed", range(12))
def test_a_derived_table_merges_exactly_as_a_built_one(seed):
    rng = np.random.default_rng(seed)
    srcs, keeps = _random_case(rng)
    parent = MergeIndex(srcs, checkpoint=64)
    derived = parent.filtered(keeps)
    scratch = MergeIndex(
        [MergeSource(index=s.index, skew_us=s.skew_us, name=s.name, keep=keeps[i])
         for i, s in enumerate(srcs)], checkpoint=64)

    assert derived.total == scratch.total
    a, b = derived.materialise(), scratch.materialise()
    assert np.array_equal(a.source, b.source)
    assert np.array_equal(a.entry, b.entry)
    assert np.array_equal(a.ts, b.ts)

    for _ in range(6):
        if not derived.total:
            break
        start = int(rng.integers(0, derived.total))
        count = int(rng.integers(1, 200))
        got = derived.rows(start, count)
        assert np.array_equal(got.entry, a.entry[start:start + count])
        assert np.array_equal(got.source, a.source[start:start + count])


@pytest.mark.parametrize("seed", range(6))
def test_row_of_inverts_row_at_on_a_filtered_merge(seed):
    rng = np.random.default_rng(100 + seed)
    srcs, keeps = _random_case(rng)
    merge = MergeIndex(srcs, checkpoint=64).filtered(keeps)
    if not merge.total:
        pytest.skip("this draw filtered everything out")
    for row in rng.integers(0, merge.total, size=min(40, merge.total)):
        source, entry, _ts = merge.row_at(int(row))
        assert merge.row_of(source, entry) == int(row)
        assert int(merge.cursors_at_row(int(row)).sum()) == int(row)


def test_rows_of_agrees_with_row_of_one_at_a_time():
    """
    `rows_of` is the vectorised form a search's hit list needs. It is a second
    implementation of the same arithmetic, so it is held against the first.
    """
    rng = np.random.default_rng(5)
    srcs, keeps = _random_case(rng)
    merge = MergeIndex(srcs, checkpoint=64).filtered(keeps)
    if not merge.total:
        pytest.skip("this draw filtered everything out")
    rows = rng.integers(0, merge.total, size=min(50, merge.total))
    pairs = [merge.row_at(int(r))[:2] for r in rows]
    sources = np.array([p[0] for p in pairs])
    entries = np.array([p[1] for p in pairs])
    assert np.array_equal(merge.rows_of(sources, entries),
                          np.array([merge.row_of(s, e)
                                    for s, e in zip(sources, entries,
                                                    strict=True)]))


def test_the_derived_table_keeps_a_checkpoint_at_row_zero():
    """
    `_replay` resumes from the last checkpoint at or before a row, and reads
    `cp_cursors[0]` when there is none before it. A table whose first entry was
    not row 0 would replay from the wrong cursors with a negative skip.
    """
    ts = np.arange(20_000, dtype=np.int64)
    parent = MergeIndex([MergeSource(index=fake_index(ts))], checkpoint=64)
    keep = np.zeros(20_000, dtype=bool)
    keep[10_000:] = True
    derived = parent.filtered([keep])
    assert derived.stats.checkpoints >= 1
    assert derived.rows(0, 1).entry.tolist() == [10_000]


def test_a_derived_table_notes_that_it_was_derived():
    ts = np.arange(50_000, dtype=np.int64)
    parent = MergeIndex([MergeSource(index=fake_index(ts))])
    derived = parent.filtered([np.ones(50_000, dtype=bool)])
    assert any("derived" in n for n in derived.stats.notes)


def test_deriving_is_far_cheaper_than_building():
    """
    The whole point. NFR-9 gives a filter 500 ms on a 100M-row set and a build
    is 2.7 Mrow/s, so this ratio is the requirement, not a nicety. Measured on
    a much smaller merge here, with a loose bound, because the suite has to
    stay fast and run on a shared CI runner.
    """
    import time

    ts = np.arange(400_000, dtype=np.int64)
    srcs = [MergeSource(index=fake_index(ts + j)) for j in range(3)]
    parent = MergeIndex(srcs)
    keep = [np.ones(ts.size, dtype=bool) for _ in srcs]

    t0 = time.perf_counter()
    parent.filtered(keep)
    derived = time.perf_counter() - t0

    t0 = time.perf_counter()
    MergeIndex([MergeSource(index=s.index, keep=keep[i])
                for i, s in enumerate(srcs)])
    built = time.perf_counter() - t0
    assert derived < built / 3, f"derive {derived * 1e3:.1f}ms, build {built * 1e3:.1f}ms"


# ---------------------------------------------------------------------------
# tier 1: the metadata predicate
# ---------------------------------------------------------------------------

def test_a_severity_filter_keeps_only_those_classes(mixed):
    s = session_over(mixed)
    whole = s.total_rows
    want = frozenset({int(Severity.ERROR), int(Severity.WARNING)})
    s.set_filter(filters.apply(s, Filter(severities=want)))
    assert 0 < s.total_rows < whole
    for row in s.rows(0, s.total_rows):
        assert row.severity in want
    assert s.unfiltered_rows == whole
    s.close()


def test_a_time_window_keeps_only_that_window(mixed):
    s = session_over(mixed)
    lo, hi = s.time_span()
    mid = lo + (hi - lo) // 2
    s.set_filter(filters.apply(s, Filter(since=mid, until=hi)))
    assert s.total_rows
    for row in s.rows(0, s.total_rows):
        assert mid <= row.ts <= hi
    s.close()


def test_a_source_filter_hides_the_others(tmp_path):
    a = write_log(tmp_path / "a.log", [(s, "INFO", f"a{s}") for s in range(20)])
    b = write_log(tmp_path / "b.log", [(s, "INFO", f"b{s}") for s in range(20)])
    s = session_over(a, b)
    s.set_filter(filters.apply(s, Filter(sources=frozenset({1}))))
    assert s.total_rows == 20
    assert {r.source for r in s.rows(0, 20)} == {1}
    s.close()


def test_clearing_a_filter_restores_the_merge_object_itself(mixed):
    """
    FR-45: reversible without re-indexing. The unfiltered merge is the *same
    object* afterwards, which is the only proof that nothing was rebuilt.
    """
    s = session_over(mixed)
    before = s.merge
    rows = s.total_rows
    s.set_filter(filters.apply(s, Filter(severities=frozenset({int(Severity.ERROR)}))))
    assert s.merge is not before
    s.set_filter(None)
    assert s.merge is before
    assert s.total_rows == rows
    s.close()


def test_an_inactive_filter_is_the_same_as_no_filter(mixed):
    s = session_over(mixed)
    rows = s.total_rows
    s.set_filter(filters.apply(s, Filter()))
    assert not s.is_filtered and s.total_rows == rows
    s.set_filter(filters.apply(s, Filter(severities=ALL_SEVERITIES)))
    assert s.total_rows == rows
    s.close()


def test_a_filter_that_passes_nothing_leaves_an_empty_but_usable_view(mixed):
    s = session_over(mixed)
    s.set_filter(filters.apply(s, Filter(include="no-such-text-anywhere")))
    assert s.total_rows == 0
    assert s.rows(0, 10) == []
    assert s.time_span() == (0, 0)
    s.set_filter(None)
    assert s.total_rows > 0
    s.close()


# ---------------------------------------------------------------------------
# tier 2: the text predicate
# ---------------------------------------------------------------------------

def test_include_and_exclude_compose(mixed):
    s = session_over(mixed)
    got = filters.apply(s, Filter(include="request", exclude="timeout"))
    s.set_filter(got)
    assert s.total_rows
    for row in s.rows(0, s.total_rows):
        assert "request" in row.text and "timeout" not in row.text
    s.close()


def test_text_and_metadata_compose(mixed):
    s = session_over(mixed)
    s.set_filter(filters.apply(s, Filter(
        severities=frozenset({int(Severity.ERROR)}), include="timeout")))
    for row in s.rows(0, s.total_rows):
        assert row.severity == int(Severity.ERROR) and "timeout" in row.text
    s.close()


def test_a_text_pass_skips_the_bytes_a_time_window_already_rejected(mixed):
    """
    SPEC 11.2: "tier 1 narrows the candidate set first, so a tier-2 pass over
    'ERROR lines in the last hour' reads only those bytes."
    """
    import logweave.session.scan as scanmod

    s = session_over(mixed)
    lo, hi = s.time_span()
    late = lo + (hi - lo) * 9 // 10
    scanmod.CHUNK_BYTES, old = 512, scanmod.CHUNK_BYTES
    try:
        narrow = filters.apply(s, Filter(since=late, include="request"))
        wide = filters.apply(s, Filter(include="request"))
    finally:
        scanmod.CHUNK_BYTES = old
    assert narrow.bytes_skipped > 0
    assert wide.bytes_skipped == 0
    s.close()


def test_a_cancelled_text_pass_is_incomplete_but_usable(mixed, monkeypatch):
    import logweave.session.scan as scanmod

    monkeypatch.setattr(scanmod, "CHUNK_BYTES", 256)
    s = session_over(mixed)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    got = filters.apply(s, Filter(include="request"), cancelled=cancel)
    assert got.cancelled and not got.complete
    s.set_filter(got)
    assert s.total_rows < 120, "it stopped early, so it cannot have them all"
    for row in s.rows(0, s.total_rows):
        assert "request" in row.text
    s.close()


def test_a_running_text_pass_publishes_partial_results(mixed, monkeypatch):
    import logweave.session.filters as filtmod
    import logweave.session.scan as scanmod

    monkeypatch.setattr(scanmod, "CHUNK_BYTES", 256)
    monkeypatch.setattr(filtmod, "PUBLISH_MS", 0.0)
    s = session_over(mixed)
    seen: list[int] = []
    final = filters.apply(s, Filter(include="request"),
                          partial=lambda f: seen.append(f.kept))
    assert len(seen) > 2
    assert seen == sorted(seen)
    assert seen[-1] <= final.kept
    s.close()


def test_an_invalid_regex_filters_nothing_and_says_why(mixed):
    s = session_over(mixed)
    got = filters.apply(s, Filter(include="(unclosed", regex=True))
    assert got.notes
    assert got.kept == got.total, "a filter nobody can evaluate hides nothing"
    s.close()


def test_a_source_whose_bytes_cannot_be_searched_fails_an_include(tmp_path):
    """
    A UTF-16 source cannot be scanned as bytes. An include it could not be
    tested against has not been met, and the note says so -- the alternative is
    keeping rows nobody proved match.
    """
    path = tmp_path / "u16.log"
    path.write_bytes("\n".join(
        f"2026-10-04 10:00:{s:02d}.000000 INFO needle {s}" for s in range(5)
    ).encode("utf-16"))
    s = session_over(path)
    got = filters.apply(s, Filter(include="needle"))
    assert got.kept == 0
    assert any("byte-oriented" in n for n in got.notes)
    s.close()


# ---------------------------------------------------------------------------
# the overview strips under a filter
# ---------------------------------------------------------------------------

def test_a_severity_filter_zeroes_the_histogram_columns_for_nothing(mixed):
    """
    The strips are drawn from the density histogram, which counts every entry.
    A severity filter is a column selection on `counts`, so it costs no pass
    over the entries at all -- and the strips stop showing bands in colours the
    filter just excluded.
    """
    s = session_over(mixed)
    before = s.sources[0].hist
    want = frozenset({int(Severity.ERROR)})
    s.set_filter(filters.apply(s, Filter(severities=want)))
    after = s.sources[0].hist
    assert after is not before
    assert after.counts[:, int(Severity.INFO)].sum() == 0
    assert after.counts[:, int(Severity.ERROR)].sum() == \
        before.counts[:, int(Severity.ERROR)].sum()
    s.close()


def test_the_zeroed_histogram_does_not_write_through_to_the_original(mixed):
    """
    `counts` can be the sidecar's memory map, and writing through it would
    corrupt the cache on disk.
    """
    s = session_over(mixed)
    original = np.array(s.sources[0].index.hist.counts, copy=True)
    s.set_filter(filters.apply(s, Filter(severities=frozenset({int(Severity.ERROR)}))))
    assert np.array_equal(s.sources[0].index.hist.counts, original)
    s.close()


def test_a_text_filter_rebuilds_the_histogram_from_the_kept_rows(mixed):
    s = session_over(mixed)
    before = s.sources[0].index.hist.total
    s.set_filter(filters.apply(s, Filter(include="timeout")))
    assert s.sources[0].hist.total == s.total_rows < before
    s.close()


def test_the_strips_see_the_filtered_stream(mixed):
    s = session_over(mixed)
    s.set_filter(filters.apply(s, Filter(severities=frozenset({int(Severity.ERROR)}))))
    strip = s.overview.minimap(40)
    assert strip.counts[:, int(Severity.INFO)].sum() == 0
    s.close()


# ---------------------------------------------------------------------------
# interactions
# ---------------------------------------------------------------------------

def test_opening_another_file_drops_the_filter_rather_than_misapplying_it(tmp_path):
    """
    A filter's masks are sized to each source's entry count, so a rebuild has
    invalidated them. Dropped, not silently reused -- a mask of the wrong
    length applied to the wrong file is how you hide the lines you asked for.
    """
    a = write_log(tmp_path / "a.log", [(s, "INFO", f"a{s}") for s in range(10)])
    b = write_log(tmp_path / "b.log", [(s, "ERROR", f"b{s}") for s in range(10)])
    s = session_over(a)
    s.set_filter(filters.apply(s, Filter(severities=frozenset({int(Severity.INFO)}))))
    assert s.is_filtered
    s.add_files([b])
    assert not s.is_filtered and s.filter is None
    assert s.total_rows == 20
    s.close()


def test_hiding_a_source_under_a_filter_affects_both_merges(tmp_path):
    a = write_log(tmp_path / "a.log", [(s, "INFO", f"a{s}") for s in range(10)])
    b = write_log(tmp_path / "b.log", [(s, "INFO", f"b{s}") for s in range(10)])
    s = session_over(a, b)
    s.set_filter(filters.apply(s, Filter(include="a")))
    assert s.total_rows == 10
    s.set_enabled(0, False)
    assert s.total_rows == 0
    s.set_enabled(0, True)
    assert s.total_rows == 10
    s.set_filter(None)
    assert s.total_rows == 20
    s.close()


def test_a_skew_under_a_filter_moves_both_merges(tmp_path):
    a = write_log(tmp_path / "a.log", [(s, "INFO", f"a{s}") for s in range(10)])
    s = session_over(a)
    s.set_filter(filters.apply(s, Filter(include="a")))
    lo, _hi = s.time_span()
    s.set_skew(0, 3600 * US)
    assert s.time_span()[0] == lo + 3600 * US
    s.close()


def test_the_filter_describes_itself_for_the_status_bar():
    f = Filter(severities=frozenset({int(Severity.ERROR)}), include="pool",
               since=1, until=2)
    text = f.describe()
    assert "ERROR" in text and "pool" in text and "time window" in text
    assert Filter().describe() == ""
    assert Filter().tier == 0
    assert Filter(severities=frozenset({1})).tier == 1
    assert Filter(include="x").tier == 2
