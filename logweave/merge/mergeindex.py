"""
The checkpointed k-way merge -- SPEC 6, FR-17, D2, D13.

## The claim this rests on

The merged order is *fully determined* by the per-source `ts` arrays and the sort
key `(ts + skew, source_order, entry_index)`. So it does not have to be stored --
only enough state to **resume** the merge at intervals.

That matters because materialising the order costs 8 bytes a row: 16 GB at
NFR-15's two-billion-row ceiling, against NFR-7's 500 MB budget for everything.
A checkpoint every 4096 rows costs `(8 + 8 + 4k)` bytes per checkpoint instead --
~1.7 MB for 100M rows across 8 sources. M0 measured the ratio at **409x**.

## Three operations, three very different costs

* `time_to_row(t)` -- **no replay at all**: one `searchsorted` per source, summed.
  A handful of microseconds (M0: 27us against NFR-6's 50 ms budget). This is why
  the timeline strip can seek anywhere instantly.
* `rows(start, count)` -- replays at most 4096 rows from the preceding
  checkpoint, vectorised as one small `lexsort`. Whole blocks are cached, so
  scrolling amortises this to nothing.
* `build_checkpoints()` -- one pass over all the timestamps. Embarrassingly
  parallel by construction (SPEC 6.3), though this implementation is serial;
  the fan-out lands with the parallel indexer.

D13: time is the only ordering. There is no notion of "file order" beyond
tie-breaking.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

from ..index.fileindex import FileIndex

CHECKPOINT = 4096
"""
Merged rows between checkpoints (SPEC 6.2).

Sets the trade directly: checkpoint memory scales as `1/C`, and the worst-case
replay to reach an arbitrary row scales as `C`. 4096 puts a cold seek at ~1 ms
and 100M rows at ~1.7 MB, which is the right corner for a 16 ms frame budget.
"""

WINDOW_ROWS = 200_000
"""Rows per build window. Large enough to amortise, small enough to stay in cache."""

BLOCK_CACHE_BLOCKS = 64
"""64 blocks x 4096 rows x ~20 B = ~5 MB, per SPEC 6.2."""


@dataclass
class MergeSource:
    """
    One source as the merge sees it: ascending timestamps, plus a skew.

    Wraps a `FileIndex` rather than copying its arrays, and reads `ts_sorted`, so
    a file that was not itself time-ordered (SPEC 4.6) participates correctly
    with no special case here. The price is that positions in `ts` are positions
    in the *sorted* order, so they must be mapped back through the permutation
    before they mean anything to a caller -- `to_entries` does that.
    """

    index: FileIndex
    skew_us: int = 0
    """D2 / FR-18. Applied at merge time, never baked into the index, so changing
    it rebuilds checkpoints and touches nothing else."""
    enabled: bool = True
    name: str = ""

    @property
    def ts(self) -> np.ndarray:
        return self.index.ts_sorted

    @property
    def n(self) -> int:
        return self.index.n

    def to_entries(self, positions: np.ndarray) -> np.ndarray:
        """
        Sorted positions -> real entry indices.

        For the common monotonic file these are the same thing. For a reordered
        one they are not, and a caller handed sorted positions would read the
        wrong text and cite the wrong line number (D9).
        """
        order = self.index.order
        if order is None:
            return positions
        return order[positions].astype(np.int64)


@dataclass
class MergedRows:
    """A contiguous run of merged rows."""

    source: np.ndarray
    """Index into the merge's source list."""
    entry: np.ndarray
    """Entry index within that source, already mapped through its permutation."""
    ts: np.ndarray
    """`wall_us` including skew -- what the view shows and sorts by."""

    def __len__(self) -> int:
        return int(self.ts.size)


