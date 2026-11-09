"""
Incremental find across the merged stream -- FR-44, NFR-10, SPEC 11.1.

A find is a cancellable pass over every byte of every source, reporting hits as
it goes. What it returns is **(source, entry) pairs and the merged rows they
sit on**, which is the pair of facts the three consumers need: the row for "hit
7 of 1,284", for next/previous and for the scroll; the time for the marks on
both overview strips.

Qt-free, and safely runnable on a worker thread for the same two reasons
`session.marks` is: it only ever reads the session's arrays and the source's
bytes, and what it returns is made of its own. `ui.findscan` holds the thread.

## Rows, not times, and why that costs something worth paying

`MarkLayer` records a rule's hits as timestamps, because a row number moves
when a source is switched off or a followed file appends in the middle. A find
cannot do only that: "next hit" from the row the cursor is on has to compare
against *rows*, and in a busy log a timestamp is shared by dozens of them, so
seeking by time would land on the first row of the second and call it the next
hit. So the durable record is the `(source, entry)` pairs -- which nothing can
invalidate short of re-indexing the file -- and the rows are **derived**, by
`MergeIndex.rows_of`, whenever the merge changes underneath. Deriving 200,000 of
them is tens of milliseconds (see `rows_of`); doing it per hit would be
seconds, which is why that method exists.

## The cap

`MAX_HITS` is 200,000, as it is for strip marks and for the same reason: past
that point the strips mark every pixel, next/previous is not how anyone is
reading, and collecting a million match objects to say so is the slowest
possible way to say it. The scan stops there and reports itself truncated; the
find bar says to narrow the pattern.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

import numpy as np

from . import scan as scanning
from .marks import MarkLayer

MAX_HITS = 200_000
"""Hits kept, across the whole session. See the module docstring."""

PARTIAL_EVERY_MS = 120.0
"""
How often a running scan publishes what it has found.

