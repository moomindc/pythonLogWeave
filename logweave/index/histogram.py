"""
The density histogram -- SPEC 8.1, FR-32, FR-33, risk R3.

One structure feeds **both** overview strips. That is the whole of the R3
mitigation: the timeline and the minimap are two ways of reading the same
precomputed array, so adding the second strip costs no second indexing pass and
no second cache entry.

## The shape

    t0         int     wall_us of the first bucket's lower edge
    bucket_us  int     bucket width, a whole number of seconds
    counts     uint32[buckets, 8]   entries per bucket per severity class

`counts` is built once, during indexing, from arrays the indexer already has in
hand. A repaint never touches it directly -- it reads `cum`, the prefix sum
below.

## Why the prefix sum exists

A strip `H` pixels tall needs, per pixel, the counts over that pixel's time
range: a range-sum over a contiguous run of buckets. Done from `counts` that is
a `cumsum` per repaint, and M0 measured that at **7.4 ms** over a million
buckets -- half a 60fps frame (NFR-5) spent re-deriving numbers that had not
changed since the file was indexed.

With the prefix sum prepared, the same repaint is **two fancy-index reads and a
subtraction**: 74 us for 800 pixels, measured. So `cum` is built once per
histogram and rebuilt only when the histogram itself changes, which is on
append and nowhere else.

It is built lazily rather than in the constructor, because a source that is
indexed and immediately written to a sidecar never needs it, and it is twice
the size of the counts it is derived from.

## The bucket count, and where SPEC 8.1 was wrong

SPEC 8.1 specified `bucket_us = max(1s, span_us // 1s)`, capped at a million
buckets, and in the same breath claimed the cost was "a few MB per source".
Those two statements are not compatible. A million buckets is

    counts  1e6 x 8 x 4 B  =  32 MB
    cum     1e6 x 8 x 8 B  =  64 MB

which is 96 MB **per source**, and at eight sources is 770 MB against NFR-7's
500 MB budget for everything. The cap is therefore `MAX_BUCKETS = 65536`, which
costs 2 MB of counts and 4 MB of prefix sum -- "a few MB per source", as
claimed -- and SPEC 8.1 has been corrected to match.

Nothing is lost by it. The buckets exist to be aggregated onto pixels, and
65536 is still 32x the height of a 2000-pixel strip, so every pixel on the
tallest display anyone has covers at least 32 buckets. The resolution that was
being paid for could not be seen.

## What a bucket boundary costs in accuracy

A pixel's time range is snapped outward to bucket boundaries, so a pixel can
report an entry that belongs to its neighbour. At the resolutions above that is
a sub-pixel error on a strip whose job is to show where the activity is, and
the alternative -- a binary search into every source's `ts` per pixel per
repaint -- is the 8 ms repaint this structure exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .severity import Severity

CLASSES = len(Severity)
"""Eight, fixed by D11: `flags` bits 0-2 hold the class and the palette has eight."""

BUCKET_FLOOR_US = 1_000_000
"""
Finest bucket, one second (SPEC 8.1).

