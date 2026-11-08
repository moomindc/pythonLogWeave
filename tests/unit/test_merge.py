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
from logweave.merge.mergeindex import ROW_TIME_ANCHORS

US = 1_000_000


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


# ---------------------------------------------------------------------------
# row-to-time anchors, for the minimap (SPEC 8.3, M5)
# ---------------------------------------------------------------------------

def test_row_times_is_exact_at_every_anchor():
    """
    The anchors are read straight out of the merge while it is being walked, so
    there is no reason for any of them to be approximate -- and the minimap's
    accuracy is bounded by them being right.
    """
    m = merge_of([fake_index(np.arange(0, 20_000, 2) * 1_000_000),
                  fake_index(np.arange(1, 20_000, 2) * 1_000_000)])
    exact = m.materialise().ts
    rows = m._anchor_row
    assert rows.size > 0
    assert np.array_equal(m._anchor_ts, exact[rows])


def test_row_times_is_exact_everywhere_for_a_small_merge():
    """
    Below the anchor bound the stride is one, so the table holds every row and
    there is nothing to interpolate. Small and medium merges are the common
    case, and they are the ones checkpoint interpolation got visibly wrong.
    """
    m = merge_of([fake_index(np.arange(1000) * 1_000_000)])
    exact = m.materialise().ts
    assert np.array_equal(m.row_times(np.arange(1000)), exact)


def test_row_times_places_an_idle_gap_where_it_actually_is():
    """
    The defect that put the anchor table in. With a gap inside one checkpoint
    block, interpolating between checkpoints smears it over the surrounding
    rows: the minimap showed ten blank pixels in the middle of a 7000-row merge
    and a thousand rows piled into the last pixel, neither of which was real.

    The gap here is deliberately inside a single 4096-row block.
    """
    ts = np.concatenate([
        np.arange(0, 1000) * 1_000_000,
        np.arange(0, 1000) * 1_000_000 + 10 * 3600 * 1_000_000,
    ])
    m = merge_of([fake_index(ts)])
    got = m.row_times(np.array([0, 999, 1000, 1999]))
    exact = m.materialise().ts
    assert got[0] == exact[0]
    assert got[1] == exact[999]
    assert got[2] == exact[1000]
    assert got[3] == exact[1999]
    # The jump belongs between rows 999 and 1000 and nowhere else.
    steps = np.diff(m.row_times(np.arange(2000)))
    assert int(np.argmax(steps)) == 999


def test_the_anchor_table_is_a_fixed_size_whatever_the_merge():
    """
    The reason the anchors exist at all rather than one per `n` rows: at
    NFR-15's two-billion-row ceiling that would be 64 MB against NFR-7's
    500 MB for everything, and this is 64 KB.
    """
    small = merge_of([fake_index(np.arange(5_000) * 1_000_000)])
    large = merge_of([fake_index(np.arange(0, 2_000_000, 2) * 1_000_000),
                      fake_index(np.arange(1, 2_000_000, 2) * 1_000_000)])
    assert large.total == 2_000_000
    assert large.stats.anchor_bytes <= 64 * 1024
    assert large.stats.anchor_bytes <= small.stats.anchor_bytes * 2


def test_the_anchors_are_not_counted_as_checkpoint_bytes():
    """
    SPEC 6.2's 409x memory claim is written against `checkpoint_bytes` and is
    asserted above. The anchor table is a fixed cost that does not scale with
    rows, so folding it in would make that ratio depend on the merge's size.
    """
    m = merge_of([fake_index(np.arange(2_000_000) * 1_000_000)])
    assert m.stats.anchor_bytes > 0
    assert m.checkpoint_bytes == (m._cp_row.nbytes + m._cp_ts.nbytes
                                 + m._cp_cursors.nbytes)


def test_row_times_is_monotonic_and_clamped():
    m = merge_of([fake_index(np.arange(10_000) * 1_000_000)])
    got = m.row_times(np.array([-100, 0, 5000, 9999, 10_000, 99_999_999]))
    assert (np.diff(got) >= 0).all()
    assert got[0] == got[1]
    assert got[-1] == got[-2] == got[3]


def test_row_times_on_an_empty_merge_is_zeros():
    m = merge_of([])
    assert not m.row_times(np.arange(5)).any()


def test_row_times_on_a_single_row_merge_is_that_row():
    m = merge_of([fake_index([1234])])
    assert m.row_times(np.arange(4)).tolist() == [1234] * 4


def test_row_times_error_is_bounded_by_the_anchor_stride():
    """
    Between anchors the rate is assumed constant, which is what makes this
    cheap. The error that buys is what matters: against an 800-pixel strip,
    every pixel holds about five anchors, so a row's placement is good to a
    fifth of a pixel.
    """
    rng = np.random.default_rng(5)
    ts = np.sort(rng.integers(0, 10**12, size=200_000)).astype(np.int64)
    m = merge_of([fake_index(ts)])
    rows = np.linspace(0, m.total - 1, 801).astype(np.int64)
    got = m.row_times(rows)
    want = ts[rows]
    span = int(ts[-1] - ts[0])
    # Each strip pixel covers 250 rows; the anchors are 49 rows apart.
    assert np.abs(got - want).max() < span / 800


