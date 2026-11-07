"""
The checkpointed merge -- SPEC 6, FR-17, FR-9, D2.

Two kinds of test here, and both are needed.

*Ground truth*: the merged order of the corpus scenario, every row, against the
generator's manifest -- which computes it independently by sorting
`(wall_us, file_order, entry_index)` in Python.

*Self-consistency*: `materialise()` and the checkpointed random-access path must
agree. They are different code paths -- one merges everything in a single window,
the other replays from a checkpoint -- so agreement between them is what proves
the checkpointing itself, as opposed to proving the sort.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import files_of, index_of

from logweave.index.fileindex import FileIndex
from logweave.merge import CHECKPOINT, MergeIndex, MergeSource, merge_of


def fake_index(ts, order=None, source_id="fake") -> FileIndex:
    """
    A `FileIndex` carrying only what the merge reads.

    Synthetic timestamps make the awkward cases -- long runs of duplicates,
    millions of rows, a deliberately reordered file -- reachable without
    generating a log file to match.
    """
    ts = np.asarray(ts, dtype=np.int64)
    n = ts.size
    return FileIndex(
        source_id=source_id,
        ts=ts,
        off=np.arange(n + 1, dtype=np.uint64) * 100,
        flags=np.zeros(n, dtype=np.uint8),
        line_no=(np.arange(n, dtype=np.uint32) + 1),
        order=None if order is None else np.asarray(order, dtype=np.uint64),
    )


def merged_pairs(m: MergeIndex) -> list[tuple[int, int]]:
    got = m.materialise()
    return list(zip(got.source.tolist(), got.entry.tolist(), strict=True))


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------

def test_merged_order_matches_ground_truth(corpus, manifests):
    """
    The headline: every merged row of the `basic` scenario, in order.

    The manifest's order is tie-broken `(wall_us, file_order, entry_index)` per
    FR-17, computed by the generator in plain Python. The engine arrives at it by
    a vectorised lexsort over checkpoint windows.
    """
    man = manifests["basic"]
    names = [f["name"] for f in man["files"]]
    indexes = [index_of(corpus / "basic" / n) for n in names]
    m = merge_of(indexes, names=names)

    assert m.total == len(man["merged"])
    assert merged_pairs(m) == [(f, e) for f, e in man["merged"]]


def test_random_access_agrees_with_the_full_merge(corpus, manifests):
    """
    `rows()` replays from a checkpoint; `materialise()` merges in one window.
    Agreement is what proves the checkpointing rather than the sort.
    """
    names = list(files_of(manifests["basic"]))
    m = merge_of([index_of(corpus / "basic" / n) for n in names], names=names)
    whole = m.materialise()

    rng = np.random.default_rng(11)
    for _ in range(60):
        start = int(rng.integers(0, m.total))
        count = int(rng.integers(1, 300))
        got = m.rows(start, count)
        end = min(start + count, m.total)
        assert np.array_equal(got.source, whole.source[start:end])
        assert np.array_equal(got.entry, whole.entry[start:end])
        assert np.array_equal(got.ts, whole.ts[start:end])


def test_merged_timestamps_are_ascending(corpus, manifests):
    """D13: time is the only ordering."""
    names = list(files_of(manifests["basic"]))
    m = merge_of([index_of(corpus / "basic" / n) for n in names], names=names)
    assert np.all(np.diff(m.materialise().ts) >= 0)


# ---------------------------------------------------------------------------
# tie-breaking (FR-17)
# ---------------------------------------------------------------------------

def test_ties_break_by_source_then_entry():
    """
    FR-17. Every entry shares one timestamp, so the order is decided entirely by
    the tie-break: all of source 0 in entry order, then all of source 1.
    """
    m = merge_of([fake_index([500] * 3), fake_index([500] * 2)])
    assert merged_pairs(m) == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1)]


def test_duplicate_timestamps_are_all_present():
    """Collapsing equal timestamps would silently drop log lines."""
    m = merge_of([fake_index([1, 1, 1, 2]), fake_index([1, 2, 2])])
    assert m.total == 7
    assert len(merged_pairs(m)) == 7


def test_a_long_run_of_identical_timestamps_still_builds():
    """
    Exercises the window-widening path. When every row in a window ties with an
    entry not yet read, nothing can be safely committed, so the window must grow
    rather than commit a row that may be overtaken.
    """
    n = 5000
    m = MergeIndex(
        [MergeSource(fake_index([7] * n)), MergeSource(fake_index([7] * n))],
        checkpoint=256,
    )
    m.build_checkpoints(window=64)
    assert m.total == 2 * n
    pairs = merged_pairs(m)
    assert pairs[:2] == [(0, 0), (0, 1)]
    assert pairs[n] == (1, 0)
    whole = m.materialise()
    assert np.array_equal(m.rows(0, m.total).source, whole.source)


# ---------------------------------------------------------------------------
# NFR-6: time_to_row
# ---------------------------------------------------------------------------

def test_time_to_row_is_exact():
    m = merge_of([fake_index([10, 20, 30]), fake_index([15, 25])])
    # merged: 10 20(?) -> order is 10,15,20,25,30
    assert m.time_to_row(0) == 0
    assert m.time_to_row(10) == 0
    assert m.time_to_row(11) == 1
    assert m.time_to_row(15) == 1
    assert m.time_to_row(20) == 2
    assert m.time_to_row(30) == 4
    assert m.time_to_row(31) == 5


def test_time_to_row_lands_on_the_first_of_a_duplicate_run():
    """
    `side="left"`, so seeking to a timestamp shows the first entry at it, not
    the last. Scrolling to a time must not skip rows at that time.
    """
    m = merge_of([fake_index([5, 5, 5]), fake_index([5])])
    assert m.time_to_row(5) == 0


def test_time_to_row_agrees_with_the_merged_order(corpus, manifests):
    """
    The property that matters: for any row, seeking to that row's timestamp must
    land at or before it, and the rows in between must share the timestamp.
    """
    names = list(files_of(manifests["basic"]))
    m = merge_of([index_of(corpus / "basic" / n) for n in names], names=names)
    whole = m.materialise()

    rng = np.random.default_rng(5)
    for r in rng.integers(0, m.total, 200):
        r = int(r)
        t = int(whole.ts[r])
        landed = m.time_to_row(t)
        assert landed <= r
        assert np.all(whole.ts[landed : r + 1] == t)


def test_row_to_time_round_trips(corpus, manifests):
    names = list(files_of(manifests["basic"]))
    m = merge_of([index_of(corpus / "basic" / n) for n in names], names=names)
    whole = m.materialise()
    for r in (0, 1, m.total // 2, m.total - 1):
        assert m.row_to_time(r) == int(whole.ts[r])


# ---------------------------------------------------------------------------
# SPEC 4.6: a source that was not itself time-ordered
# ---------------------------------------------------------------------------

def test_entry_indices_are_real_not_sorted_positions():
    """
    The subtle one, and the reason `MergeSource.to_entries` exists.

    A file whose own timestamps go backwards gets a sort permutation (SPEC 4.6),
    so the merge walks its timestamps in *sorted* position order. If those
    positions leaked out as entry indices, every consumer would read the wrong
    text and cite the wrong line number (D9) -- and the merged order would still
    look perfectly sorted, so nothing would appear wrong.
    """
    # `FileIndex.ts` is always in file order; `ts_sorted` applies the
    # permutation. So the raw array goes in as-is, not pre-sorted.
    raw = np.array([30, 10, 20], dtype=np.int64)
    order = np.argsort(raw, kind="stable")          # -> [1, 2, 0]
    ix = fake_index(raw, order=order)
    assert ix.ts_sorted.tolist() == [10, 20, 30]
    m = merge_of([ix])

    got = m.materialise()
    assert got.ts.tolist() == [10, 20, 30]
    # The entry indices must be the file-order ones, not 0,1,2.
    assert got.entry.tolist() == [1, 2, 0]
    # And they must index the real arrays correctly.
    assert ix.line_no[got.entry].tolist() == [2, 3, 1]


def test_out_of_order_corpus_file_merges_correctly(corpus, manifests):
    """The same property through the real indexer on a real out-of-order file."""
    ix = index_of(corpus / "edge" / "ooo_unique.log")
    assert ix.order is not None, "this corpus file is supposed to be out of order"
    m = merge_of([ix])
    got = m.materialise()
    assert np.all(np.diff(got.ts) >= 0)
    # Every entry appears exactly once, and entry indices are a permutation.
    assert sorted(got.entry.tolist()) == list(range(ix.n))
    # The timestamp at each row must be that entry's own timestamp.
    assert np.array_equal(got.ts, ix.ts[got.entry])


# ---------------------------------------------------------------------------
# D2 / FR-18: skew
# ---------------------------------------------------------------------------

def test_skew_changes_the_merged_order():
    """
    D2. Skew is applied at merge time, so correcting a clock is a checkpoint
    rebuild and not a re-index.
    """
    m = merge_of([fake_index([100, 200]), fake_index([150])])
    assert merged_pairs(m) == [(0, 0), (1, 0), (0, 1)]

    m.set_skew(1, -80)          # source 1 now reads 70
    assert merged_pairs(m) == [(1, 0), (0, 0), (0, 1)]
    assert m.materialise().ts.tolist() == [70, 100, 200]


def test_skew_shifts_time_to_row():
    m = merge_of([fake_index([100]), fake_index([200])])
    assert m.time_to_row(150) == 1
    m.set_skew(1, -100)         # source 1 now reads 100
    assert m.time_to_row(150) == 2


def test_setting_the_same_skew_does_not_rebuild():
    m = merge_of([fake_index([1, 2, 3])])
    before = m.stats.checkpoints
    m.set_skew(0, 0)
    assert m.stats.checkpoints == before


# ---------------------------------------------------------------------------
# FR-9: enabling and disabling
# ---------------------------------------------------------------------------

def test_disabling_a_source_removes_its_rows():
    m = merge_of([fake_index([10, 30]), fake_index([20])])
    assert m.total == 3
    m.set_enabled(1, False)
    assert m.total == 2
    assert merged_pairs(m) == [(0, 0), (0, 1)]
    assert m.materialise().ts.tolist() == [10, 30]


def test_re_enabling_restores_the_rows_without_reindexing():
    """SPEC 6.5: the FileIndex is untouched, so this is just another rebuild."""
    ix = [fake_index([10, 30]), fake_index([20])]
    m = merge_of(ix)
    m.set_enabled(1, False)
    m.set_enabled(1, True)
    assert m.total == 3
    assert merged_pairs(m) == [(0, 0), (1, 0), (0, 1)]
    assert ix[1].n == 1, "the index must not have been rebuilt"


def test_disabling_every_source_is_an_empty_view_not_an_error():
    m = merge_of([fake_index([1, 2])])
    m.set_enabled(0, False)
    assert m.total == 0
    assert len(m.rows(0, 10)) == 0
    assert m.time_to_row(1) == 0
    with pytest.raises(IndexError):
        m.row_at(0)


# ---------------------------------------------------------------------------
# the block cache (NFR-5)
# ---------------------------------------------------------------------------

def test_sequential_scrolling_mostly_hits_the_cache():
    """
    SPEC 6.2. A scroll paints ~60 rows at a time; without whole-block caching
    each paint would replay up to 4096 rows, which is the difference between a
    smooth scroll and a visible stutter.
    """
    m = merge_of([fake_index(np.arange(0, 60_000, 2)),
                  fake_index(np.arange(1, 60_000, 2))])
    for start in range(0, 12_000, 60):
        m.rows(start, 60)
    assert m.stats.hit_rate > 0.9, f"hit rate only {m.stats.hit_rate:.0%}"


def test_the_cache_is_bounded():
    m = MergeIndex([MergeSource(fake_index(np.arange(200_000)))], cache_blocks=4)
    for start in range(0, 200_000, CHECKPOINT):
        m.rows(start, 10)
    assert len(m._cache) <= 4


def test_a_range_spanning_several_blocks_is_contiguous():
    m = MergeIndex([MergeSource(fake_index(np.arange(0, 20_000, 2))),
                    MergeSource(fake_index(np.arange(1, 20_000, 2)))],
                   checkpoint=512)
    whole = m.materialise()
    got = m.rows(500, 2000)      # crosses four block boundaries
    assert len(got) == 2000
    assert np.array_equal(got.ts, whole.ts[500:2500])
    assert np.array_equal(got.entry, whole.entry[500:2500])


def test_rows_clamps_rather_than_raising():
    m = merge_of([fake_index([1, 2, 3])])
    assert len(m.rows(2, 100)) == 1
    assert len(m.rows(99, 10)) == 0
    assert len(m.rows(-5, 2)) == 2


# ---------------------------------------------------------------------------
# NFR-7: the memory claim
# ---------------------------------------------------------------------------

def test_checkpoints_cost_far_less_than_materialising():
    """
    SPEC 6.1's whole justification. Materialising costs 8 bytes a row; if the
    saving is not enormous, this class should not exist.
    """
    rows = 2_000_000
    m = merge_of([fake_index(np.arange(0, rows, 2)), fake_index(np.arange(1, rows, 2))])
    assert m.total == rows

    materialised = rows * 8
    ratio = materialised / m.checkpoint_bytes
    assert ratio > 100, f"only {ratio:.0f}x smaller than materialising"
    assert m.stats.mb_per_1m_rows < 0.05, f"{m.stats.mb_per_1m_rows:.4f} MB per 1M rows"


def test_an_empty_source_set_is_harmless():
    m = merge_of([])
    assert m.total == 0
    assert m.time_to_row(0) == 0
    assert len(m.materialise()) == 0


def test_a_source_with_no_entries_is_skipped():
    m = merge_of([fake_index([]), fake_index([5, 6])])
    assert m.total == 2
    assert merged_pairs(m) == [(1, 0), (1, 1)]
