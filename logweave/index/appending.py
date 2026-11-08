"""
Indexing an appended region -- SPEC 7.3, FR-21, M6.

The index layer's half of following. Everything here is Qt-free and knows
nothing about sessions, merges or timers: given a source whose file has grown
and the index that covers the old part of it, produce the index that covers all
of it.

## The partial-line rule, and why it needs code

SPEC 7.3 states it in one sentence -- "a read that does not end at a line
terminator must not produce an entry" -- and `build_index` already honours half
of it: an unterminated final line never opens an entry, because the line
splitter only emits terminated lines.

The half it does not honour is the **extent**. `build_index` records
`off[-1] = end`, where `end` is the size it was given, so after indexing a file
whose last line is half written:

    off  = [0, 35, 103]       n = 2, indexed_to = 103

Those 103 bytes include the 32 bytes of half-written line. Two consequences,
both silent:

* the last entry's range runs to 103, so the view shows the previous entry with
  the beginning of the next one stuck to the end of it;
* a follow append resumes at 103, so when the writer finishes the line its
  remainder becomes an **entry of its own** -- the row appears twice, once as a
  fragment attached to its predecessor and once as the tail of itself.

`safe_end` finds where the appended region may be cut, and `trim_to_line`
pulls an already-built index back to that cut. Together they are SPEC 7.3's
"pending buffer": nothing is buffered in memory, because the bytes are still in
the file and the resume offset is the only state needed to come back for them.

## Cost

An append is `build_index` over the appended bytes plus `splice`, and `splice`
concatenates the whole of both sides -- O(entries). That is the one part of a
follow tick that is not O(appended), and it is a `memcpy` of four arrays at
~21 bytes an entry, so a 2 M-entry index copies ~42 MB per tick. Measured at
13 ms, which is inside NFR-8's 200 ms but not free; it is the obvious next thing
to attack (a chunked index that appends in place) and is deliberately not
attacked yet, because the number above was needed before it was worth the
complexity.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..source.base import LogSource
from .cached import detection_of, splice
from .fileindex import FileIndex
from .indexer import build_index
from .severity import DEFAULT_RULESET, SeverityRuleset

SCAN_BACK = 64 * 1024
"""
Bytes read at a time when hunting backwards for the last line terminator.

One block covers the appended region of any ordinary log, so the common case is
a single read. It is a loop rather than a single read of the whole region
because "no terminator anywhere" is a real state -- a writer part-way through a
very long line -- and reading a 100 MB unterminated region in one go to answer
"not yet" would be the wrong shape.
"""

TERMINATORS = (b"\n", b"\r")


@dataclass
class AppendResult:
    """What one append pass did."""

    index: FileIndex
    """The index to use from now on -- a new object, not the one passed in."""
    added: int = 0
    """Entries gained."""
    from_entry: int = 0
    """First entry index that is new, so a caller knows what to repaint."""
    bytes_indexed: int = 0
    pending_bytes: int = 0
    """Bytes read but held back as a part-written line (SPEC 7.3)."""
    needs_rebuild: bool = False
    """
    The append could not be done incrementally; re-index the whole source.

    Not an error. It happens routinely on a log that is still short: a file of
    a few lines is detected by the scalar pattern rather than by a fixed layout
    (SPEC 4.3), and without a layout there is nothing for `detection_of` to
    rebuild the detection from. Re-indexing such a file costs microseconds, and
    once it is long enough to detect a layout the appends become incremental by
    themselves.
    """
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.added > 0


def safe_end(source: LogSource, start: int, end: int) -> int:
    """
    The largest offset in `(start, end]` that sits just after a line terminator.

    Returns `start` when there is none, which means "the whole appended region
    is one unfinished line, come back later".

    Both terminators are accepted because a classic-Mac or bare-CR log is a real
    thing (`detect_lone_cr` exists for it), and because a CRLF file's `\\r` is
    immediately followed by `\\n`, so taking whichever comes last is the same
    answer either way.
    """
    end = min(int(end), source.size())
    start = max(0, int(start))
    pos = end
    while pos > start:
        lo = max(start, pos - SCAN_BACK)
        block = source.read_range(lo, pos)
        if not block:
            break
        cut = max(block.rfind(t) for t in TERMINATORS)
        if cut >= 0:
            return lo + cut + 1
        pos = lo
    return start


def trim_to_line(source: LogSource, index: FileIndex) -> int:
    """
    Pull `index`'s extent back to the last line terminator. Returns bytes given up.

    Called once when a source starts being followed, because the index it starts
    with was built against whatever the file happened to contain at that instant
    -- quite possibly a half-written line, since that is what a live log looks
    like most of the time.

    Only the final `off` slot moves, which shortens the last entry's range and
    nothing else. The entry itself cannot be affected: an unterminated line never
    opened one.
    """
    if index.n == 0:
        return 0
    extent = index.indexed_to
    cut = safe_end(source, int(index.off[index.n - 1]), extent)
    if cut >= extent or cut <= int(index.off[index.n - 1]):
        return 0
    off = np.array(index.off, dtype=np.uint64, copy=True)
    off[-1] = cut
    index.off = off
    return extent - cut


def append_region(
    source: LogSource,
    index: FileIndex,
    *,
    mtime_wall_us: int,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
    **build_kwargs: object,
) -> AppendResult:
    """
    Index whatever has been added to `source` since `index` was built.

    The index is left untouched and a spliced one returned, so a caller that
    swaps it in under a lock has an atomic change rather than a half-updated
    structure that a paint could land in the middle of.

    Returns an unchanged result, rather than raising, in three cases that are
    all normal: nothing was appended; what was appended is still an unfinished
    line; the file's format cannot be recovered from the index, so continuing
    would mean re-detecting on a fragment (`detection_of` explains why that is
    refused). The third sends the caller to a full re-index.
    """
    start = index.indexed_to
    size = source.size()
    if size <= start:
        return AppendResult(index=index, note="nothing appended")

    cut = safe_end(source, start, size)
    if cut <= start:
        return AppendResult(index=index, pending_bytes=size - start,
                            note="appended region is an unfinished line")

    detection = detection_of(index)
    if detection is None:
        return AppendResult(index=index, needs_rebuild=True,
                            note="format not recoverable from the index")

    tail = build_index(
        source,
        mtime_wall_us=mtime_wall_us,
        ruleset=ruleset,
        start_at=start,
        limit=cut - start,
        detection=detection,
        first_line=_next_line(source, index),
        **build_kwargs,
    )
    before = index.n
    merged = splice(index, tail, incremental=True)
    return AppendResult(
        index=merged,
        added=merged.n - before,
        from_entry=before,
        bytes_indexed=cut - start,
        pending_bytes=size - cut,
    )


def _next_line(source: LogSource, index: FileIndex) -> int:
    """
    The 1-based line number the appended region starts at (D9).

    The same reasoning as the sidecar's append path: the last indexed entry may
    span several physical lines, and its stored range includes its own
    terminators, so counting them is exact. One short read against getting every
    copied line number in the tail wrong.
    """
    if not index.n:
        return 1
    lo, hi = index.entry_range(index.n - 1)
    try:
        last = source.read_range(lo, hi)
    except OSError:
        last = b""
    return int(index.line_no[-1]) + max(1, last.count(b"\n"))
