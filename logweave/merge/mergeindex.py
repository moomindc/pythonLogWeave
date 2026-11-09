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
from .selection import Selection, sorted_mask

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

ROW_TIME_ANCHORS = 4096
"""
Row-to-time anchors recorded during the checkpoint build, for the minimap.

**A correction to SPEC 8.3**, which said the row-proportional strip converts
each pixel's row boundaries to times by "`row_to_time` on each boundary (a
checkpoint lookup)". The checkpoints are too coarse for that, and the failure
is not subtle. They sit 4096 rows apart, so a strip pixel only contains one
when the merge is over ~3.3M rows; below that, times between checkpoints are
interpolated at a constant rate, and a log with a genuine idle gap inside one
checkpoint block has that gap smeared evenly over the rows around it. The first
minimap built this way showed a 7000-row merge as ten blank pixels in the
middle and a solid band at the end -- an idle period the minimap had invented,
and a thousand rows piled into the last pixel.

So the build records a **fixed number** of row-to-time anchors, spread evenly
over the merged rows, instead of relying on the checkpoints' spacing. Fixed is
the point: 4096 anchors is 32 KB whatever the merge's size, where one anchor
per `n` rows would be 64 MB at NFR-15's ceiling. Against an 800-pixel strip
every pixel holds about five anchors, so a row's placement is good to a fifth
of a pixel -- and for any merge under 4096 rows the step is one and the table
is exact.
"""


