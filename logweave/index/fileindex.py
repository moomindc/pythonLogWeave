"""
`FileIndex` -- the per-source index -- SPEC 2.3, 5.3.

## The shape, and why it is this shape

One entry costs **21 bytes**, in four parallel arrays rather than one array of
structs:

    off      uint64[n+1]   entry start byte offset; off[n] ends the region
    ts       int64[n]      wall_us, before skew (D2 applies skew at merge time)
    flags    uint8[n]      severity, multi-line, inferred, fallback
    line_no  uint32[n]     1-based line within this file (D9)
    order    uint64[n]     stable argsort of ts -- only if non-monotonic

At 21 bytes an entry, a 10 GB log of 100-byte lines costs about 2.1 GB of
index -- which is why the sidecar cache (M2) exists and why nothing bigger is
stored. In particular the entry *text* is never held: it is read back from the
source on demand, which is what `off` is for.

Parallel arrays rather than a struct array because every consumer wants one
column at a time. The merge engine binary-searches `ts` alone; the minimap
histograms `flags` alone. A struct array would drag all 21 bytes through cache
for each, and `searchsorted` would not work on it at all.

`order` is allocated only when the file turns out to be non-monotonic, because
a well-behaved log does not need it and it is the largest array of the five.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .layouts import (
    FLAG_MULTILINE,
    FLAG_SCALAR_FALLBACK,
    FLAG_SEVERITY_MASK,
    FLAG_TS_INFERRED,
)

GROWTH = 2.0
INITIAL_CAPACITY = 4096


class _Growable:
    """
    An append-only typed array with amortised doubling.

    `np.concatenate` per chunk would be O(n^2) over the build; this keeps it
    linear. `view` always returns a zero-copy slice of the live buffer, so
    callers must not hold one across an append.
    """

    __slots__ = ("_buf", "_n")

    def __init__(self, dtype: np.dtype | type, capacity: int = INITIAL_CAPACITY):
        self._buf = np.empty(capacity, dtype=dtype)
        self._n = 0

    def __len__(self) -> int:
        return self._n

    def reserve(self, extra: int) -> None:
        need = self._n + extra
        if need <= self._buf.size:
            return
        cap = max(need, int(self._buf.size * GROWTH) or INITIAL_CAPACITY)
        grown = np.empty(cap, dtype=self._buf.dtype)
        grown[: self._n] = self._buf[: self._n]
        self._buf = grown

    def extend(self, values: np.ndarray) -> None:
        self.reserve(values.size)
        self._buf[self._n : self._n + values.size] = values
        self._n += values.size

    def append(self, value: object) -> None:
        self.reserve(1)
        self._buf[self._n] = value
        self._n += 1

    @property
    def view(self) -> np.ndarray:
        return self._buf[: self._n]


@dataclass
class IndexStats:
    """What the build saw. Surfaced in the UI (FR-19) and asserted in tests."""

    bytes_indexed: int = 0
    lines: int = 0
    entries: int = 0
    multiline_entries: int = 0
    inferred: int = 0
    unparsed_lines: int = 0
    chunks: int = 0
    monotonic: bool = True
    year_rollovers: int = 0
    day_rollovers: int = 0
    used_fast_path: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class FileIndex:
    """
    The index for one source. Qt-free, picklable, and the unit the cache stores.

    Deliberately not tied to a `LogSource`: the indexer holds the source, this
    holds only the result, so an index can be loaded from cache (M2) or sent
    back from a worker process without dragging a file handle along.
    """

    source_id: str = ""
    format_name: str = ""
    fast_path: bool = False
    confidence: float = 0.0
    encoding: str = ""
    stats: IndexStats = field(default_factory=IndexStats)

    message_offset: int = 0
    """
    Characters to skip to get past a line-leading timestamp (SPEC 9.5).

    Zero when there is nothing to skip: no format detected, or the timestamp
    sits inside the line rather than at the front of it, as in an Apache or IIS
    access log. Recorded here because it is a property of the proven layout, and
    the layout does not survive past indexing.

    Characters, not bytes. The two agree for every timestamp in the catalogue
    because they are all ASCII, and the scan buffer a UTF-16 file is narrowed
    into (SPEC 3.3) is one byte per character over that subset.
    """

    off: np.ndarray = field(default_factory=lambda: np.zeros(1, dtype=np.uint64))
    ts: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    flags: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.uint8))
    line_no: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.uint32))
    order: np.ndarray | None = None

    layout_json: dict | None = None
    """
    The proven layout, as plain data, for resuming this index later.

    Carried so that an append -- a reopen of a grown file, or a follow tick --
    indexes the new bytes with the layout the rest of the file was indexed
    with. Re-detecting on a short tail can land on a different member of the
    ISO family, and the two halves of the index would then disagree about where
    the timestamp is (SPEC 5.4). Plain data rather than a `Layout` so it
    survives a round trip through the sidecar and a process boundary.
    """

    severity_offset: int | None = None
    """Fixed column the level word was found at, or None if there was none."""

    @property
    def n(self) -> int:
        return int(self.ts.size)

    @property
    def indexed_to(self) -> int:
        """Byte offset the index is complete to -- where a follow append resumes."""
        return int(self.off[-1]) if self.off.size else 0

    @property
    def severity(self) -> np.ndarray:
        return self.flags & FLAG_SEVERITY_MASK

    @property
    def is_multiline(self) -> np.ndarray:
        return (self.flags & FLAG_MULTILINE) != 0

    @property
    def ts_inferred(self) -> np.ndarray:
        return (self.flags & FLAG_TS_INFERRED) != 0

    @property
    def used_fallback(self) -> np.ndarray:
        return (self.flags & FLAG_SCALAR_FALLBACK) != 0

    @property
    def ts_sorted(self) -> np.ndarray:
        """
        `ts` in ascending order, which is what the merge engine binary-searches.

        For the overwhelmingly common monotonic file this is `ts` itself with no
        copy; for a file that needed reordering it is the permuted view.
        """
        return self.ts if self.order is None else self.ts[self.order]

    def release(self) -> None:
        """
        Drop any memory mappings this index holds.

        A sidecar is served by mapping it (SPEC 5.4), and on Windows an open
        mapping makes the file undeletable: the prune that is supposed to
        retire a superseded sidecar fails silently, and a log reopened a few
        times accumulates a near-full-size cache entry per visit. The arrays
        are copied out before this is called, so the index that survives owns
        its memory outright.

        Using the index after this is undefined -- not an exception, a crash --
        so it is called only on an index that has just been spliced away.
        """
        for name in ("off", "ts", "flags", "line_no", "order"):
            arr = getattr(self, name, None)
            mapped = getattr(arr, "_mmap", None)
            if mapped is not None:
                mapped.close()

    def entry_range(self, i: int) -> tuple[int, int]:
        """Byte range of entry `i`, including its continuation lines (FR-7)."""
        return int(self.off[i]), int(self.off[i + 1])

    def row_at_time(self, wall_us: int, skew_us: int = 0) -> int:
        """
        First row at or after `wall_us`. The primitive the merge engine's
        `time_to_row` sums across sources (SPEC 6.2).
        """
        return int(np.searchsorted(self.ts_sorted, wall_us - skew_us, side="left"))

    def time_span(self) -> tuple[int, int]:
        s = self.ts_sorted
        return (int(s[0]), int(s[-1])) if s.size else (0, 0)

    def nbytes(self) -> int:
        total = self.off.nbytes + self.ts.nbytes + self.flags.nbytes + self.line_no.nbytes
        return total + (self.order.nbytes if self.order is not None else 0)

    def bytes_per_entry(self) -> float:
        return self.nbytes() / self.n if self.n else 0.0
