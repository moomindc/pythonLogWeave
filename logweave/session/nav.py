"""
Jumping to the next thing worth reading -- FR-41, FR-42, FR-43.

Three questions, one shape: from the row the cursor is on, which is the nearest
row **in merged order** that is from a given file, or has a given severity, or
both. `Go to timestamp` (FR-41) is not here because it is already a one-liner
on the session -- `time_to_row` is NFR-6's `searchsorted` and needs no scan.

## Why this is not a scan over merged rows

The obvious implementation walks forward through `session.rows()` until
something matches. That is a block replay every 4096 rows and a Python loop
over all of them, so finding the next CRITICAL in a quiet 10M-row log would
read the whole merge.

Instead each source is asked the question in **its own** arrays -- "the first
entry at or after this position whose severity is in this set", which is one
vectorised test over a slice of `flags` -- and the nearest of those answers
wins. The cost is one pass over the entries actually skipped, not over the
rows, and nothing is decoded or read from disk at all.

The windows grow geometrically (`WINDOW` upward by 4x) because the common case
is a hit a few hundred rows away and the worst case is no hit at all: a fixed
small window would make the worst case thousands of numpy calls, and a fixed
large one would make the common case read a megabyte of flags to answer a
question about the next screenful.
"""

from __future__ import annotations

import re

import numpy as np

from ..index.civil import US_PER_DAY, US_PER_SEC, days_from_civil_scalar
from ..index.severity import Severity

WINDOW = 1 << 14
"""Entries examined in the first vectorised pass, then 4x per pass."""

AT_LEAST: dict[int, frozenset[int]] = {
    int(level): frozenset(int(s) for s in Severity if int(s) >= int(level))
    for level in Severity
}
"""
`severity >= X` as a set, which is the form the UI offers (FR-42).

Precomputed because "next warning or worse" is one menu item and one keystroke,
and because `Severity` being ordered is the whole reason D11 fixed it at eight
classes.
"""


def next_row(session, row: int, *, severities: frozenset[int] | None = None,
             source: int | None = None, back: bool = False) -> int | None:
    """
    The nearest row after (or before) `row` matching the given restrictions.

    `None` when there is none, which the caller reports rather than wrapping:
    silently going back to the top when you ask for the next error is how you
    read the same error twice and think there are two.
    """
    merge = session.merge
    if merge is None or merge.total == 0:
        return None
    # Clamped to -1 and not to 0, so that "from before the first row" is
    # expressible: with a floor of 0 a search from the top would skip a match
    # *on* row 0, which is where the first error in a log that failed at
    # startup is.
    row = max(-1, min(int(row), merge.total))

    # The frontier *including* the current row going forward, excluding it
    # going back: "next" and "previous" both have to move off the row the
    # cursor is on, or holding the key down finds the same entry forever.
    cursors = merge.cursors_at_row(row + 1 if not back else row)

    best: tuple[int, int, int] | None = None
    best_entry = -1
    for j in merge.active:
        if source is not None and j != source:
            continue
        ms = merge.sources[j]
        found = _first_match(ms, int(cursors[j]), severities, back=back)
        if found is None:
            continue
        pos, entry = found
        t = int(ms.index.ts_sorted[pos]) + ms.skew_us
        key = (t, j, pos)
        if best is None or (key < best if not back else key > best):
            best, best_entry = key, entry
    if best is None:
        return None
    return merge.row_of(best[1], best_entry)