@dataclass
class MergeSource:
    """
    One source as the merge sees it: ascending timestamps, plus a skew.

    Wraps a `FileIndex` rather than copying its arrays, and reads `ts_sorted`, so
    a file that was not itself time-ordered (SPEC 4.6) participates correctly
    with no special case here. The price is that positions in `ts` are positions
    in the *sorted* order, so they must be mapped back through the permutation
    before they mean anything to a caller -- `to_entries` does that.

    ## Two position spaces, and why the distinction is load-bearing (M7)

    A filtered source (FR-45) presents only the entries a predicate passed, so
    there are two kinds of position and the merge uses both:

    * a **raw** position, into `index.ts_sorted` -- what `to_entries` maps and
      what a byte range is found from;
    * a **view** position, counting only the entries that pass -- what a cursor
      holds and what a merged row number is a sum of.

    They are the same thing when nothing is filtered, which is why every method
    here has a branch that costs nothing in that case. Confusing the two in
    either direction produces a merge that reads plausible text under the wrong
    row number, so the names say which is which and nothing returns a bare
    "position".
    """

    index: FileIndex
    skew_us: int = 0
    """D2 / FR-18. Applied at merge time, never baked into the index, so changing
    it rebuilds checkpoints and touches nothing else."""
    enabled: bool = True
    name: str = ""
    keep: np.ndarray | None = None
    """
    Entry-order mask of the entries a filter passed (FR-45), or None for all.

    Entry order, not sorted order, because that is the space a filter is
    evaluated in -- `flags` and `ts` are entry-ordered arrays -- and because an
    append extends it at the end. `Selection` holds the permuted copy.
    """

    def __post_init__(self) -> None:
        self._sel: Selection | None = None
        self._rank_of_entry: np.ndarray | None = None
        self._build_selection()

    def _build_selection(self) -> None:
        self._sel = (None if self.keep is None
                     else Selection(sorted_mask(self.keep, self.index.order)))

    def set_keep(self, keep: np.ndarray | None) -> None:
        """Replace the filter mask. The only way it may change, so that the
        derived `Selection` can never be left describing a previous one."""
        self.keep = keep
        self._build_selection()

    def set_index(self, index: FileIndex) -> None:
        """Swap in a re-read index -- the follow path (SPEC 6.4, M6)."""
        self.index = index
        self._rank_of_entry = None
        self._build_selection()

    @property
    def selection(self) -> Selection | None:
        return self._sel

    @property
    def ts(self) -> np.ndarray:
        """
        Every ascending timestamp, filter or no filter.

        Deliberately the *unfiltered* array: it is what a raw position indexes
        and what a time is searched in, and a filtered copy of it is exactly the
        8-bytes-a-row allocation `Selection` exists to avoid.
        """
        return self.index.ts_sorted

    @property
    def n(self) -> int:
        """Rows this source contributes -- a **view** count."""
        return self.index.n if self._sel is None else self._sel.count

    @property
    def raw_n(self) -> int:
        """Entries indexed, filtered or not. What growth is detected against."""
        return self.index.n

    def search(self, wall_us: int, *, after: bool = False) -> int:
        """
        View position of the first entry at or after `wall_us` (`after`: strictly
        after). The primitive `time_to_row` sums.
        """
        side = "right" if after else "left"
        raw = int(np.searchsorted(self.ts, wall_us - self.skew_us, side=side))
        return raw if self._sel is None else self._sel.rank(raw)

    def searches(self, wall_us: np.ndarray) -> np.ndarray:
        """
        `search` at many **ascending** times, in one pass over the mask.

        The vectorised form matters only for a filtered source, and it is what
        makes deriving a filtered checkpoint table cheap (SPEC 11.2): tens of
        thousands of times at once instead of a block sum each.
        """
        raw = np.searchsorted(self.ts, np.asarray(wall_us, dtype=np.int64)
                              - self.skew_us, side="left")
        if self._sel is None:
            return raw.astype(np.int64)
        return self._sel.ranks_sorted(raw)

    def view_rank(self, raw_pos: int) -> int:
        """Raw position -> view position."""
        return int(raw_pos) if self._sel is None else self._sel.rank(raw_pos)

    def window(self, lo: int, count: int) -> tuple[np.ndarray, np.ndarray]:
        """
        `count` rows from view position `lo`, as `(raw positions, skewed ts)`.

        Short when the source runs out. The unfiltered path slices rather than
        fancy-indexing, because this is the merge's inner loop and a slice plus
        an add is one pass where a gather is two.
        """
        lo = max(0, int(lo))
        if self._sel is None:
            hi = min(self.index.n, lo + max(0, int(count)))
            if hi <= lo:
                empty = np.zeros(0, dtype=np.int64)
                return empty, empty.copy()
            return (np.arange(lo, hi, dtype=np.int64),
                    self.ts[lo:hi] + self.skew_us)
        pos = self._sel.take(self._sel.position(lo), count)
        return pos, self.ts[pos] + self.skew_us

    def span(self) -> tuple[int, int]:
        """First and last skewed timestamp in the view."""
        if self.n == 0:
            return (0, 0)
        if self._sel is None:
            return (int(self.ts[0]) + self.skew_us,
                    int(self.ts[-1]) + self.skew_us)
        lo = self._sel.position(0)
        hi = self._sel.position(self._sel.count - 1)
        return (int(self.ts[lo]) + self.skew_us,
                int(self.ts[hi]) + self.skew_us)

    def earliest_from(self, entry: int) -> int | None:
        """
        Smallest skewed timestamp among entries `[entry, n)` that are in the
        view -- what an append is resumed from (SPEC 6.4).

        Read from the index's **raw** `ts` and the entry-order mask for the
        reason the follow path already documents: in sorted order the appended
        entries are not a slice at all.
        """
        ts = self.index.ts[entry:]
        if self.keep is not None:
            ts = ts[self.keep[entry:]]
        return int(ts.min()) + self.skew_us if ts.size else None

    def view_ranks(self, raw: np.ndarray) -> np.ndarray:
        """`view_rank` at many **ascending** raw positions."""
        raw = np.asarray(raw, dtype=np.int64)
        return raw if self._sel is None else self._sel.ranks_sorted(raw)

    def sorted_positions(self, entries: np.ndarray) -> np.ndarray:
        """`sorted_position` for many entries, in one gather."""
        entries = np.asarray(entries, dtype=np.int64)
        if self.index.order is None:
            return entries
        self.sorted_position(0)                 # builds the inverse permutation
        return self._rank_of_entry[entries]

    def sorted_position(self, entry: int) -> int:
        """
        Entry index -> raw sorted position: the inverse of `to_entries`.

        Identity for a monotonic file, and for one that needed reordering the
        inverse permutation is built once and kept. Eight bytes an entry, which
        is why it is built on demand rather than at construction -- only
        `row_of` needs it, and only on a file that was out of order.
        """
        order = self.index.order
        if order is None:
            return int(entry)
        if self._rank_of_entry is None or self._rank_of_entry.size != order.size:
            inverse = np.empty(order.size, dtype=np.int64)
            inverse[order] = np.arange(order.size, dtype=np.int64)
            self._rank_of_entry = inverse
        return int(self._rank_of_entry[int(entry)])

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
    row_time_anchors: int = 0
    anchor_bytes: int = 0
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
        derive_from: MergeIndex | None = None,
    ):
        self.sources = sources
        self.checkpoint = checkpoint
        self.stats = MergeStats()
        self._cache: OrderedDict[int, MergedRows] = OrderedDict()
        self._cache_blocks = cache_blocks
        self._cp_row = np.zeros(0, dtype=np.int64)
        self._cp_ts = np.zeros(0, dtype=np.int64)
        self._cp_cursors = np.zeros((0, 0), dtype=np.int64)
        self._anchor_row = np.zeros(0, dtype=np.int64)
        self._anchor_ts = np.zeros(0, dtype=np.int64)
        self._seen = np.zeros(len(sources), dtype=np.int64)
        self._built = False
        if derive_from is None:
            self.build_checkpoints()
        else:
            self.derive_checkpoints(derive_from)

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
        return sum(self.sources[i].search(wall_us) for i in self.active)

    def cursors_at_time(self, wall_us: int) -> np.ndarray:
        k = len(self.sources)
        cur = np.zeros(k, dtype=np.int64)
        for i in self.active:
            cur[i] = self.sources[i].search(wall_us)
        return cur

    def cursors_at_row(self, row: int) -> np.ndarray:
        """
        How many rows each source has contributed by merged row `row`.

        Exact where `cursors_at_time` is not: a run of identical timestamps has
        one time but many rows, and "the first row at that time" is not where
        row 1,000,004 of the run sits. Jumping to the next entry of a given
        severity (FR-42) starts from a row, so it needs the exact frontier --
        with the approximate one it would find a hit it had just come from.

        Costs one block replay at most, like any other row lookup.
        """
        row = max(0, min(int(row), self.total))
        k = len(self.sources)
        if self._cp_row.size == 0:
            base = np.zeros(k, dtype=np.int64)
            skip = row
        else:
            c = max(int(np.searchsorted(self._cp_row, row, side="right")) - 1, 0)
            base = self._cp_cursors[c].copy()
            skip = row - int(self._cp_row[c])
        if skip <= 0:
            return base
        _, s_s, _, _ = self._window(base, skip)
        return base + np.bincount(s_s[:skip], minlength=k).astype(np.int64)

    def row_of(self, source: int, entry: int) -> int:
        """
        The merged row an entry occupies -- the inverse of `row_at`.

        What a search hit needs (FR-44): a scan finds `(source, entry)` pairs,
        and "hit 7 of 1,284" and "jump to it" are both questions about row
        numbers. Doing it by seeking to the hit's *time* would be wrong wherever
        a timestamp repeats, which in a busy log is everywhere.

        Exact, because the merged order is fully determined by the sort key
        (ts, source order, position within the source): entries from a
        lower-numbered source that share this one's timestamp come first, those
        from a higher-numbered source come after, and within this source the
        count is the position itself. One `searchsorted` per source.

        For an entry the filter excluded this answers where it *would* sit --
        the row of the next entry that did pass -- rather than refusing, so a
        hit that a filter has hidden still scrolls somewhere sensible.
        """
        s = self.sources[source]
        pos = s.sorted_position(entry)
        t = int(s.index.ts_sorted[pos]) + s.skew_us
        row = 0
        for j in self.active:
            if j == source:
                row += s.view_rank(pos)
            else:
                row += self.sources[j].search(t, after=j < source)
        return row

    def rows_of(self, sources: np.ndarray, entries: np.ndarray) -> np.ndarray:
        """
        `row_of` for many entries at once -- what a search's hit list needs.

        A find over a 10 GB set can return the 200,000 hits `MAX_HITS` allows,
        and every one of them needs a row number: for "hit 7 of 1,284", for
        next and previous, and for the scroll. One `row_of` per hit is
        `O(hits x sources)` trips into Python and measures in seconds, so the
        same arithmetic is done per *source pair* over whole arrays instead --
        `sources^2` vectorised `searchsorted`s, which is tens of milliseconds
        at that scale and not visible at any realistic one.

        The hits of one source are sorted by position before the pass and put
        back afterwards, because the filtered rank lookup needs ascending input
        (`Selection.ranks_sorted`) and because a monotone time array lets every
        source's `searchsorted` be one call rather than one per hit.
        """
        sources = np.asarray(sources, dtype=np.int64)
        entries = np.asarray(entries, dtype=np.int64)
        out = np.zeros(sources.size, dtype=np.int64)
        for si in np.unique(sources):
            si = int(si)
            s = self.sources[si]
            mine = sources == si
            pos = s.sorted_positions(entries[mine])
            order = np.argsort(pos, kind="stable")
            pos = pos[order]
            t = s.index.ts_sorted[pos].astype(np.int64) + s.skew_us
            acc = s.view_ranks(pos)
            for j in self.active:
                if j == si:
                    continue
                sj = self.sources[j]
                # A tie goes to the lower-numbered source (FR-17), so an entry
                # from a source before this one with the *same* timestamp is
                # already past -- which is the whole content of `side`.
                raw = np.searchsorted(sj.ts, t - sj.skew_us,
                                      side="right" if j < si else "left")
                acc = acc + sj.view_ranks(raw.astype(np.int64))
            back = np.empty_like(acc)
            back[order] = acc
            out[mine] = back
        return out

    def row_to_time(self, row: int) -> int:
        block = self.rows(row, 1)
        return int(block.ts[0]) if len(block) else 0

    def row_times(self, rows: np.ndarray) -> np.ndarray:
        """
        Wall time at each of many merged rows, from the anchor table.

        What the row-proportional minimap needs (SPEC 8.3): a strip `H` pixels
        tall converts `H + 1` row boundaries to times on every
        re-aggregation. Doing that through `row_to_time` would be `H` block
        replays -- 800 of them, at 1.2 ms cold -- so this reads the anchors
        recorded during the checkpoint build and interpolates between them,
        which is one `searchsorted` over at most 4096 values.

        Exact at every anchor, interpolated between them at a constant rate.
        See `ROW_TIME_ANCHORS` for why the anchors and not the checkpoints, and
        what the first version of this got visibly wrong.
        """
        rows = np.asarray(rows, dtype=np.int64)
        total = self.total
        if total == 0:
            return np.zeros(rows.shape, dtype=np.int64)

        clipped = np.clip(rows, 0, total - 1)
        xp, fp = self._anchor_row, self._anchor_ts
        if xp.size == 0:
            return np.full(rows.shape, self.time_span()[0], dtype=np.int64)
        if xp.size == 1:
            return np.full(rows.shape, int(fp[0]), dtype=np.int64)

        # The last anchor is up to `stride` rows short of the end, and without
        # something past it every row beyond would be given that anchor's
        # timestamp -- which is how a thousand rows ended up in one minimap
        # pixel. `time_span` already knows when the merge ends and costs
        # nothing; the guard is because `np.interp` wants `xp` increasing, and
        # a merge whose last row *is* an anchor would otherwise get a duplicate.
        if total - 1 > int(xp[-1]):
            xp = np.append(xp, total - 1)
            fp = np.append(fp, self.time_span()[1])
        return np.interp(clipped, xp, fp).astype(np.int64)

    def time_span(self) -> tuple[int, int]:
        lo, hi = None, None
        for i in self.active:
            a, b = self.sources[i].span()
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
            pos, ts = s.window(lo, take)
            if pos.size == 0:
                continue
            segs_ts.append(ts)
            segs_s.append(np.full(pos.size, i, dtype=np.int64))
            segs_p.append(pos)
            if lo + pos.size < s.n:
                last = int(ts[-1])
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
        """Rebuild from scratch: one pass over every timestamp."""
        k = len(self.sources)
        found = self._scan(0, np.zeros(k, dtype=np.int64), self._stride(), window)
        rows, tss, curs, anchor_rows, anchor_tss = found

        self._cp_row = np.asarray(rows, dtype=np.int64)
        self._cp_ts = np.asarray(tss, dtype=np.int64)
        self._cp_cursors = (
            np.vstack(curs) if curs else np.zeros((0, k), dtype=np.int64)
        )
        self._anchor_row = (np.concatenate(anchor_rows) if anchor_rows
                            else np.zeros(0, dtype=np.int64))
        self._anchor_ts = (np.concatenate(anchor_tss) if anchor_tss
                           else np.zeros(0, dtype=np.int64))
        self._built = True
        self._cache.clear()
        self._seen = self._counts()
        self._record_stats()

    # ------------------------------------------------------------------
    # SPEC 11.2: the same merge over a subset of the entries (FR-45, NFR-9)
    # ------------------------------------------------------------------

    def filtered(self, keeps: list[np.ndarray | None]) -> MergeIndex:
        """
        A merge over only the entries each mask passes, built from this one.

        One mask per source, in entry order, or None for "all of it". The
        result is an ordinary `MergeIndex`: the session hands it to the view in
        place of this one and keeps this one for the moment the filter is
        cleared, which is what makes clearing instant (FR-45).
        """
        if len(keeps) != len(self.sources):
            raise ValueError("one mask per source")
        srcs = [
            MergeSource(index=s.index, skew_us=s.skew_us, enabled=s.enabled,
                        name=s.name, keep=keeps[i])
            for i, s in enumerate(self.sources)
        ]
        return MergeIndex(srcs, checkpoint=self.checkpoint,
                          cache_blocks=self._cache_blocks, derive_from=self)

    def derive_checkpoints(self, parent: MergeIndex) -> None:
        """
        Build this merge's checkpoints out of an unfiltered merge's, with no
        re-merge at all. **This is what meets NFR-9.**

        ## Why a filtered merge cannot simply be built

        `build_checkpoints` is one pass over every timestamp -- measured at
        2.7 Mrow/s (SPEC 14.1), so NFR-9's 100M-row set is **37 seconds**
        against a 500 ms budget. SPEC 11.2 said the vectorised mask pass is
        what meets NFR-9, and that was only ever half the story: the masks are
        indeed a few milliseconds, and the merge built over them was not.

        ## Why the parent's checkpoints transfer

        The filtered merged order is the **subsequence** of the unfiltered
        merged order containing the rows that passed -- both are the same
        entries under the same sort key, and dropping some of them cannot
        reorder the rest. So every unfiltered checkpoint is a cut through the
        filtered order too, and the only thing that changes is the arithmetic
        describing it:

        * its **cursors** become the kept entries before that cut, which is
          `Selection.rank` per source -- and at a checkpoint time that is the
          same thing as `cursors_at_time`, so this is one `searchsorted` and one
          masked pass per source for the *whole* table rather than per
          checkpoint.
        * its **row** is the sum of those cursors, because a merged row number
          is exactly how many rows precede it.

        A cut is a valid resume point whenever `row == sum(cursors)` and the
        cursors describe a prefix of the merge order, which the entries before a
        given time always do. `tests/unit/test_filter.py` holds the derived
        table against a from-scratch build, because a merge whose order depended
        on how it was built would be the worst defect this design can have.

        ## What is given up

        `_cp_ts` is a **lower bound** on the time at its row rather than the
        exact value: if the filter removed every entry at that instant, the row
        that inherits the checkpoint is a later one. Only `extend_checkpoints`
        reads it, and only to pick a checkpoint strictly before an appended
        timestamp, where a lower bound resumes early -- slower, never wrong.
        """
        self._cache.clear()
        if self.total == 0 or parent._cp_row.size == 0:
            # Nothing to place, or a parent too small to have a table. Either
            # way a build is immediate -- and it keeps exactly one definition
            # of what an unfiltered-sized merge's checkpoints are.
            self.build_checkpoints()
            return

        self._cp_ts, self._cp_row, self._cp_cursors = self._transfer(
            parent._cp_ts, want_cursors=True)
        anchor_ts, anchor_row, _ = self._transfer(parent._anchor_ts)
        self._anchor_ts, self._anchor_row = anchor_ts, anchor_row
        self._built = True
        self._seen = self._counts()
        self._record_stats()
        self.stats.notes.append("checkpoints derived from the unfiltered merge")

    def _transfer(self, times: np.ndarray, *, want_cursors: bool = False):
        """
        Parent cut times -> `(times, rows, cursors)` in this merge's numbering.

        Cuts landing on the same filtered row are collapsed to the first of
        them, which is both a memory saving on a selective filter -- where
        thousands of parent checkpoints can map to one row -- and what keeps
        `_cp_row` strictly increasing, as `np.searchsorted` and `np.interp`
        both require of it.
        """
        k = len(self.sources)
        times = np.asarray(times, dtype=np.int64)
        cursors = np.zeros((times.size, k), dtype=np.int64)
        for i in self.active:
            cursors[:, i] = self.sources[i].searches(times)
        rows = cursors.sum(axis=1)

        first = np.ones(rows.size, dtype=bool)
        first[1:] = rows[1:] != rows[:-1]
        times, rows = times[first], rows[first]
        return times, rows, (cursors[first] if want_cursors
                             else np.zeros((0, k), dtype=np.int64))

    def _stride(self) -> int:
        """
        Rows between row-to-time anchors, so their count stays bounded.

        One row per anchor below the bound, which makes the table exact for a
        small merge (see ROW_TIME_ANCHORS).
        """
        return max(1, -(-self.total // ROW_TIME_ANCHORS))

    def _scan(
        self,
        row: int,
        cursors: np.ndarray,
        stride: int,
        window: int,
    ) -> tuple[list[int], list[int], list[np.ndarray], list, list]:
        """
        Merge from `(row, cursors)` to the end, collecting checkpoints and anchors.

        Split out of `build_checkpoints` so `extend_checkpoints` can run the
        same loop from a checkpoint in the middle. There is deliberately only
        one copy of the commit rule -- a second copy that drifted would produce
        a merge whose order depended on whether the file was followed or
        reopened, which is the worst class of bug this design can have.
        """
        C = self.checkpoint
        k = len(self.sources)
        rows: list[int] = []
        tss: list[int] = []
        curs: list[np.ndarray] = []
        anchor_rows: list[np.ndarray] = []
        anchor_tss: list[np.ndarray] = []
        cursors = np.asarray(cursors, dtype=np.int64).copy()
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

            # Row-to-time anchors over the same committed prefix. Read out of
            # `ts_s` while it is in hand, because reconstructing these later
            # would mean replaying the merge a second time.
            local = np.arange((-row) % stride, commit, stride, dtype=np.int64)
            if local.size:
                anchor_rows.append(row + local)
                anchor_tss.append(ts_s[local])

            cursors = cursors + np.array(
                [positions[i].size for i in range(k)], dtype=np.int64
            )
            row += commit

        return rows, tss, curs, anchor_rows, anchor_tss

    # ------------------------------------------------------------------
    # SPEC 6.4: keeping the checkpoints current under follow (M6)
    # ------------------------------------------------------------------

    def extend_checkpoints(self, window: int = WINDOW_ROWS) -> int:
        """
        Fold appended entries into the existing checkpoints. Returns the first
        merged row whose content may have changed, or `self.total` if nothing did.

        ## Why this is not just "rebuild, it is only one pass"

        One pass over every timestamp is the cheapest possible *full* build and
        it is still O(rows): measured at **7.3 Mrow/s**, so a 10 M-row merge
        costs 1.4 s. FR-21 gives appended lines 200 ms to appear, and NFR-8
        wants following to be nearly free, so a rebuild per tick fails both by
        an order of magnitude on exactly the sets people tail.

        ## What actually has to be redone

        The merged order is determined by the timestamps (see the module
        docstring), so an appended entry can only disturb rows it sorts *before*.
        Let `t` be the earliest timestamp among the appended entries. Every row
        before the last checkpoint with `cp_ts < t` has a timestamp below `t`,
        so no appended entry can precede it -- that checkpoint's cursors are
        still exact, and the merge can resume from there.

        For a well-behaved tail that is the **last** checkpoint, and the work is
        the appended rows plus at most `checkpoint` rows of old merge. For a
        source whose clock ran behind, it is further back, and the cost rises in
        proportion to how far out of order the writer is -- which is the right
        shape, because nothing cheaper can be correct.

        A source that *shrank* is not an append at all (truncation or rotation,
        SPEC 7.2); it falls back to a full rebuild, and the caller finds out
        from the returned row being 0.
        """
        if not self._built:
            self.build_checkpoints(window)
            return 0

        now = self._counts()
        if np.array_equal(now, self._seen):
            return self.total
        if np.any(now < self._seen):
            # Truncated or rotated: the entries behind the existing cursors are
            # not the ones they were recorded against.
            self.build_checkpoints(window)
            return 0

        earliest = self._earliest_appended(now)
        if earliest is None:                     # growth only in disabled sources
            self._seen = now
            self._record_stats()
            return self.total

        # Last checkpoint strictly before the earliest appended entry.
        c = int(np.searchsorted(self._cp_ts, earliest, side="left")) - 1
        if c < 0:
            self.build_checkpoints(window)
            return 0

        from_row = int(self._cp_row[c])
        stride = self._stride()
        found = self._scan(from_row, self._cp_cursors[c], stride, window)
        rows, tss, curs, anchor_rows, anchor_tss = found

        self._cp_row = np.concatenate([self._cp_row[:c],
                                       np.asarray(rows, dtype=np.int64)])
        self._cp_ts = np.concatenate([self._cp_ts[:c],
                                      np.asarray(tss, dtype=np.int64)])
        self._cp_cursors = np.vstack(
            [self._cp_cursors[:c]]
            + ([np.vstack(curs)] if curs else
               [np.zeros((0, len(self.sources)), dtype=np.int64)]))

        keep = int(np.searchsorted(self._anchor_row, from_row, side="left"))
        self._anchor_row = np.concatenate(
            [self._anchor_row[:keep]]
            + (anchor_rows or [np.zeros(0, dtype=np.int64)]))
        self._anchor_ts = np.concatenate(
            [self._anchor_ts[:keep]]
            + (anchor_tss or [np.zeros(0, dtype=np.int64)]))
        self._thin_anchors()

        # Blocks are replayed from the checkpoints, so everything from the
        # resume point on is stale even where the checkpoints agree.
        for block in [b for b in self._cache if b * self.checkpoint >= from_row]:
            del self._cache[block]

        self._seen = now
        self._record_stats()
        return from_row

    def _counts(self) -> np.ndarray:
        """
        Entries indexed per source -- **raw** counts, not view counts.

        Growth is a property of the file, not of the filter in front of it: a
        filtered merge whose predicate rejects every appended line has still
        seen those entries, and recording the view count instead would make it
        re-detect the same append on every tick.
        """
        return np.array([s.raw_n for s in self.sources], dtype=np.int64)

    def _earliest_appended(self, now: np.ndarray) -> int | None:
        """
        Smallest skewed timestamp among entries not seen at the last build.

        Read from the index's RAW `ts`, not the merge's `ts_sorted`. Entry
        indices `[seen, n)` are the appended ones by definition, so that slice
        is O(appended). In `ts_sorted` they are not a slice at all: a file that
        was not time-ordered (SPEC 4.6) puts an appended line with an old
        timestamp early in the sorted view, and `ts_sorted[seen:]` would then be
        the *old* entries that got shifted along -- reporting the tail's
        timestamp and resuming far too late. `MergeSource.earliest_from` holds
        that rule, and applies the filter mask over the same slice.
        """
        best: int | None = None
        for i in self.active:
            s = self.sources[i]
            at = int(self._seen[i])
            if at >= int(now[i]):
                continue
            lowest = s.earliest_from(at)
            if lowest is None:
                continue                      # appended, but all of it filtered out
            best = lowest if best is None else min(best, lowest)
        return best

    def _thin_anchors(self) -> None:
        """
        Keep the anchor table bounded across many appends.

        The stride is chosen from the row count, so every append makes it
        coarser -- and the anchors kept from before the resume point were
        recorded at the *old*, finer stride. Left alone, a file followed for an
        hour accumulates them. Dropping every n-th is safe because `row_times`
        interpolates between whatever anchors exist and does not assume they are
        evenly spaced; the ones kept stay exact.
        """
        size = int(self._anchor_row.size)
        if size <= 2 * ROW_TIME_ANCHORS:
            return
        step = -(-size // ROW_TIME_ANCHORS)
        self._anchor_row = self._anchor_row[::step].copy()
        self._anchor_ts = self._anchor_ts[::step].copy()

    def _record_stats(self) -> None:
        self.stats.rows = self.total
        self.stats.sources = len(self.active)
        self.stats.checkpoints = int(self._cp_row.size)
        self.stats.checkpoint_bytes = int(
            self._cp_row.nbytes + self._cp_ts.nbytes + self._cp_cursors.nbytes
        )
        # Counted apart from the checkpoints: `checkpoint_bytes` is the figure
        # SPEC 6.2's 409x memory claim is written against and is asserted in
        # tests, and the anchor table is a fixed 32 KB that does not scale with
        # the row count. Folding it in would make that ratio size-dependent.
        self.stats.row_time_anchors = int(self._anchor_row.size)
        self.stats.anchor_bytes = int(self._anchor_row.nbytes + self._anchor_ts.nbytes)

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