# ---------------------------------------------------------------------------
# SPEC 6.4: incremental checkpoints under follow (M6)
# ---------------------------------------------------------------------------
#
# The standard of proof here is identity, not similarity. A merge that was
# followed must end up with the *same* checkpoints, cursors, anchors and rows
# as the same content opened cold -- otherwise someone who tailed a file sees a
# different log from someone who opened it afterwards, and no test of "looks
# about right" would catch it.


def grown(ix: FileIndex, more, *, reorder: bool = False) -> FileIndex:
    """Append timestamps to a fake index in place, as the indexer would."""
    ix.ts = np.concatenate([ix.ts, np.asarray(more, dtype=np.int64)])
    n = ix.ts.size
    ix.off = np.arange(n + 1, dtype=np.uint64) * 100
    ix.flags = np.zeros(n, dtype=np.uint8)
    ix.line_no = np.arange(n, dtype=np.uint32) + 1
    if reorder:
        ix.order = np.argsort(ix.ts, kind="stable").astype(np.uint64)
    return ix


def rebuilt_like(m: MergeIndex) -> MergeIndex:
    """A cold merge over the same sources' current content."""
    srcs = []
    for s in m.sources:
        ix = fake_index(s.index.ts, source_id=s.index.source_id)
        ix.order = s.index.order
        srcs.append(MergeSource(ix, skew_us=s.skew_us, enabled=s.enabled,
                                name=s.name))
    return MergeIndex(srcs)


def assert_identical(a: MergeIndex, b: MergeIndex) -> None:
    assert a.total == b.total
    assert np.array_equal(a._cp_row, b._cp_row)
    assert np.array_equal(a._cp_ts, b._cp_ts)
    assert np.array_equal(a._cp_cursors, b._cp_cursors)
    assert merged_pairs(a) == merged_pairs(b)