@dataclass
class MergeStats:
    rows: int = 0
    sources: int = 0
    checkpoints: int = 0
    checkpoint_bytes: int = 0
    block_hits: int = 0
    block_misses: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def mb_per_1m_rows(self) -> float:
        """
        Checkpoint cost in **megabytes** per million rows, which is the unit
        SPEC 6.2 and the M0 benchmark both quote (measured: 0.0196 MB/1M against
        8 MB/1M materialised). Named for the unit because getting it wrong by a
        factor of a million is otherwise easy and silent.
        """
        return self.checkpoint_bytes / max(1, self.rows) * 1_000_000 / 1e6

    @property
    def hit_rate(self) -> float:
        total = self.block_hits + self.block_misses
        return self.block_hits / total if total else 0.0


class MergeIndex:
    """
    Random access to a merged view of several sources, without storing the order.

    Qt-free (SPEC 1, layer 3). Construct, `build_checkpoints`, then query.
    """

    def __init__(
        self,
        sources: list[MergeSource],
        *,
        checkpoint: int = CHECKPOINT,
        cache_blocks: int = BLOCK_CACHE_BLOCKS,
    ):
        self.sources = sources
        self.checkpoint = checkpoint
        self.stats = MergeStats()
        self._cache: OrderedDict[int, MergedRows] = OrderedDict()
        self._cache_blocks = cache_blocks
        self._cp_row = np.zeros(0, dtype=np.int64)
        self._cp_ts = np.zeros(0, dtype=np.int64)
        self._cp_cursors = np.zeros((0, 0), dtype=np.int64)
        self._built = False
        self.build_checkpoints()

    # ------------------------------------------------------------------
    # the active set (FR-9)
    # ------------------------------------------------------------------

    @property
    def active(self) -> list[int]:
        """Indices of enabled sources. A disabled source leaves the cursor set."""
        return [i for i, s in enumerate(self.sources) if s.enabled and s.n]

    @property
    def total(self) -> int:
        return sum(self.sources[i].n for i in self.active)

    def set_enabled(self, source: int, enabled: bool) -> None:
        """
        FR-9 / SPEC 6.5. The source's `FileIndex` is untouched, so re-enabling is
        just another rebuild and never re-reads the file.
        """
        if self.sources[source].enabled == enabled:
            return
        self.sources[source].enabled = enabled
        self.build_checkpoints()

    def set_skew(self, source: int, skew_us: int) -> None:
        """
        D2 / FR-18. Skew lives here and not in the index precisely so that
        correcting a clock costs a checkpoint rebuild rather than a re-index.
        """
        if self.sources[source].skew_us == skew_us:
            return
        self.sources[source].skew_us = skew_us
        self.build_checkpoints()

    # ------------------------------------------------------------------
    # NFR-6: seek by time, with no replay
    # ------------------------------------------------------------------

    def time_to_row(self, wall_us: int) -> int:
        """
        The first merged row at or after `wall_us`.

        One `searchsorted` per source and a sum -- O(k log n), no replay, no
        block fetch. Exact, not approximate: because every source's timestamps
        are ascending, the count of entries strictly before `t` *is* the row
        number of the first entry at or after it.
        """
        total = 0
        for i in self.active:
            s = self.sources[i]
            total += int(np.searchsorted(s.ts, wall_us - s.skew_us, side="left"))
        return total

    def cursors_at_time(self, wall_us: int) -> np.ndarray:
        k = len(self.sources)
        cur = np.zeros(k, dtype=np.int64)
        for i in self.active:
            s = self.sources[i]
            cur[i] = np.searchsorted(s.ts, wall_us - s.skew_us, side="left")
        return cur

    def row_to_time(self, row: int) -> int:
        block = self.rows(row, 1)
        return int(block.ts[0]) if len(block) else 0

    def time_span(self) -> tuple[int, int]:
        lo, hi = None, None
        for i in self.active:
            s = self.sources[i]
            a, b = int(s.ts[0]) + s.skew_us, int(s.ts[-1]) + s.skew_us
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
        return (lo or 0, hi or 0)

    # ------------------------------------------------------------------
    # the merge window
    # ------------------------------------------------------------------

    def _window(self, cursors: np.ndarray, take: int) -> tuple:
        """
        Merge up to `take` entries from each source, from `cursors`.

        Returns the sorted arrays plus a `bound`: the smallest last-timestamp
        among sources that still have unread entries. Rows at or after `bound`
        may be overtaken by an entry not yet read, so only rows strictly before
        it can be committed. That is what makes a window boundary exact even
        when timestamps repeat.
        """
        segs_ts, segs_s, segs_p = [], [], []
        bound = None
        for i in self.active:
            s = self.sources[i]
            lo = int(cursors[i])
            hi = min(s.n, lo + take)
            if hi <= lo:
                continue
            segs_ts.append(s.ts[lo:hi] + s.skew_us)
            segs_s.append(np.full(hi - lo, i, dtype=np.int64))
            segs_p.append(np.arange(lo, hi, dtype=np.int64))
            if hi < s.n:
                last = int(segs_ts[-1][-1])
                bound = last if bound is None else min(bound, last)

        if not segs_ts:
            empty = np.zeros(0, dtype=np.int64)
            return empty, empty, empty.copy(), None

        ts_all = np.concatenate(segs_ts)
        s_all = np.concatenate(segs_s)
        p_all = np.concatenate(segs_p)
        # FR-17: ties break by source order, then by position within the source.
        # lexsort takes its PRIMARY key last, which is the opposite of how the
        # tuple reads -- getting this backwards silently sorts by entry index.
        # Sorted position is the right secondary key because a stable argsort
        # (SPEC 4.6) preserves original order among equal timestamps, so
        # position order and line-number order agree.
        order = np.lexsort((p_all, s_all, ts_all))
        return ts_all[order], s_all[order], p_all[order], bound

    # ------------------------------------------------------------------
    # SPEC 6.3: building the checkpoints
    # ------------------------------------------------------------------

    def build_checkpoints(self, window: int = WINDOW_ROWS) -> None:
        C = self.checkpoint
        k = len(self.sources)
        cursors = np.zeros(k, dtype=np.int64)
        rows: list[int] = []
        tss: list[int] = []
        curs: list[np.ndarray] = []
        row = 0
        total = self.total
        take = window

        while row < total:
            ts_s, s_s, _p_s, bound = self._window(cursors, take)
            if ts_s.size == 0:
                break
            if bound is None:
                commit = int(ts_s.size)
            else:
                commit = int(np.searchsorted(ts_s, bound, side="left"))
                if commit == 0:
                    # Every row in the window ties with an entry we have not
                    # read: a long run of identical timestamps. Widen and retry
                    # rather than committing something that may be overtaken.
                    take *= 2
                    continue

            # Where each source's rows landed within the committed prefix.
            positions = [np.flatnonzero(s_s[:commit] == i) for i in range(k)]
            first = (-row) % C
            for j in range(first, commit, C):
                taken = np.array(
                    [np.searchsorted(positions[i], j) for i in range(k)],
                    dtype=np.int64,
                )
                rows.append(row + j)
                tss.append(int(ts_s[j]))
                curs.append(cursors + taken)

            cursors = cursors + np.array(
                [positions[i].size for i in range(k)], dtype=np.int64
            )
            row += commit

        self._cp_row = np.asarray(rows, dtype=np.int64)
        self._cp_ts = np.asarray(tss, dtype=np.int64)
        self._cp_cursors = (
            np.vstack(curs) if curs else np.zeros((0, k), dtype=np.int64)
        )
        self._built = True
        self._cache.clear()

        self.stats.rows = total
        self.stats.sources = len(self.active)
        self.stats.checkpoints = int(self._cp_row.size)
        self.stats.checkpoint_bytes = int(
            self._cp_row.nbytes + self._cp_ts.nbytes + self._cp_cursors.nbytes
        )

    @property
    def checkpoint_bytes(self) -> int:
        return self.stats.checkpoint_bytes

    # ------------------------------------------------------------------
    # random access
    # ------------------------------------------------------------------

    def _replay(self, start_row: int, count: int) -> MergedRows:
        c = int(np.searchsorted(self._cp_row, start_row, side="right") - 1)
        c = max(c, 0)
        if self._cp_row.size == 0:
            cursors = np.zeros(len(self.sources), dtype=np.int64)
            skip = start_row
        else:
            cursors = self._cp_cursors[c].copy()
            skip = start_row - int(self._cp_row[c])
        need = skip + count
        ts_s, s_s, p_s, _ = self._window(cursors, need)
        sl = slice(skip, skip + count)
        s_sel, p_sel, ts_sel = s_s[sl], p_s[sl], ts_s[sl]

        # Map sorted positions back to real entry indices, per source.
        entries = np.empty_like(p_sel)
        for i in np.unique(s_sel):
            m = s_sel == i
            entries[m] = self.sources[int(i)].to_entries(p_sel[m])
        return MergedRows(s_sel, entries, ts_sel)

    def _block(self, block_no: int) -> MergedRows:
        cached = self._cache.get(block_no)
        if cached is not None:
            self._cache.move_to_end(block_no)
            self.stats.block_hits += 1
            return cached
        self.stats.block_misses += 1
        start = block_no * self.checkpoint
        got = self._replay(start, min(self.checkpoint, self.total - start))
        self._cache[block_no] = got
        if len(self._cache) > self._cache_blocks:
            self._cache.popitem(last=False)
        return got

    def clear_cache(self) -> None:
        """
        Drop cached blocks, keeping the checkpoints.

        Needed when a source's content changes under follow (SPEC 6.4) -- the
        checkpoints may still be valid while the blocks replayed from them are
        stale -- and it is also the only honest way to measure a cold seek,
        since rebuilding the index instead would measure construction.
        """
        self._cache.clear()

    def rows(self, start_row: int, count: int) -> MergedRows:
        """
        Merged rows `[start_row, start_row + count)`.

        Served from whole cached blocks, so painting 60 consecutive rows while
        scrolling costs one replay per 4096 rows rather than one per paint --
        which is the difference between a smooth scroll and a visibly stuttering
        one (NFR-5).
        """
        if not self._built:
            raise RuntimeError("build_checkpoints() has not run")
        start_row = max(0, int(start_row))
        count = max(0, min(int(count), self.total - start_row))
        if count == 0:
            z = np.zeros(0, dtype=np.int64)
            return MergedRows(z, z.copy(), z.copy())

        first = start_row // self.checkpoint
        last = (start_row + count - 1) // self.checkpoint
        if first == last:
            b = self._block(first)
            off = start_row - first * self.checkpoint
            return MergedRows(
                b.source[off : off + count],
                b.entry[off : off + count],
                b.ts[off : off + count],
            )

        parts = [self._block(n) for n in range(first, last + 1)]
        src = np.concatenate([p.source for p in parts])
        ent = np.concatenate([p.entry for p in parts])
        tss = np.concatenate([p.ts for p in parts])
        off = start_row - first * self.checkpoint
        return MergedRows(src[off : off + count], ent[off : off + count],
                          tss[off : off + count])

    def row_at(self, row: int) -> tuple[int, int, int]:
        """One merged row as `(source, entry, ts)`."""
        got = self.rows(row, 1)
        if len(got) == 0:
            raise IndexError(f"row {row} out of range (total {self.total})")
        return int(got.source[0]), int(got.entry[0]), int(got.ts[0])

    def materialise(self) -> MergedRows:
        """
        The whole merged order at once.

        Only for tests and small inputs -- materialising is the thing this class
        exists to avoid, and at NFR-15's ceiling it would want 16 GB.
        """
        return self._replay(0, self.total)


def merge_of(
    indexes: list[FileIndex],
    *,
    skews: list[int] | None = None,
    names: list[str] | None = None,
) -> MergeIndex:
    """Convenience constructor: `FileIndex` list in, `MergeIndex` out."""
    srcs = [
        MergeSource(
            index=ix,
            skew_us=(skews[i] if skews else 0),
            name=(names[i] if names else ix.source_id),
        )
        for i, ix in enumerate(indexes)
    ]
    return MergeIndex(srcs)
