"""
A subset of one source's entries, addressable by position -- SPEC 11.2, FR-45.

A filter changes row numbering, so the passing rows have to be *addressable*:
the scrollbar needs their count, and the view needs "the 400,000th passing row"
to be a question with a cheap answer. SPEC 11.2 calls the result "a filtered
`MergeIndex` built over masked cursor arrays", and this is the mask and the
arithmetic that turns it into positions.

## Why a mask and not a list of positions

The obvious representation is the kept positions themselves, `flatnonzero(keep)`.
It is 8 bytes a kept entry, and it is wrong at the scale NFR-9 names: a
100M-row set whose filter keeps most rows would want 800 MB for that array
alone, against NFR-7's 500 MB for the whole process. The mask is **one byte an
entry** and does not grow with how much of the set passes -- 100 MB at the same
scale, which is a cost the filter can actually be charged.

What a list gives for free, a mask has to compute:

* **rank** -- how many kept entries lie before a raw position. A full prefix sum
  would be back to 8 bytes a row, so this keeps one **sampled** at every
  `BLOCK`th entry (24k values for 100M entries, 200 KB whatever the size) and
  finishes with a vectorised sum over the ≤4096-entry remainder.
* **position** -- where the i-th kept entry is. One `searchsorted` over the
  sampled prefix, then one `flatnonzero` inside that single block.

Both are O(BLOCK) with no Python loop, and neither allocates anything
proportional to the source.

## The one operation whose cost depends on the filter

`take(pos, count)` -- the next `count` kept positions, which is what a merge
window asks for -- has to look at every entry it skips. For a filter keeping a
tenth of the rows that is ten entries read per row returned; for one keeping a
millionth it is a million. That is the honest cost of not storing the positions,
and it is self-limiting: a filter that sparse has almost no rows to page
through, so the whole of its traversal is one pass over the mask. A dense filter
reads little per row and has many rows. Either way a full scan of a filtered
view costs about one vectorised pass over the mask.

`np.add.reduceat` does the heavy lifting in `ranks_sorted`, and it has a trap
worth stating: **where two consecutive indices are equal it returns the element
at that index, not zero**. Handed a checkpoint table with a repeated position --
which a sparse filter produces constantly -- it silently reports one extra kept
entry per duplicate, and the derived checkpoints then disagree with a rebuilt
merge by a row or two. The positions are deduplicated before the reduction for
that reason alone.
"""

from __future__ import annotations

import numpy as np

BLOCK = 4096
"""
Entries between sampled rank values.

The same number as the merge's checkpoint spacing, for the same reason: it is
the point where the sampled table is small (200 KB at 100M entries) and the
remainder a lookup has to sum is still one short vectorised pass.
"""