def _first_match(ms, cursor: int, severities, *, back: bool):
    """
    `(raw position, entry)` of the nearest matching entry from one source.

    `cursor` is a **view** position -- how many of this source's rows are
    already behind the frontier -- so it has to be turned into a raw position
    before the source's own arrays can be sliced. For a filtered source that is
    `Selection.position`, and the mask is then part of the predicate: an entry
    the filter hid is not a row, so jumping to it would put the cursor on
    whatever row took its place.
    """
    sel = ms.selection
    n = ms.index.n
    if n == 0:
        return None
    start = sel.position(cursor) if sel is not None else min(cursor, n)
    order = ms.index.order
    sev = None if severities is None else np.fromiter(
        sorted(severities), dtype=np.uint8)
    whole = np.asarray(ms.index.severity)

    def test(lo: int, hi: int) -> np.ndarray:
        rows = np.arange(lo, hi, dtype=np.int64)
        entries = rows if order is None else order[lo:hi].astype(np.int64)
        ok = np.ones(hi - lo, dtype=bool)
        if sev is not None:
            ok &= np.isin(whole[entries], sev)
        if sel is not None:
            ok &= sel.keep[lo:hi]
        return ok

    if not back:
        at, width = start, WINDOW
        while at < n:
            hi = min(n, at + width)
            hit = np.flatnonzero(test(at, hi))
            if hit.size:
                pos = at + int(hit[0])
                return pos, (pos if order is None else int(order[pos]))
            at, width = hi, width * 4
        return None

    at, width = start, WINDOW
    while at > 0:
        lo = max(0, at - width)
        hit = np.flatnonzero(test(lo, at))
        if hit.size:
            pos = lo + int(hit[-1])
            return pos, (pos if order is None else int(order[pos]))
        at, width = lo, width * 4
    return None


_ABSOLUTE = re.compile(
    r"""^\s*
    (?:(?P<y>\d{4})[-/](?P<mo>\d{1,2})[-/](?P<d>\d{1,2}))?   # the date, if any
    [\sT]*
    (?:(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?
       (?:[.,](?P<frac>\d{1,6}))?)?                          # the time, if any
    \s*$""", re.VERBOSE)

_RELATIVE = re.compile(r"^\s*(?P<sign>[-+])\s*(?P<n>\d+(?:\.\d+)?)\s*"
                       r"(?P<unit>us|ms|s|m|h|d)\s*$", re.IGNORECASE)

_UNIT_US = {"us": 1, "ms": 1_000, "s": 1_000_000, "m": 60_000_000,
            "h": 3_600_000_000, "d": 86_400_000_000}


def parse_time(text: str, anchor_wall_us: int = 0) -> int | None:
    """
    `Go to timestamp` as someone actually types it -- FR-41.

    Returns naive-local `wall_us`, or None if the text is not a time. Four
    shapes, because a reader with a log open has four different things in mind:

    * a whole timestamp -- `2026-10-04 14:22:08.113`, pasted out of a ticket;
    * a date alone -- the start of that day;
    * **a time alone** -- `14:22`, which takes its date from where the view
      currently is. This is the common case by a long way: the log being read
      is one day's, and typing the date again every time would be a tax on the
      only navigation command that is otherwise free (NFR-6);
    * **a relative step** -- `+5m`, `-30s`, from where the view is. "What
       happened in the two minutes before this" is a question with no absolute
       timestamp in it at all.

    Deliberately not `datetime.strptime` with a list of formats: this has to
    accept a partial time, which `strptime` cannot, and the date arithmetic is
    already here in `index.civil`.
    """
    text = (text or "").strip()
    if not text:
        return None

    if (step := _RELATIVE.match(text)):
        delta = float(step["n"]) * _UNIT_US[step["unit"].lower()]
        return int(anchor_wall_us) + int(delta if step["sign"] == "+" else -delta)

    got = _ABSOLUTE.match(text)
    if got is None or not (got["y"] or got["h"]):
        return None

    if got["y"]:
        y, mo, d = int(got["y"]), int(got["mo"]), int(got["d"])
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        day = days_from_civil_scalar(y, mo, d)
    else:
        day = int(anchor_wall_us) // US_PER_DAY

    h = int(got["h"] or 0)
    mi = int(got["mi"] or 0)
    s = int(got["s"] or 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    # A fractional second is padded, not parsed as an integer: `.5` is half a
    # second, not five microseconds.
    frac = int((got["frac"] or "").ljust(6, "0") or 0)
    return (day * US_PER_DAY + h * 3_600_000_000 + mi * 60_000_000
            + s * US_PER_SEC + frac)


def row_at_time(session, wall_us: int) -> int:
    """
    FR-41's `Go to timestamp`, FR-43's near-instant seek.

    Here only so that the window has one place to go for navigation and does
    not have to know that this one is free while the others are not.
    """
    return session.time_to_row(int(wall_us))
