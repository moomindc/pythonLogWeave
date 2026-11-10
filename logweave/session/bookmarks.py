"""
Remembered rows -- FR-37, FR-49. Qt-free, layer 4.

A bookmark is a place a reader wants to come back to: the line where the pool
was exhausted, the restart they are measuring everything else against. There
are tens of them, never thousands, which is why this is a dictionary and not
an index.

## A bookmark is `(source, entry)`, never a row

The same conclusion M7 reached about search hits, for the same reason. A row
number moves when a source is hidden (FR-9), when a filter is applied (FR-45),
and when a followed file appends in the middle (SPEC 6.4) -- so a bookmark
stored as row 4,812 is a bookmark that silently drifts onto another line.
`(source, entry)` is what the reader actually marked, and rows are derived on
demand from whatever merge is in force.

A bookmark on a source that is switched off or filtered out therefore has *no*
row. It is kept, because showing the file again must bring it back, and it is
simply not among the places the navigation keys step through: F2 has to land
somewhere the reader can see.

## In a profile it is `(alias, timestamp)`, and the timestamp is the file's

An entry number is meaningless once a file has been re-indexed, rotated or
grown, so a saved bookmark names the source by alias and the row by its
timestamp (SPEC 12.2). That timestamp is the one **in the file**, without the
source's skew (D2, FR-18): a skew is a correction a reader tunes during a
session, and tuning it must not make every saved bookmark point at a different
line.

Restoring takes the first entry at or after the stored time, so a bookmark
survives the file having grown; if the entry it named has been rotated out of
existence, it lands on the next one rather than vanishing, and the UI says how
many did not land exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .marks import MarkLayer


@dataclass(frozen=True)
class Bookmark:
    """One marked entry. `note` is the reader's own, and usually empty."""

    source: int
    entry: int
    note: str = ""