class Selection:
    """
    Which of a source's entries pass a filter, in **sorted order**.

    Sorted order, not entry order: every position in this class is a position
    in `FileIndex.ts_sorted`, because that is the space the merge works in. A
    file that was not itself time-ordered (SPEC 4.6) has a different mask in
    each space, and mixing them up would keep the wrong lines -- so the
    caller does the permutation once, up front, and nothing here has to know.
    """

    __slots__ = ("keep", "n", "count", "_prefix")

    def __init__(self, keep: np.ndarray):
        self.keep = np.ascontiguousarray(keep, dtype=np.bool_)
        self.n = int(self.keep.size)
        # Sampled rank, one value per block boundary, inclusive of both ends:
        # `_prefix[b]` is how many entries are kept before `b * BLOCK`. The
        # final element is the total, which is what `count` reads.
        bounds = np.arange(0, self.n, BLOCK, dtype=np.int64)
        if bounds.size:
            per_block = np.add.reduceat(self.keep, bounds).astype(np.int64)
        else:
            per_block = np.zeros(0, dtype=np.int64)
        self._prefix = np.concatenate(
            [np.zeros(1, dtype=np.int64), np.cumsum(per_block)])
        self.count = int(self._prefix[-1])

    # ------------------------------------------------------------------

    @property
    def nbytes(self) -> int:
        return int(self.keep.nbytes + self._prefix.nbytes)

    @property
    def selectivity(self) -> float:
        return self.count / self.n if self.n else 1.0

    def rank(self, pos: int) -> int:
        """Kept entries strictly before raw sorted position `pos`."""
        pos = max(0, min(int(pos), self.n))
        block = pos // BLOCK
        base = int(self._prefix[block])
        start = block * BLOCK
        if pos == start:
            return base
        return base + int(np.count_nonzero(self.keep[start:pos]))

    def ranks_sorted(self, positions: np.ndarray) -> np.ndarray:
        """
        `rank` at many **ascending** positions, in one pass over the mask.

        What deriving a filtered checkpoint table out of an unfiltered one
        needs (SPEC 11.2): tens of thousands of ranks at once, where calling
        `rank` each time would re-sum a block per call. Ascending is required
        rather than checked at cost -- the caller is a checkpoint table, which
        is sorted by construction.
        """
        asked = np.asarray(positions, dtype=np.int64)
        flat = np.clip(asked.ravel(), 0, self.n)
        if flat.size == 0 or self.n == 0:
            return np.zeros(asked.shape, dtype=np.int64)

        # Deduplicated because `reduceat` returns `keep[i]` rather than 0 for a
        # zero-width segment -- see the module docstring. The two ends are
        # excluded as well: 0 and `n` are known without reading anything, and
        # `n` is not a legal `reduceat` index at all.
        uniq, back = np.unique(flat, return_inverse=True)
        out = np.empty(uniq.size, dtype=np.int64)
        inner = (uniq > 0) & (uniq < self.n)
        bounds = np.concatenate([np.zeros(1, dtype=np.int64), uniq[inner]])
        # `reduceat` sums `[bounds[i], bounds[i + 1])` for every index but the
        # last, which runs to the end of the array -- so the first `len(inner)`
        # partial sums are the ranks asked for and the extra one is discarded.
        segments = np.add.reduceat(self.keep, bounds).astype(np.int64)
        out[inner] = np.cumsum(segments)[: int(inner.sum())]
        out[uniq <= 0] = 0
        out[uniq >= self.n] = self.count
        return out[back].reshape(asked.shape)

    def position(self, i: int) -> int:
        """
        Raw sorted position of the `i`-th kept entry; `n` once `i` reaches the
        count, so it can be used as an exclusive end.
        """
        i = int(i)
        if i <= 0:
            return int(np.argmax(self.keep)) if self.count else self.n
        if i >= self.count:
            return self.n
        block = int(np.searchsorted(self._prefix, i, side="right")) - 1
        start = block * BLOCK
        local = i - int(self._prefix[block])
        hits = np.flatnonzero(self.keep[start:start + BLOCK])
        return start + int(hits[local])

    def take(self, pos: int, count: int) -> np.ndarray:
        """
        Raw sorted positions of the next `count` kept entries at or after `pos`.

        Short when the mask runs out, like every other range accessor in the
        engine: a view scrolled to the bottom asks for more rows than exist on
        every frame.
        """
        pos = max(0, min(int(pos), self.n))
        count = int(count)
        if count <= 0 or pos >= self.n:
            return np.zeros(0, dtype=np.int64)
        end = self.position(self.rank(pos) + count)
        return pos + np.flatnonzero(self.keep[pos:end]).astype(np.int64)


def sorted_mask(keep: np.ndarray, order: np.ndarray | None) -> np.ndarray:
    """
    An entry-order mask in sorted order -- the permutation `Selection` wants.

    Returned as-is for the overwhelmingly common monotonic file, so filtering
    one costs no copy at all.
    """
    keep = np.ascontiguousarray(keep, dtype=np.bool_)
    return keep if order is None else keep[order]