Finer than this is not useful on a strip and is not free: a log that bursts
within a second shows as one dense pixel either way, and the bucket count is
what the memory bill is written against.
"""

MAX_BUCKETS = 65_536
"""
Coarsest resolution the histogram is allowed to keep. See the module docstring
for the arithmetic -- this is the number that holds a source to a few MB.
"""


def bucket_us_for(span_us: int) -> int:
    """
    Bucket width for a span: at least a second, and never more than
    `MAX_BUCKETS` buckets.

    Rounded up to a whole second, which costs nothing and buys two things: the
    timeline's boundaries fall where a reader expects them, and two sources of
    a similar span land on the *same* grid, so a merged strip is summing
    aligned arrays rather than nearly-aligned ones.
    """
    span_us = max(0, int(span_us))
    need = -(-(span_us + 1) // MAX_BUCKETS)        # ceil
    bucket = max(BUCKET_FLOOR_US, need)
    return -(-bucket // BUCKET_FLOOR_US) * BUCKET_FLOOR_US


@dataclass
class Histogram:
    """
    Entry counts per time bucket per severity class, plus a prepared prefix sum.

    Qt-free and picklable, so it survives the worker-process boundary (SPEC 5.3)
    and a round trip through the sidecar.
    """

    t0: int = 0
    """Lower edge of bucket 0, in `wall_us`. A multiple of `bucket_us`."""

    bucket_us: int = BUCKET_FLOOR_US
    counts: np.ndarray = field(
        default_factory=lambda: np.zeros((0, CLASSES), dtype=np.uint32))

    _cum: np.ndarray | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------
    # building
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, ts: np.ndarray, severity: np.ndarray) -> Histogram:
        """
        Histogram `ts` (wall_us, any order) by `severity`.

        One `bincount` over `bucket * 8 + class` rather than eight passes or an
        `np.add.at`: M0 measured this form at 50.8 M entries/s and `np.add.at`
        at a fraction of it, which is the difference between folding this into
        the indexing pass and it being a visible cost.

        `ts` need not be sorted -- only its extremes are used to place the grid
        -- so this works equally on a file that needed a sort permutation
        (SPEC 4.6).
        """
        ts = np.asarray(ts)
        if ts.size == 0:
            return cls()

        lo = int(ts.min())
        hi = int(ts.max())
        bucket = bucket_us_for(hi - lo)
        t0 = (lo // bucket) * bucket
        nbuckets = (hi - t0) // bucket + 1

        b = (ts.astype(np.int64) - t0) // bucket
        np.clip(b, 0, nbuckets - 1, out=b)
        sev = np.asarray(severity, dtype=np.int64) & (CLASSES - 1)
        flat = np.bincount(b * CLASSES + sev, minlength=nbuckets * CLASSES)
        counts = flat[: nbuckets * CLASSES].reshape(nbuckets, CLASSES)
        return cls(t0=int(t0), bucket_us=int(bucket),
                   counts=counts.astype(np.uint32))

    @property
    def cum(self) -> np.ndarray:
        """
        `(buckets + 1, 8)` prefix sum, with a zero row in front.

        int64, not uint32: this accumulates to the entry count of the whole
        source, and the leading zero row is what makes `cum[j] - cum[i]` the
        count over buckets `[i, j)` with no special case at zero.
        """
        if self._cum is None:
            nb, k = self.counts.shape
            cum = np.zeros((nb + 1, k), dtype=np.int64)
            np.cumsum(self.counts, axis=0, dtype=np.int64, out=cum[1:])
            self._cum = cum
        return self._cum

    def invalidate(self) -> None:
        """Drop the prefix sum after `counts` has changed (an append)."""
        self._cum = None

    # ------------------------------------------------------------------
    # following (M6, SPEC 7.3)
    # ------------------------------------------------------------------

    def extend(self, ts: np.ndarray, severity: np.ndarray) -> None:
        """
        Add appended entries, in place, without re-reading the ones already here.

        **This is what makes follow affordable.** `build` is O(entries), which
        is fine once per file and ruinous five times a second: a 10 M-entry log
        being tailed would spend a quarter of a second per tick recounting
        entries that have not moved. This is O(appended) plus, occasionally,
        O(buckets) to reshape the grid.

        Three things can happen to the grid, and none of them needs the
        original timestamps:

        * the new entries fit as they are -- just count them in;
        * they run past the end, or (an out-of-order writer) before the start
          -- the counts are padded with zeros at that end;
        * padding would take the grid past `MAX_BUCKETS` -- the existing
          histogram is **coarsened** by summing whole groups of buckets, which
          is exact, because a wider bucket that is a multiple of the current
          one is just these buckets added together.

        That last case is why `bucket_us` is always a whole number of seconds
        and why the new width is forced to a multiple of the old. Choosing a
        width that was not a multiple would need the entries back to recount
        them, and they are a memory map of a file that may be 10 GB.

        The price of that restriction: a histogram grown by appending can end
        up a little coarser than the same entries built in one pass -- 8 s
        buckets where `build` would have chosen 7 s, measured. Counts stay
        exact either way; it is resolution that is given up, below the floor at
        which any strip can show it (65536 buckets against at most ~2000
        pixels). Reopening the file rebuilds at the finer width.
        """
        ts = np.asarray(ts, dtype=np.int64)
        if ts.size == 0:
            return
        if not self.nbuckets:
            fresh = Histogram.build(ts, severity)
            self.t0, self.bucket_us, self.counts = (
                fresh.t0, fresh.bucket_us, fresh.counts)
            self._cum = None
            return

        lo, hi = int(ts.min()), int(ts.max())
        self._regrid(lo, hi)
        self._make_writeable()

        b = (ts - self.t0) // self.bucket_us
        np.clip(b, 0, self.nbuckets - 1, out=b)
        first, last = int(b.min()), int(b.max())
        reach = last - first + 1

        # Count into the touched buckets only. Counting into the whole grid
        # would be O(buckets) -- measured at 3.3 ms on a day of log at one
        # second a bucket, which is a fifth of a frame spent per follow tick on
        # buckets that did not change. Against the touched range it is 25 us.
        sev = np.asarray(severity, dtype=np.int64) & (CLASSES - 1)
        add = np.bincount((b - first) * CLASSES + sev, minlength=reach * CLASSES)
        self.counts[first : last + 1] += add.reshape(reach, CLASSES).astype(np.uint32)
        self._cum = None

    def _make_writeable(self) -> None:
        """
        Copy the counts if they are still the sidecar's memory map.

        A cached index hands out `np.memmap(..., mode="r")`, which is the whole
        point of the sidecar -- opening a 10 GB set must not read it (NFR-4).
        The first append to such an index is the moment that stops being free,
        and it costs one copy of a couple of MB, once.
        """
        if not self.counts.flags.writeable or not self.counts.flags.owndata:
            self.counts = np.array(self.counts, dtype=np.uint32, copy=True)

    def _regrid(self, lo: int, hi: int) -> None:
        """Widen the grid so `[lo, hi]` falls inside it, coarsening if need be."""
        bucket = self.bucket_us
        new_t0 = min(self.t0, (lo // bucket) * bucket)
        end = max(self.t0 + self.nbuckets * bucket, ((hi // bucket) + 1) * bucket)
        need = (end - new_t0) // bucket

        while need > MAX_BUCKETS:
            # Coarsen by an integer factor, then re-measure. Re-measuring
            # rather than trusting the factor matters: re-anchoring `t0` to the
            # wider bucket can add a bucket at each end, so a factor that was
            # exactly enough can come out one or two over. A second pass then
            # doubles again and settles it -- and `factor` is at least 2, so
            # this cannot spin.
            self._coarsen(-(-need // MAX_BUCKETS))
            bucket = self.bucket_us
            new_t0 = min(self.t0, (lo // bucket) * bucket)
            end = max(self.t0 + self.nbuckets * bucket,
                      ((hi // bucket) + 1) * bucket)
            need = (end - new_t0) // bucket

        before = (self.t0 - new_t0) // bucket
        after = need - before - self.nbuckets
        if before or after:
            self.counts = np.pad(np.asarray(self.counts),
                                 ((int(before), int(max(0, after))), (0, 0)))
            self.t0 = int(new_t0)

    def _coarsen(self, factor: int) -> None:
        """
        Merge every `factor` buckets into one. Exact, and needs no entries.

        The grid is re-anchored first, because the new width is a multiple of
        the old but `t0` need not be a multiple of the *new* width -- and a
        reshape that silently started mid-group would shift every count by part
        of a bucket.
        """
        factor = max(2, int(factor))
        wide = self.bucket_us * factor
        counts = np.asarray(self.counts)

        anchored = (self.t0 // wide) * wide
        lead = (self.t0 - anchored) // self.bucket_us
        tail = -(len(counts) + lead) % factor
        if lead or tail:
            counts = np.pad(counts, ((int(lead), int(tail)), (0, 0)))

        self.counts = counts.reshape(-1, factor, CLASSES).sum(axis=1).astype(np.uint32)
        self.t0 = int(anchored)
        self.bucket_us = int(wide)
        self._cum = None

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    @property
    def nbuckets(self) -> int:
        return int(self.counts.shape[0])

    @property
    def total(self) -> int:
        return int(self.counts.sum())

    def span(self) -> tuple[int, int]:
        """Time range the grid covers -- the *buckets*, not the entries."""
        if not self.nbuckets:
            return (0, 0)
        return (self.t0, self.t0 + self.nbuckets * self.bucket_us)

    def nbytes(self) -> int:
        return int(self.counts.nbytes
                   + (0 if self._cum is None else self._cum.nbytes))

    def bucket_of(self, times: np.ndarray) -> np.ndarray:
        """
        Bucket index for each wall time, clamped into `[0, buckets]`.

        Clamped to `buckets` rather than `buckets - 1` because these are used as
        *prefix-sum* indices, where `buckets` is the valid one-past-the-end
        position. A time past the source's last entry must select everything,
        not everything bar the final bucket.
        """
        if not self.nbuckets:
            return np.zeros(np.shape(times), dtype=np.int64)
        b = (np.asarray(times, dtype=np.int64) - self.t0) // self.bucket_us
        return np.clip(b, 0, self.nbuckets)

    def range_counts(self, edges: np.ndarray) -> np.ndarray:
        """
        Counts per severity for each of the `len(edges) - 1` time ranges.

        `edges` is ascending `wall_us`, in this histogram's own time base -- a
        caller correcting for skew (D2) subtracts it before calling. The result
        is `(len(edges) - 1, 8)` int64.

        This is the repaint. Two fancy-index reads and a subtraction, O(pixels),
        and entirely independent of how many entries the source holds.
        """
        e = self.bucket_of(edges)
        if not self.nbuckets:
            return np.zeros((max(0, e.size - 1), CLASSES), dtype=np.int64)
        cum = self.cum
        return cum[e[1:]] - cum[e[:-1]]


def histogram_of(ts: np.ndarray, severity: np.ndarray) -> Histogram:
    """Convenience alias used by the indexer and the benchmarks."""
    return Histogram.build(ts, severity)
