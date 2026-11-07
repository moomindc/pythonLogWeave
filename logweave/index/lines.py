"""
Line splitting and entry grouping -- SPEC 3.3, 5.2, FR-6, FR-7.

A *line* is a run of bytes ending in a terminator. An *entry* is what the user
sees as one row: a line carrying a timestamp, plus any following lines that
carry none -- a Java stack trace is one entry, not forty (FR-7).

Nothing here looks at the content of a line beyond its terminator, so the same
code serves every encoding the source layer resolves: UTF-8, ANSI, and
(once transcoded) UTF-16.
"""

from __future__ import annotations

import numpy as np

LF = 0x0A
CR = 0x0D


def detect_lone_cr(buf: np.ndarray) -> bool:
    """
    True if the chunk contains a CR that is not part of a CRLF.

    SPEC 3.3 takes the lone-CR path only when a file needs it, because
    supporting it costs a second scan of the chunk. Detection runs once per
    file, on the first chunk, and the answer is carried forward.
    """
    cr = np.flatnonzero(buf == CR)
    if cr.size == 0:
        return False
    at_end = cr == buf.size - 1
    nxt = buf[np.minimum(cr + 1, buf.size - 1)]
    return bool(np.any((nxt != LF) | at_end))


def split_lines(
    buf: np.ndarray, lone_cr: bool | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """
    Split a chunk into complete lines.

    Returns `(starts, lens)` where `lens` excludes the terminator and any
    preceding CR, so a CRLF file and an LF file produce identical results
    (FR-6). `lone_cr` selects the terminator rule -- False for the LF-only fast
    path, True to also honour a bare CR, None to detect.

    An unterminated trailing line is deliberately excluded. That is the
    partial-line rule of SPEC 7.3, and it is what stops a half-written line
    being indexed now and duplicated when the writer finishes it -- the bug that
    makes a naive `tail -f` implementation show torn rows.
    """
    if lone_cr is None:
        lone_cr = detect_lone_cr(buf)

    term = np.flatnonzero(buf == LF)
    if lone_cr:
        cr = np.flatnonzero(buf == CR)
        if cr.size:
            at_end = cr == buf.size - 1
            nxt = buf[np.minimum(cr + 1, buf.size - 1)]
            lone = cr[(nxt != LF) | at_end]
            if lone.size:
                term = np.sort(np.concatenate([term, lone]))

    n = term.size
    if n == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    starts = np.empty(n, dtype=np.int64)
    starts[0] = 0
    starts[1:] = term[:-1] + 1
    lens = term - starts
    # Strip a CR belonging to a CRLF pair. Gated on the terminator actually
    # being an LF, so a lone-CR terminator is not shortened by mistake -- the
    # bug the mixed-newline corpus file caught at M0.
    is_lf = buf[term] == LF
    has_cr = (buf[np.maximum(term - 1, 0)] == CR) & is_lf
    return starts, lens - has_cr


def last_complete_line_end(buf: np.ndarray, lone_cr: bool = False) -> int:
    """
    Byte offset just past the last terminator, for aligning chunk boundaries.

    The indexer reads in fixed-size chunks but must never split a line across
    two of them, so each chunk is trimmed back to here and the remainder becomes
    the head of the next.
    """
    last = -1
    lf = np.flatnonzero(buf == LF)
    if lf.size:
        last = int(lf[-1])
    if lone_cr:
        cr = np.flatnonzero(buf == CR)
        if cr.size:
            at_end = cr == buf.size - 1
            nxt = buf[np.minimum(cr + 1, buf.size - 1)]
            lone = cr[(nxt != LF) | at_end]
            if lone.size:
                last = max(last, int(lone[-1]))
    return last + 1


def group_entries(valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Collapse lines into entries: a timestamped line opens one, untimestamped
    lines that follow belong to it (FR-7).

    `np.flatnonzero(valid)` *is* the line ordinal of each entry within the
    chunk, so `line_no` (D9) costs nothing beyond adding the chunk base.

    Returns the line indices that open entries, and the number of lines each
    entry spans -- the latter sets `FLAG_MULTILINE` and gives the renderer its
    row height. Untimestamped lines before the first entry are not covered by
    any span; `leading_orphans` counts them, and the indexer attaches them to
    the entry the previous chunk left open.
    """
    opens = np.flatnonzero(valid)
    if opens.size == 0:
        return opens, np.empty(0, dtype=np.int64)

    # Each entry runs to the line before the next one opens; the last runs to
    # the end of the chunk.
    ends = np.empty(opens.size, dtype=np.int64)
    ends[:-1] = opens[1:]
    ends[-1] = valid.size
    return opens, ends - opens


def leading_orphans(valid: np.ndarray) -> int:
    """How many untimestamped lines sit before the chunk's first entry."""
    if valid.size == 0:
        return 0
    first = np.flatnonzero(valid)
    return int(first[0]) if first.size else int(valid.size)
