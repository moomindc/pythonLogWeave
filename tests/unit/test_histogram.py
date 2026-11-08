"""
The overview histogram -- SPEC 8.1, M5.

Three things are checked, and the third is the one that would otherwise rot.

*Correctness*: the counts match a per-entry tally computed independently, on
the corpus as well as on synthetic input.

*The prefix sum*: `range_counts` is the repaint path, and it must agree with
summing `counts` directly over the same buckets. They are different code --
one is a subtraction of two cumulative rows, the other a slice and a sum -- so
agreement is evidence, not tautology.

*The memory bound*: the cap on bucket count is the whole reason SPEC 8.1 was
rewritten, so the arithmetic that justifies it is asserted rather than
commented.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import files_of, index_of

from logweave.index.histogram import (
    BUCKET_FLOOR_US,
    CLASSES,
    MAX_BUCKETS,
    Histogram,
    bucket_us_for,
)
from logweave.index.severity import Severity

US = 1_000_000


def test_a_short_span_gets_one_second_buckets():
    assert bucket_us_for(0) == BUCKET_FLOOR_US
    assert bucket_us_for(30 * US) == BUCKET_FLOOR_US
    assert bucket_us_for(MAX_BUCKETS * US - 1) == BUCKET_FLOOR_US


def test_a_long_span_is_coarsened_rather_than_given_more_buckets():
    """
    The cap, which is the point of SPEC 8.1 as rewritten.

    A thirty-day span at one-second resolution would be 2.6M buckets and 125 MB
    of counts and prefix sum per source. It gets coarser buckets instead.
    """
    span = 30 * 24 * 3600 * US
    bucket = bucket_us_for(span)
    assert bucket > BUCKET_FLOOR_US
    assert span // bucket + 1 <= MAX_BUCKETS


@pytest.mark.parametrize("span_us", [
    0, 1, US - 1, US, 12345 * US,
    MAX_BUCKETS * US, 7 * 24 * 3600 * US, 365 * 24 * 3600 * US,
    50 * 365 * 24 * 3600 * US,
])
def test_the_bucket_count_never_exceeds_the_cap(span_us):
    bucket = bucket_us_for(span_us)
    assert bucket % BUCKET_FLOOR_US == 0, "buckets are whole seconds"
    assert span_us // bucket + 1 <= MAX_BUCKETS


def test_the_cap_holds_a_source_to_a_few_megabytes():
    """
    SPEC 8.1's "a few MB per source", asserted.

    The first version of this module took SPEC 8.1's million buckets literally
    and cost 96 MB a source -- 770 MB over eight sources, against NFR-7's
    500 MB for the whole application. The number below is what makes the claim
    in the spec true, so it is a test and not a comment.
    """
    ts = np.array([0, 50 * 365 * 24 * 3600 * US], dtype=np.int64)
    hist = Histogram.build(ts, np.zeros(2, dtype=np.uint8))
    _ = hist.cum                           # force the expensive half
    assert hist.nbytes() < 8 * 1024 * 1024
    assert 8 * hist.nbytes() < 100 * 1024 * 1024


def test_counts_match_a_per_entry_tally():
    rng = np.random.default_rng(7)
    t0 = 1_791_500_000_000_000
    ts = t0 + rng.integers(0, 4000 * US, size=5000).astype(np.int64)
    sev = rng.integers(0, CLASSES, size=5000).astype(np.uint8)

    hist = Histogram.build(ts, sev)
    want = np.zeros((hist.nbuckets, CLASSES), dtype=np.int64)
    for t, s in zip(ts.tolist(), sev.tolist(), strict=True):
        want[(t - hist.t0) // hist.bucket_us, s] += 1
    assert np.array_equal(hist.counts, want)
    assert hist.total == ts.size


def test_every_entry_lands_in_exactly_one_bucket():
    """No entry may be dropped or double-counted -- a strip cannot show either."""
    rng = np.random.default_rng(11)
    ts = rng.integers(-5 * US, 5 * US, size=997).astype(np.int64)
    hist = Histogram.build(ts, np.zeros(997, dtype=np.uint8))
    assert hist.total == 997


def test_the_grid_is_aligned_to_the_bucket_width():
    ts = np.array([1_791_500_000_123_456, 1_791_500_900_000_000], dtype=np.int64)
    hist = Histogram.build(ts, np.zeros(2, dtype=np.uint8))
    assert hist.t0 % hist.bucket_us == 0
    assert hist.t0 <= int(ts.min())


def test_an_empty_input_is_an_empty_histogram():
    hist = Histogram.build(np.empty(0, dtype=np.int64), np.empty(0, dtype=np.uint8))
    assert hist.nbuckets == 0
    assert hist.total == 0
    # The repaint path must survive it rather than raising: a file with no
    # recognisable timestamps still opens (FR-19) and still gets a strip.
    counts = hist.range_counts(np.array([0, 10, 20], dtype=np.int64))
    assert counts.shape == (2, CLASSES)
    assert not counts.any()


def test_the_prefix_sum_is_the_cumulative_counts():
    rng = np.random.default_rng(3)
    ts = 1_000 * US + rng.integers(0, 600 * US, size=2000).astype(np.int64)
    hist = Histogram.build(ts, rng.integers(0, CLASSES, 2000).astype(np.uint8))
    cum = hist.cum
    assert cum.shape == (hist.nbuckets + 1, CLASSES)
    assert not cum[0].any(), "the leading row must be zero"
    assert np.array_equal(cum[1:] - cum[:-1], hist.counts)
    assert cum[-1].sum() == hist.total


def test_the_prefix_sum_is_built_once_and_dropped_on_invalidate():
    hist = Histogram.build(np.arange(100, dtype=np.int64) * US,
                           np.zeros(100, dtype=np.uint8))
    assert hist._cum is None
    first = hist.cum
    assert hist.cum is first, "rebuilt per access would put the cumsum in the frame"
    hist.invalidate()
    assert hist._cum is None


def test_range_counts_agrees_with_summing_the_buckets():
    """
    The repaint against the slow truth.

    `range_counts` subtracts two rows of the prefix sum; this sums the raw
    counts over the same bucket range. Two different pieces of arithmetic, so
    agreement means the prefix sum is right rather than that it is consistent
    with itself.
    """
    rng = np.random.default_rng(19)
    base = 1_791_500_000_000_000
    ts = base + rng.integers(0, 3600 * US, size=8000).astype(np.int64)
    hist = Histogram.build(ts, rng.integers(0, CLASSES, 8000).astype(np.uint8))

    edges = np.linspace(hist.t0, hist.t0 + hist.nbuckets * hist.bucket_us,
                        41).astype(np.int64)
    got = hist.range_counts(edges)
    b = hist.bucket_of(edges)
    for i in range(40):
        want = hist.counts[b[i]:b[i + 1]].sum(axis=0)
        assert np.array_equal(got[i], want)


def test_a_range_past_the_end_selects_everything_and_not_everything_but_one():
    """
    The off-by-one `bucket_of`'s clamp exists to prevent.

    Clamping to `buckets - 1` instead of `buckets` would quietly exclude the
    last bucket from any range that ran off the end -- which every strip's final
    pixel does.
    """
    ts = np.array([0, 10 * US, 3599 * US], dtype=np.int64)
    hist = Histogram.build(ts, np.zeros(3, dtype=np.uint8))
    far = hist.t0 + hist.nbuckets * hist.bucket_us + 10_000 * US
    got = hist.range_counts(np.array([hist.t0 - 10 * US, far], dtype=np.int64))
    assert int(got.sum()) == 3


def test_severity_is_masked_to_the_eight_classes():
    """
    The indexer passes `flags & FLAG_SEVERITY_MASK`, but a caller handing over
    raw flags must not index out of the array -- it must land on the class the
    low three bits name.
    """
    flags = np.array([0b1000_0110, 0b0100_0011], dtype=np.uint8)
    hist = Histogram.build(np.array([0, US], dtype=np.int64), flags)
    assert hist.counts[:, Severity.ERROR].sum() == 1
    assert hist.counts[:, Severity.INFO].sum() == 1


# ---------------------------------------------------------------------------
# against the corpus
# ---------------------------------------------------------------------------

def test_the_indexer_builds_a_histogram_matching_the_index(corpus, manifests):
    """
    Every corpus file's histogram totals its own entries, per class.

    Checked against `index.severity` rather than against the manifest, because
    what is being tested here is that the histogram agrees with the index it
    was built from -- the index's own agreement with ground truth is
    `test_prototypes`' job.
    """
    files = files_of(manifests["basic"])
    for name in files:
        idx = index_of(corpus / "basic" / name)
        if not idx.n:
            assert idx.hist is None
            continue
        assert idx.hist is not None
        want = np.bincount(idx.severity, minlength=CLASSES)
        assert np.array_equal(idx.hist.counts.sum(axis=0), want)
        assert idx.hist.total == idx.n


def test_a_file_with_no_timestamps_has_no_histogram(corpus):
    idx = index_of(corpus / "edge" / "no_timestamps.log")
    assert idx.n == 0
    assert idx.hist is None
    assert idx.overview_bytes() == 0


def test_the_histogram_is_not_counted_in_bytes_per_entry(corpus, manifests):
    """
    SPEC 2.3's 21 bytes an entry is a per-entry figure, and the histogram is a
    fixed cost that does not scale with entries. Folding it into `nbytes` would
    make `bytes_per_entry` meaningless on a small file -- and it is a number the
    corpus tests assert against.
    """
    name = next(iter(files_of(manifests["basic"])))
    idx = index_of(corpus / "basic" / name)
    assert idx.hist is not None
    assert idx.overview_bytes() > 0
    assert idx.bytes_per_entry() < 32