NFR-10 asks for incremental results, and the useful cadence is "as fast as a
reader can take in a changing number" rather than "every chunk": a 4 MB chunk
is about 3 ms, so per chunk would be 300 repaints a second of a label nobody
can read.
"""


@dataclass(frozen=True)
class Query:
    """What to look for. Hashable, so it can key a cached result."""

    text: str = ""
    regex: bool = False
    case_sensitive: bool = False

    @property
    def empty(self) -> bool:
        return not self.text

    def validate(self) -> str:
        """Empty if this query can be run, else why it cannot."""
        if self.empty:
            return ""
        if self.regex:
            return scanning.validate_regex(self.text)
        return ""

    def describe(self) -> str:
        bits = ["regex" if self.regex else "text"]
        bits.append("case-sensitive" if self.case_sensitive else "any case")
        return f"{self.text!r} ({', '.join(bits)})"


@dataclass
class Hits:
    """
    Where a query matched, in merged order.

    Not frozen, because `locate` memoises the derived rows against the merge
    generation they were computed for -- and because a running scan publishes
    partial results, which are this same object with `complete` false.
    """

    query: Query = Query()
    source: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64))
    entry: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64))
    times: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64))
    """Skewed merged `wall_us` per hit, in merged order (not sorted on its own
    account -- merged order and time order agree except across ties)."""
    rows: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64))
    """Merged row per hit, ascending. Re-derived by `locate`."""

    truncated: bool = False
    cancelled: bool = False
    complete: bool = False
    notes: tuple[str, ...] = ()
    bytes_scanned: int = 0
    bytes_total: int = 0
    elapsed_ms: float = 0.0
    key: str = ""

    @property
    def count(self) -> int:
        """Hits the view can actually get to. See `locate`."""
        return int(self.rows.size)

    @property
    def found(self) -> int:
        """Hits in the files, including any a filter or a hidden source is
        keeping out of the view -- the count a scan reported."""
        return int(self.entry.size)

    @property
    def hidden(self) -> int:
        return max(0, self.found - self.count)

    @property
    def progress(self) -> float:
        if self.complete or not self.bytes_total:
            return 1.0
        return min(1.0, self.bytes_scanned / self.bytes_total)

    # ------------------------------------------------------------------
    # navigation (FR-44)
    # ------------------------------------------------------------------

    def ordinal(self, row: int) -> int:
        """
        Which hit is on `row`, 1-based, or 0 if none is.

        One-based because it is shown to a person -- "3 of 18" -- and zero is
        then free to mean "the cursor is not on a hit", which is the state the
        find bar is in for most of its life.
        """
        if not self.rows.size:
            return 0
        at = int(np.searchsorted(self.rows, int(row), side="left"))
        return at + 1 if at < self.rows.size and int(self.rows[at]) == int(row) else 0

    def next_after(self, row: int, *, wrap: bool = True) -> int | None:
        """The first hit strictly after `row`, wrapping to the first."""
        if not self.rows.size:
            return None
        at = int(np.searchsorted(self.rows, int(row), side="right"))
        if at < self.rows.size:
            return int(self.rows[at])
        return int(self.rows[0]) if wrap else None

    def prev_before(self, row: int, *, wrap: bool = True) -> int | None:
        """The last hit strictly before `row`, wrapping to the last."""
        if not self.rows.size:
            return None
        at = int(np.searchsorted(self.rows, int(row), side="left")) - 1
        if at >= 0:
            return int(self.rows[at])
        return int(self.rows[-1]) if wrap else None

    def at(self, ordinal: int) -> int | None:
        """The row of the n-th hit, 1-based."""
        if 1 <= int(ordinal) <= self.rows.size:
            return int(self.rows[int(ordinal) - 1])
        return None

    # ------------------------------------------------------------------

    def layer(self, colour: str = "") -> MarkLayer:
        """
        These hits as a strip overlay (FR-44, SPEC 8.4).

        A `MarkLayer` is exactly the right shape and already drawn by both
        strips, so a find mark costs no new drawing code at all -- it is one
        more layer, in the find colour, above the rule marks.

        Only the reachable hits are marked. `locate` sorts the unreachable ones
        to the front, so the visible hits are the last `count` of `times` --
        a strip describes the stream as displayed, and a mark beside a row that
        a filter has removed would point at whatever took its place.
        """
        times = (self.times[self.found - self.count:] if self.count
                 else self.times[:0])
        return MarkLayer(rule="find", colour=colour,
                         times=np.sort(times), truncated=self.truncated)

    def locate(self, merge) -> Hits:
        """
        Re-derive the rows against `merge`, in place, and return self.

        Called when the merge changes under a finished scan: a filter applied or
        cleared, a source switched off, a followed file appending in the
        middle. The `(source, entry)` pairs survive all of those; the row
        numbers do not, and a stale one scrolls to the wrong line with complete
        confidence.

        A hit the current view cannot reach -- in a source switched off (FR-9),
        or on a row a filter excluded (FR-45) -- gets **no row**. It stays in
        `source`/`entry`, so showing the file again brings it back without
        rescanning, and it is counted in `hidden` rather than in `count`:
        "3 of 18" has to mean eighteen rows this view can step through, and
        next/previous must not land on a row that no longer holds the match.
        """
        if merge is None or self.source.size == 0:
            self.rows = np.zeros(0, dtype=np.int64)
            return self
        reachable = np.zeros(self.source.size, dtype=bool)
        active = set(merge.active)
        for si in np.unique(self.source):
            si = int(si)
            if si not in active:
                continue
            mine = self.source == si
            keep = merge.sources[si].keep
            reachable[mine] = True if keep is None else keep[self.entry[mine]]

        rows = np.full(self.source.size, -1, dtype=np.int64)
        if reachable.any():
            rows[reachable] = merge.rows_of(self.source[reachable],
                                            self.entry[reachable])
        # Sorted by row with the unreachable ones first, so slicing them off
        # leaves the reachable rows ascending -- which is what every lookup
        # here binary-searches.
        order = np.argsort(rows, kind="stable")
        self.source = self.source[order]
        self.entry = self.entry[order]
        self.times = self.times[order]
        self.rows = rows[order]
        self.rows = self.rows[self.rows >= 0]
        return self


def search_key(session, query: Query) -> str:
    """
    A digest of everything the answer depends on, and nothing else.

    The pattern and its flags, plus which sources are open and how many entries
    each has -- the last of which is what makes a followed file's append
    produce a new key. Deliberately *not* which sources are enabled or what
    their skews are: a find's hits are entries in files, and hiding a source or
    nudging its clock moves where they are rather than whether they are there.
    """
    parts = [query.text, str(int(query.regex)), str(int(query.case_sensitive))]
    for i, ss in enumerate(getattr(session, "sources", [])):
        parts.append(f"{i}:{ss.meta.origin}:{int(ss.index.n)}")
    return hashlib.blake2b("\x1e".join(parts).encode("utf-8"),
                           digest_size=12).hexdigest()


def search(session, query: Query, *, progress=None, cancelled=None,
           partial=None, max_hits: int = MAX_HITS) -> Hits:
    """
    Find every entry matching `query`, over every open source.

    `progress(done, total)` and `cancelled() -> bool` are called per chunk;
    `partial(hits)` is called at most every `PARTIAL_EVERY_MS` with the hits
    found so far, which is how the count and the strip marks move while the
    scan is still running (NFR-10).

    Scans **every** source, enabled or not. A hidden source's hits are still
    hits -- its rows come back the moment it is shown again, and rescanning the
    whole set because someone unticked a file for a minute would be the slowest
    possible answer to a question that has not changed.
    """
    started = time.perf_counter()
    sources = list(getattr(session, "sources", []))
    total = sum(int(ss.index.indexed_to) for ss in sources)
    merge = getattr(session, "merge", None)

    bad = query.validate()
    if query.empty or bad:
        return Hits(query=query, complete=True, bytes_total=total,
                    notes=((bad,) if bad else ()),
                    key=search_key(session, query))

    found_s: list[np.ndarray] = []
    found_e: list[np.ndarray] = []
    notes: list[str] = []
    state = scanning.ScanProgress(total=total)
    last_publish = started
    truncated = False

    for si, ss in enumerate(sources):
        needle, why = scanning.compile_needle(
            query.text, ss, regex=query.regex,
            case_sensitive=query.case_sensitive)
        if needle is None:
            notes.append(f"{ss.alias}: {why}")
            state.done += int(ss.index.indexed_to)
            continue

        # Chunks of the source being scanned, so a partial result includes
        # everything found so far and not only the latest chunk.
        batches: list[np.ndarray] = []

        # `st` and `trunc` are bound here on purpose. `state` is the same
        # `ScanProgress` object throughout -- `scan_source` fills in the one it
        # is handed -- so binding it reads live progress, and binding the flag
        # takes its value as the snapshot was taken.
        def publish(entries, si=si, batches=batches, st=state,
                    trunc=truncated) -> None:
            nonlocal last_publish
            batches.append(entries)
            now = time.perf_counter()
            if partial is None or (now - last_publish) * 1000.0 < PARTIAL_EVERY_MS:
                return
            last_publish = now
            so_far = np.concatenate(batches)
            partial(_assemble(
                query, sources, merge,
                found_s + [np.full(so_far.size, si, dtype=np.int64)],
                found_e + [so_far],
                notes=notes, truncated=trunc, state=st, started=started))

        rows, state = scanning.scan_source(
            needle, ss, max_hits=max_hits, progress=progress,
            cancelled=cancelled, state=state, label=query.text,
            on_batch=publish)
        truncated = truncated or state.capped
        if rows.size:
            found_s.append(np.full(rows.size, si, dtype=np.int64))
            found_e.append(rows)
        if state.cancelled:
            break
        if state.capped:
            notes.append(f"more than {max_hits:,} matches -- narrow the search")
            break

    hits = _assemble(query, sources, merge, found_s, found_e, notes=notes,
                     truncated=truncated, state=state, started=started)
    hits.complete = not state.cancelled
    hits.cancelled = state.cancelled
    hits.key = search_key(session, query)
    return hits


def _assemble(query, sources, merge, found_s, found_e, *, notes, truncated,
              state, started) -> Hits:
    """One `Hits` out of the per-source entry arrays collected so far."""
    parts: list[np.ndarray] = []
    for s_arr, e_arr in zip(found_s, found_e, strict=True):
        if not e_arr.size:
            continue
        ss = sources[int(s_arr[0])]
        parts.append(np.asarray(ss.index.ts, dtype=np.int64)[e_arr]
                     + int(ss.meta.skew_us))
    if parts:
        src = np.concatenate([a for a in found_s if a.size])
        ent = np.concatenate([a for a in found_e if a.size])
        ts = np.concatenate(parts)
    else:
        src = np.zeros(0, dtype=np.int64)
        ent = np.zeros(0, dtype=np.int64)
        ts = np.zeros(0, dtype=np.int64)

    hits = Hits(query=query, source=src, entry=ent, times=ts,
                truncated=truncated, notes=tuple(notes),
                bytes_scanned=state.done, bytes_total=state.total,
                elapsed_ms=(time.perf_counter() - started) * 1000.0)
    return hits.locate(merge)