def test_a_tail_append_lands_identically_to_a_cold_rebuild():
    a = fake_index(np.arange(5000, dtype=np.int64) * US, source_id="a")
    b = fake_index(np.arange(5000, dtype=np.int64) * US + US // 2, source_id="b")
    m = MergeIndex([MergeSource(a, name="a"), MergeSource(b, name="b")])

    grown(a, np.arange(5000, 5100, dtype=np.int64) * US)
    grown(b, np.arange(5000, 5050, dtype=np.int64) * US + US // 2)
    m.extend_checkpoints()

    assert_identical(m, rebuilt_like(m))
    assert m.total == 10_150


def test_extending_resumes_near_the_tail_rather_than_at_the_start():
    """
    The whole point. An append may only disturb rows it sorts before, so the
    merge resumes from the last checkpoint below the earliest appended
    timestamp -- which for an ordinary tail is within one checkpoint of the end.
    """
    a = fake_index(np.arange(40_000, dtype=np.int64) * US, source_id="a")
    m = MergeIndex([MergeSource(a, name="a")])
    grown(a, np.arange(40_000, 40_010, dtype=np.int64) * US)

    from_row = m.extend_checkpoints()

    assert from_row > 0
    assert m.total - from_row <= CHECKPOINT + 10


def test_extending_reports_nothing_changed_when_no_source_grew():
    a = fake_index(np.arange(100, dtype=np.int64) * US, source_id="a")
    m = MergeIndex([MergeSource(a, name="a")])
    cps = m._cp_row.copy()
    assert m.extend_checkpoints() == m.total    # == total means "no rows moved"
    assert np.array_equal(m._cp_row, cps)


def test_an_append_that_sorts_backwards_rebuilds_the_rows_it_overtakes():
    """
    A writer whose clock is behind appends entries that belong *before* rows
    already shown. Resuming at the tail would merge them in at the wrong place,
    and the merged view would disagree with the same files opened cold.
    """
    a = fake_index(np.arange(20_000, dtype=np.int64) * US, source_id="a")
    b = fake_index(np.arange(20_000, dtype=np.int64) * US + US // 2, source_id="b")
    m = MergeIndex([MergeSource(a, name="a"), MergeSource(b, name="b")])

    # 3000 s back, which is ~6000 merged rows -- well behind the last checkpoint.
    grown(a, [17_000 * US + 1], reorder=True)
    from_row = m.extend_checkpoints()

    assert from_row < m.total - CHECKPOINT      # went back further than the tail
    assert_identical(m, rebuilt_like(m))


def test_the_earliest_appended_entry_is_found_through_raw_entry_order():
    """
    Regression. The appended entries are entry indices `[seen, n)`, which is a
    slice of the index's raw `ts` but *not* of the merge's `ts_sorted`: in a
    reordered file (SPEC 4.6) a backdated append shifts every sorted position
    after it along, so `ts_sorted[seen:]` is a handful of old entries and
    reports a timestamp far too high. Resuming from there would merge the
    backdated entry in after rows it belongs before.

    Built so the trap fires: the appended entry is the *earliest* in the file,
    so in sorted order it takes position 0 and `ts_sorted[seen:]` is the tail.
    """
    a = fake_index((np.arange(12_000, dtype=np.int64) + 1000) * US, source_id="a")
    m = MergeIndex([MergeSource(a, name="a")])
    grown(a, [0], reorder=True)

    assert m.extend_checkpoints() == 0          # must redo from the very start
    assert_identical(m, rebuilt_like(m))
    assert m.row_to_time(0) == 0


def test_a_truncated_source_falls_back_to_a_full_rebuild():
    """
    Fewer entries than last time is not an append (SPEC 7.2: truncation or
    rotation). The recorded cursors point into content that is no longer there,
    so there is nothing to resume from and the caller is told so by row 0.
    """
    a = fake_index(np.arange(3000, dtype=np.int64) * US, source_id="a")
    m = MergeIndex([MergeSource(a, name="a")])
    a.ts = a.ts[:1000]
    a.off = np.arange(1001, dtype=np.uint64) * 100
    a.flags = np.zeros(1000, dtype=np.uint8)
    a.line_no = np.arange(1000, dtype=np.uint32) + 1

    assert m.extend_checkpoints() == 0
    assert m.total == 1000
    assert_identical(m, rebuilt_like(m))


def test_growth_in_a_disabled_source_changes_no_rows():
    """FR-9: a source that is off contributes nothing, including when it grows."""
    a = fake_index(np.arange(500, dtype=np.int64) * US, source_id="a")
    b = fake_index(np.arange(500, dtype=np.int64) * US, source_id="b")
    m = MergeIndex([MergeSource(a, name="a"),
                    MergeSource(b, enabled=False, name="b")])
    before = merged_pairs(m)

    grown(b, np.arange(500, 700, dtype=np.int64) * US)
    assert m.extend_checkpoints() == m.total
    assert merged_pairs(m) == before


def test_many_appends_leave_the_checkpoints_exact_and_the_anchors_bounded():
    """
    300 follow ticks. Checkpoints must still match a cold build exactly, and the
    anchor table must not grow with the number of ticks -- the stride coarsens
    as rows accumulate, so anchors kept from earlier ticks are denser than the
    current stride and would otherwise pile up.
    """
    a = fake_index(np.arange(1000, dtype=np.int64) * US, source_id="a")
    m = MergeIndex([MergeSource(a, name="a")])
    for _ in range(300):
        grown(a, int(a.ts[-1]) + US + np.arange(500, dtype=np.int64) * US)
        m.extend_checkpoints()

    cold = rebuilt_like(m)
    assert m.total == 151_000
    assert np.array_equal(m._cp_row, cold._cp_row)
    assert np.array_equal(m._cp_ts, cold._cp_ts)
    assert m.stats.row_time_anchors <= 2 * ROW_TIME_ANCHORS
    # and the anchors kept are still exact, which is what the minimap reads
    probe = np.arange(0, m.total, 997)
    assert np.array_equal(m.row_times(probe), cold.row_times(probe))


def test_extending_drops_only_the_cached_blocks_that_went_stale():
    """
    Blocks are replayed from the checkpoints, so every block from the resume
    point on is stale even where the checkpoints themselves agree. Blocks
    before it are untouched -- dropping those would throw away the scrollback
    on screen and make a follow tick feel like a reload.
    """
    a = fake_index(np.arange(40_000, dtype=np.int64) * US, source_id="a")
    m = MergeIndex([MergeSource(a, name="a")])
    m.rows(0, 100)                       # block 0 cached
    m.rows(39_000, 100)                  # a block near the tail cached
    assert len(m._cache) == 2

    grown(a, [40_000 * US])
    from_row = m.extend_checkpoints()

    assert 0 in m._cache
    assert all(b * m.checkpoint < from_row for b in m._cache)


def test_a_followed_merge_serves_the_same_rows_as_a_cold_one():
    """
    End to end, on three sources with different rates and a skew, appended in
    seventeen uneven ticks: every merged row must match the cold merge.
    """
    rng = np.random.default_rng(4)
    srcs = [fake_index(np.sort(rng.integers(0, 600_000, 2000)).astype(np.int64),
                       source_id=f"s{i}") for i in range(3)]
    m = MergeIndex([MergeSource(srcs[0], name="s0"),
                    MergeSource(srcs[1], skew_us=1500, name="s1"),
                    MergeSource(srcs[2], name="s2")])
    end = 600_000
    for _tick in range(17):
        for ix in srcs:
            n = int(rng.integers(0, 60))
            if n:
                grown(ix, np.sort(rng.integers(end, end + 5000, n)).astype(np.int64))
        end += 5000
        m.extend_checkpoints()

    assert_identical(m, rebuilt_like(m))