class Bookmarks:
    """
    The marked entries of one session, in no particular order until asked.

    Mutable and small. Kept by the window rather than by the session for the
    same reason the follow engine is (SPEC 9.1): FR-33 allows several windows
    over the same files, and a bookmark is about how *this* window is being
    read.
    """

    def __init__(self) -> None:
        self._notes: dict[tuple[int, int], str] = {}

    # -- the set itself --------------------------------------------------

    def __len__(self) -> int:
        return len(self._notes)

    def __contains__(self, key: tuple[int, int]) -> bool:
        return (int(key[0]), int(key[1])) in self._notes

    def __iter__(self):
        for (source, entry), note in sorted(self._notes.items()):
            yield Bookmark(source=source, entry=entry, note=note)

    def add(self, source: int, entry: int, note: str = "") -> None:
        self._notes[(int(source), int(entry))] = note

    def discard(self, source: int, entry: int) -> None:
        self._notes.pop((int(source), int(entry)), None)

    def toggle(self, source: int, entry: int, note: str = "") -> bool:
        """Mark or unmark. Returns whether the row is now bookmarked."""
        key = (int(source), int(entry))
        if key in self._notes:
            del self._notes[key]
            return False
        self._notes[key] = note
        return True

    def clear(self) -> None:
        self._notes.clear()

    # -- where they are, in a session -----------------------------------

    def times(self, session) -> np.ndarray:
        """
        Merged `wall_us` of every bookmark, sorted. Skew applied, as the strips
        and the merge both work in merged time.
        """
        out = []
        for mark in self:
            ss = _source(session, mark.source)
            if ss is None or mark.entry >= ss.index.n:
                continue
            out.append(int(ss.index.ts[mark.entry]) + int(ss.meta.skew_us))
        return np.sort(np.asarray(out, dtype=np.int64))

    def layer(self, session, colour: str) -> MarkLayer | None:
        """
        The bookmarks as a strip layer (FR-37), or None when there are none.

        A `MarkLayer` so that both strips draw bookmarks through exactly the
        code that draws a rule's marks and the find's hits -- the strips should
        not learn a third way to put a tick in a gutter.
        """
        times = self.times(session)
        if not times.size:
            return None
        return MarkLayer(rule="bookmarks", colour=colour, times=times)

    def rows(self, session) -> np.ndarray:
        """
        The rows the bookmarks are on, ascending, in the merge now in force.

        Bookmarks the view cannot reach -- a source switched off, a row a
        filter excluded -- are left out rather than given a row they do not
        have. That is what makes "next bookmark" land somewhere visible.
        """
        merge = session.merge
        if merge is None or not self._notes:
            return np.zeros(0, dtype=np.int64)

        sources, entries = [], []
        active = set(merge.active)
        for mark in self:
            if mark.source not in active:
                continue
            ss = _source(session, mark.source)
            if ss is None or mark.entry >= ss.index.n:
                continue
            keep = merge.sources[mark.source].keep
            if keep is not None and not keep[mark.entry]:
                continue
            sources.append(mark.source)
            entries.append(mark.entry)
        if not sources:
            return np.zeros(0, dtype=np.int64)
        rows = merge.rows_of(np.asarray(sources, dtype=np.int64),
                             np.asarray(entries, dtype=np.int64))
        return np.sort(rows[rows >= 0])

    def next_row(self, session, after: int, *, back: bool = False) -> int:
        """
        The next bookmarked row after `after`, or -1 if there is none.

        Strictly after, so pressing the key twice moves twice; no wrapping,
        because a reader stepping through bookmarks to see what they marked
        should be told they are at the end rather than quietly returned to the
        start.
        """
        rows = self.rows(session)
        if not rows.size:
            return -1
        if back:
            i = int(np.searchsorted(rows, after, side="left")) - 1
            return int(rows[i]) if i >= 0 else -1
        i = int(np.searchsorted(rows, after, side="right"))
        return int(rows[i]) if i < rows.size else -1

    # -- saving and restoring (FR-49) -----------------------------------

    def to_pairs(self, session) -> list[tuple[str, int, int, str]]:
        """
        `(alias, file timestamp, line number, note)` per bookmark.

        **The line number is a correction to SPEC 12.2**, which stores a
        bookmark as alias and timestamp alone. That is not an identifier: a
        service writing several lines in the same millisecond gives them the
        same timestamp, and the corpus has duplicate timestamps on purpose
        because real logs are full of them. Restoring by time alone lands on
        the first line of the run, which is a bookmark that moved -- the exact
        failure `(source, entry)` exists to prevent, reintroduced by the file
        format.

        The line number also happens to be the part a human reading the TOML
        can act on: `gw` line 1423 is something you can go and look at.
        """
        out = []
        for mark in self:
            ss = _source(session, mark.source)
            if ss is None or mark.entry >= ss.index.n:
                continue
            out.append((ss.alias, int(ss.index.ts[mark.entry]),
                        int(ss.index.line_no[mark.entry]), mark.note))
        return out

    def restore(self, session, pairs) -> list[str]:
        """
        Put saved bookmarks back, returning what could not be placed exactly.

        Matching is by alias, then the saved line number within the run of
        entries sharing the saved timestamp, then the first entry at or after
        it. The line number is what disambiguates a service that wrote four
        lines in the same millisecond; falling back to the timestamp is what
        lets a bookmark survive the file having been rewritten with different
        line numbers.

        A source that is not open is reported rather than dropped silently: a
        reader who saved six bookmarks and sees four has to be told which two
        went and why.
        """
        by_alias = {ss.alias: i for i, ss in enumerate(session.sources)}
        notes: list[str] = []
        approximate = 0
        for alias, wall_us, line_no, note in pairs:
            index = by_alias.get(alias)
            if index is None:
                notes.append(f"bookmark on {alias!r}: that source is not open")
                continue
            ss = session.sources[index]
            ts = np.asarray(ss.index.ts, dtype=np.int64)
            at = int(np.searchsorted(ts, int(wall_us), side="left"))
            if at >= ss.index.n:
                notes.append(
                    f"bookmark on {alias!r}: no entry at or after that time")
                continue
            end = int(np.searchsorted(ts, int(wall_us), side="right"))
            if line_no and end > at:
                same = np.flatnonzero(
                    np.asarray(ss.index.line_no[at:end]) == int(line_no))
                if same.size:
                    at += int(same[0])
            if int(ts[at]) != int(wall_us):
                approximate += 1
            self.add(index, at, note)
        if approximate:
            notes.append(
                f"{approximate} bookmark{'s' if approximate > 1 else ''} "
                "landed on the next entry, because the one saved is no longer "
                "in the file")
        return notes


def _source(session, index: int):
    sources = getattr(session, "sources", [])
    return sources[index] if 0 <= index < len(sources) else None
