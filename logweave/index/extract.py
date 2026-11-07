"""
The vectorised timestamp extractor -- SPEC 4.4.

One kernel serves every format in the catalogue. It is driven entirely by a
`Layout`, which `detect.py` synthesises from what a specific file actually
contains, so adding a format costs a regex and nothing here.

## The shape of the work

For `n` lines there is no Python loop over lines, no `re`, and no `strptime`.
Instead, for each byte position the format cares about, one *gather* produces a
length-`n` `uint8` column, and the timestamp is assembled by column arithmetic.
The cost is therefore (number of interesting byte positions) gathers, not
`n` parses -- about 26 gathers for an ISO timestamp regardless of `n`.

The five details that took M0's prototype from 97 to 149 MB/s (SPEC 4.4) are all
preserved here, and the reason each matters is noted at its site. The one that is
easiest to undo by accident is `_Cols`: gathering a 2D `(n, W)` block at once
looks tidier and is much slower, because the fancy-index array itself becomes
`n * W * 8` bytes -- 208 MB at a million lines, which no cache holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from .civil import (
    MAX_PLAUSIBLE_YEAR,
    MIN_PLAUSIBLE_YEAR,
    US_PER_SEC,
    days_from_civil,
    days_from_civil_scalar,
    plausible_date,
    plausible_time,
    plausible_wall_us,
    utc_to_local_wall_us,
)
from .civil import US_PER_DAY as US_PER_DAY
from .layouts import (
    MONTH_ABBR,
    Layout,
    Semantics,
    Span,
    TimestampFormat,
    fold_upper,
    month_from_bytes,
)

ZERO = np.uint8(48)
SPACE = np.uint8(32)


@dataclass
class Parts:
    """
    Civil components, returned only when the layout cannot finish the job alone.

    A yearless format (syslog) has no year to read and a dateless one (strace)
    has no date, so for those the indexer must supply the missing piece from the
    file's mtime and walk rollovers (SPEC 4.5). Handing back the components it
    needs avoids a second pass over the bytes.
    """

    month: np.ndarray | None = None
    day: np.ndarray | None = None
    sod_us: np.ndarray | None = None
    """Microseconds since local midnight."""


@dataclass
class Extracted:
    ts: np.ndarray
    """
    `wall_us` per line, or -- when `layout.infer_date` -- microseconds since
    midnight awaiting a date. Meaningless where `valid` is False.
    """
    valid: np.ndarray
    ts_start: np.ndarray
    """Byte offset of the timestamp within the buffer, for highlighting."""
    parts: Parts | None = None

    @property
    def match_rate(self) -> float:
        return float(self.valid.mean()) if self.valid.size else 0.0


# ---------------------------------------------------------------------------
# locating the timestamp within each line
# ---------------------------------------------------------------------------

def first_byte_in_line(
    buf: np.ndarray, starts: np.ndarray, lens: np.ndarray, byte: int
) -> tuple[np.ndarray, np.ndarray]:
    """
    For each line, the offset of the first occurrence of `byte`, vectorised.

    This is what keeps variable-offset formats on the fast path. An Apache or
    IIS access line puts its timestamp after a client address, so the timestamp
    offset differs line to line -- but it is always just past the first `[`, and
    finding every `[` in the chunk at once and reducing to one per line costs
    two sorted-array operations rather than `n` separate searches.
    """
    hits = np.flatnonzero(buf == np.uint8(byte))
    found = np.zeros(starts.size, dtype=bool)
    at = np.zeros(starts.size, dtype=np.int64)
    if hits.size == 0:
        return at, found

    line = np.searchsorted(starts, hits, side="right") - 1
    # Discard hits that landed in a line terminator rather than the body.
    inside = (line >= 0) & (hits < starts[line] + lens[line])
    line = line[inside]
    hits = hits[inside]
    if line.size == 0:
        return at, found

    # `hits` is ascending, so `line` is non-decreasing and np.unique's
    # return_index gives the first hit of each line.
    uniq, first = np.unique(line, return_index=True)
    at[uniq] = hits[first]
    found[uniq] = True
    return at, found


def resolve_ts_start(
    buf: np.ndarray, starts: np.ndarray, lens: np.ndarray, layout: Layout
) -> tuple[np.ndarray, np.ndarray]:
    """Where the timestamp begins in each line, and whether it fits."""
    if layout.is_anchored:
        at, found = first_byte_in_line(buf, starts, lens, layout.anchor_byte)
        ts_start = at + layout.anchor_skip
        room = ts_start + layout.ts_width <= starts + lens
        return ts_start, found & room

    ts_start = starts + layout.ts_offset
    # The length guard is what removes the need to clip every gather index
    # (SPEC 4.4): one comparison here replaces W calls to np.clip.
    room = lens >= layout.ts_offset + layout.ts_width
    return ts_start, room


# ---------------------------------------------------------------------------
# column gathers and digit assembly
# ---------------------------------------------------------------------------

class _Cols:
    """
    Gathers one byte column at a time into a single reused index buffer.

    Reusing `self._idx` across every column is the difference between one
    `n * 8` byte scratch array and an `n * W * 8` byte fancy index.
    """

    __slots__ = ("_buf", "_base", "_idx")

    def __init__(self, buf: np.ndarray, base: np.ndarray):
        self._buf = buf
        self._base = base
        self._idx = np.empty(base.size, dtype=np.int64)

    def at(self, pos: int) -> np.ndarray:
        np.add(self._base, pos, out=self._idx)
        return self._buf[self._idx]

    def digit(self, pos: int, valid: np.ndarray) -> np.ndarray:
        """
        A single decimal digit as a `uint8` in 0..9, narrowing `valid` in place.

        One unsigned comparison, not two: subtracting `'0'` in `uint8` makes any
        byte below `'0'` wrap to a large value, so `<= 9` rejects both ends of
        the range at once (SPEC 4.4).
        """
        v = self.at(pos) - ZERO
        valid &= v <= np.uint8(9)
        return v

    def digit_or_space(self, pos: int, valid: np.ndarray) -> np.ndarray:
        """As `digit`, but a leading space reads as zero -- syslog's `Oct  4`."""
        raw = self.at(pos)
        v = raw - ZERO
        is_space = raw == SPACE
        valid &= (v <= np.uint8(9)) | is_space
        return np.where(is_space, np.uint8(0), v).astype(np.uint8)


def _assemble(limbs: list[np.ndarray], digits: int) -> np.ndarray:
    """
    Combine digit planes into one integer, widening dtype only when forced.

    Digits are paired in `uint8` first -- two decimal digits max out at 99, so
    the arithmetic stays in the narrow lane for one more step, which matters
    because this kernel is memory-bound rather than ALU-bound (SPEC 4.4).

    An odd digit count leaves the most significant limb holding a single digit;
    that is still correct, because a limb's weight is a power of 100 given by
    its position, not by how many digits it happens to carry.
    """
    base100: list[np.ndarray] = []
    i = 0
    if len(limbs) % 2 == 1:
        base100.append(limbs[0])
        i = 1
    while i < len(limbs):
        base100.append(limbs[i] * np.uint8(10) + limbs[i + 1])
        i += 2

    if len(base100) == 1:
        return base100[0]

    dtype = np.int32 if digits <= 9 else np.int64
    acc = base100[0].astype(dtype)
    for limb in base100[1:]:
        acc = acc * dtype(100) + limb
    return acc


def _number(cols: _Cols, span: Span, valid: np.ndarray) -> np.ndarray:
    """Read a fixed-width decimal field."""
    limbs = []
    for j in range(span.width):
        # Only a leading position may be space-padded; a space in the units
        # place is a malformed field, not a zero.
        if span.space_padded and j < span.width - 1:
            limbs.append(cols.digit_or_space(span.pos + j, valid))
        else:
            limbs.append(cols.digit(span.pos + j, valid))
    return _assemble(limbs, span.width)


# ---------------------------------------------------------------------------
# the kernel
# ---------------------------------------------------------------------------

def extract(
    buf: np.ndarray,
    starts: np.ndarray,
    lens: np.ndarray,
    layout: Layout,
) -> Extracted:
    """
    Extract a timestamp from every line in a chunk.

    `buf` is the raw chunk as `uint8`; `starts` and `lens` describe each line's
    body, terminator excluded. Nothing here depends on the chunk's provenance,
    so one code path serves files, gzip members, and later the Event Log spill.
    """
    n = starts.size
    if n == 0 or buf.size < layout.ts_width:
        # The width test is what makes every gather below safe without clipping.
        # Pinning an invalid lane's base to 0 (see below) keeps it in bounds only
        # while the buffer is at least as long as the timestamp -- otherwise
        # offset 0 plus the last column still runs off the end. No line in such a
        # chunk could hold a timestamp anyway, so there is nothing to lose by
        # returning early, and this is the only thing standing between a small
        # final chunk and an IndexError.
        empty = np.zeros(n, dtype=np.int64)
        return Extracted(empty, np.zeros(n, dtype=bool), empty.copy())

    ts_start, valid = resolve_ts_start(buf, starts, lens, layout)

    # Where the line is too short the gather index must still be in bounds.
    # Pinning those lanes to 0 is cheaper than clipping W separate indices, and
    # the garbage they read is discarded by `valid` (SPEC 4.4).
    base = np.where(valid, ts_start, 0)
    cols = _Cols(buf, base)

    for pos, byte in layout.seps:
        valid &= cols.at(pos) == np.uint8(byte)

    if layout.semantics in (Semantics.EPOCH_S, Semantics.EPOCH_MS):
        ts = _epoch(cols, layout, valid)
        return Extracted(ts, valid, ts_start)

    return _civil(cols, layout, valid, ts_start)


def _epoch(cols: _Cols, layout: Layout, valid: np.ndarray) -> np.ndarray:
    assert layout.epoch is not None
    raw = _number(cols, layout.epoch, valid).astype(np.int64)
    if layout.semantics is Semantics.EPOCH_S:
        ts = raw * US_PER_SEC
        if layout.frac is not None:
            ts = ts + _frac_us(cols, layout, valid)
    else:
        ts = raw * 1_000

    # An epoch form has no fields to range-check, so the plausibility window is
    # the only thing standing between a build number and a timestamp.
    valid &= plausible_wall_us(ts)
    # An epoch is an absolute instant, so it is UTC by definition and has to be
    # moved into local wall time before it can be merged (D1).
    return utc_to_local_wall_us(ts, valid)


def _frac_us(cols: _Cols, layout: Layout, valid: np.ndarray) -> np.ndarray:
    assert layout.frac is not None
    raw = _number(cols, layout.frac, valid).astype(np.int64)
    # Truncate rather than round: SPEC 2.2 defines wall_us as truncated, so a
    # nanosecond-resolution log and its microsecond-resolution neighbour stay
    # consistent with each other.
    if layout.frac_divisor > 1:
        raw = raw // layout.frac_divisor
    return raw * layout.frac_scale


def _tz_offset_us(cols: _Cols, layout: Layout, valid: np.ndarray) -> np.ndarray:
    """
    The written zone offset in microseconds, positive east of Greenwich.

    Width distinguishes the three forms the catalogue admits -- `Z`, `+HHMM`,
    `+HH:MM`. A file that mixes them has a varying timestamp width and so never
    reaches this kernel; it goes to the scalar fallback instead.
    """
    assert layout.tz is not None
    span = layout.tz
    if span.width == 1:
        z = cols.at(span.pos)
        valid &= (z == np.uint8(ord("Z"))) | (z == np.uint8(ord("z")))
        return np.zeros(valid.size, dtype=np.int64)

    sign_byte = cols.at(span.pos)
    is_plus = sign_byte == np.uint8(ord("+"))
    is_minus = sign_byte == np.uint8(ord("-"))
    valid &= is_plus | is_minus

    hh = _number(cols, Span(span.pos + 1, 2), valid).astype(np.int64)
    mm_pos = span.pos + 3 + (1 if span.width == 6 else 0)
    mm = _number(cols, Span(mm_pos, 2), valid).astype(np.int64)
    valid &= (hh <= 23) & (mm <= 59)

    magnitude = (hh * 3600 + mm * 60) * US_PER_SEC
    return np.where(is_minus, -magnitude, magnitude)


def _civil(
    cols: _Cols, layout: Layout, valid: np.ndarray, ts_start: np.ndarray
) -> Extracted:
    hour = _number(cols, layout.hour, valid).astype(np.int64) if layout.hour else None
    minute = _number(cols, layout.minute, valid).astype(np.int64) if layout.minute else None
    second = _number(cols, layout.second, valid).astype(np.int64) if layout.second else None

    sod_us = np.zeros(valid.size, dtype=np.int64)
    if hour is not None and minute is not None and second is not None:
        valid &= plausible_time(hour, minute, second)
        sod_us = (hour * 3600 + minute * 60 + second) * US_PER_SEC
    if layout.frac is not None:
        sod_us = sod_us + _frac_us(cols, layout, valid)

    month = None
    if layout.month is not None:
        month = _number(cols, layout.month, valid).astype(np.int64)
    elif layout.month_name is not None:
        p = layout.month_name
        m, ok = month_from_bytes(
            fold_upper(cols.at(p)), fold_upper(cols.at(p + 1)), fold_upper(cols.at(p + 2))
        )
        valid &= ok
        month = m.astype(np.int64)

    day = _number(cols, layout.day, valid).astype(np.int64) if layout.day else None

    if layout.infer_date:
        # No date in the text at all. The indexer anchors it from mtime and
        # walks midnight rollovers, which needs cross-chunk state (SPEC 4.5).
        return Extracted(sod_us, valid, ts_start, Parts(sod_us=sod_us))

    if layout.infer_year:
        # Month and day are present but the year is not. The year depends on how
        # many December-to-January boundaries lie between this entry and the
        # file's tail, so only the indexer can resolve it (SPEC 4.5).
        valid &= (month >= 1) & (month <= 12) & (day >= 1) & (day <= 31)
        return Extracted(
            np.zeros(valid.size, dtype=np.int64), valid, ts_start,
            Parts(month=month, day=day, sod_us=sod_us),
        )

    year = _number(cols, layout.year, valid).astype(np.int64)
    valid &= plausible_date(year, month, day)

    ts = combine_civil(year, month, day, sod_us, valid)

    if layout.tz is not None:
        # The text gave an instant in a named offset. Subtract it to reach UTC,
        # then re-express in local wall time and discard the zone (D1).
        ts = ts - _tz_offset_us(cols, layout, valid)
        ts = utc_to_local_wall_us(ts, valid)

    return Extracted(ts, valid, ts_start)


def combine_civil(
    year: np.ndarray,
    month: np.ndarray,
    day: np.ndarray,
    sod_us: np.ndarray,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """
    Assemble `wall_us` from civil components.

    Exposed because the indexer calls it a second time for yearless formats,
    once it has worked out which year each entry belongs to -- reusing the
    components rather than re-reading the bytes.

    The constant-date fast path matters more than it looks: a single 64 MB chunk
    of a busy log is usually all one calendar day, and `days_from_civil` costs a
    dozen integer divisions per row. Testing constancy over *valid* rows only is
    essential -- invalid rows hold garbage read from offset 0 and would defeat
    the test on almost every chunk (SPEC 4.4).
    """
    key = year * 10_000 + month * 100 + day
    probe = key[valid] if valid is not None else key
    if probe.size and bool((probe == probe[0]).all()):
        first = int(probe[0])
        days = np.int64(
            days_from_civil_scalar(first // 10_000, (first // 100) % 100, first % 100)
        )
    else:
        days = days_from_civil(year, month, day)
    return days * US_PER_DAY + sod_us


# ---------------------------------------------------------------------------
# the scalar reference path
# ---------------------------------------------------------------------------
#
# This exists for two reasons, and the second is the more important.
#
# 1. It is the fallback for a file whose timestamp position or field widths vary
#    line to line, where no fixed layout can be synthesised (SPEC 4.3 step 6).
# 2. It is the *oracle*. Detection runs both paths over the sample and refuses a
#    layout whose fast-path answers disagree with the regex's, which is the
#    direct mitigation for risk R4 -- a format that is silently wrong and
#    mis-orders a whole file. A clever kernel nobody checks is a liability.


_MONTH_NUM = {name: i + 1 for i, name in enumerate(MONTH_ABBR)}


class ScalarResult(NamedTuple):
    ok: bool
    ts: int
    """wall_us, or microseconds since midnight when the date must be inferred."""
    month: int
    day: int
    sod_us: int
    ts_start: int


_BAD = ScalarResult(False, 0, 0, 0, 0, -1)


def scalar_one(m: re.Match[bytes], fmt: TimestampFormat) -> ScalarResult:
    """
    Assemble one timestamp from a regex match, by the same rules as the kernel.

    Deliberately written independently of the vectorised path rather than
    sharing its internals: an oracle that shares the code under test cannot
    catch a shared mistake.
    """
    g = m.groupdict()

    def num(key: str) -> int | None:
        v = g.get(key)
        if v is None:
            return None
        v = v.strip()
        return int(v) if v else 0

    frac_us = 0
    f = g.get("f")
    if f is not None:
        digits = f.decode("ascii")
        frac_us = int(digits[:6].ljust(6, "0")) if digits else 0

    if fmt.semantics in (Semantics.EPOCH_S, Semantics.EPOCH_MS):
        raw = num("epoch")
        if raw is None:
            return _BAD
        ts = raw * US_PER_SEC + frac_us if fmt.semantics is Semantics.EPOCH_S else raw * 1_000
        if not bool(plausible_wall_us(np.array([ts], dtype=np.int64))[0]):
            return _BAD
        ts = int(utc_to_local_wall_us(np.array([ts], dtype=np.int64))[0])
        return ScalarResult(True, ts, 0, 0, 0, m.start())

    hour, minute, second = num("H"), num("M"), num("S")
    if hour is None or minute is None or second is None:
        return _BAD
    if hour > 23 or minute > 59 or second > 60:
        return _BAD
    sod_us = (hour * 3600 + minute * 60 + second) * US_PER_SEC + frac_us

    month = num("m")
    if month is None:
        b = g.get("b")
        if b is not None:
            month = _MONTH_NUM.get(b.decode("ascii", "replace").upper())
    day = num("d")

    if fmt.infer_date:
        return ScalarResult(True, sod_us, 0, 0, sod_us, m.start())

    if month is None or day is None or not (1 <= month <= 12 and 1 <= day <= 31):
        return _BAD

    if fmt.infer_year:
        return ScalarResult(True, 0, month, day, sod_us, m.start())

    year = num("Y")
    if year is None or not (MIN_PLAUSIBLE_YEAR <= year <= MAX_PLAUSIBLE_YEAR):
        return _BAD

    ts = days_from_civil_scalar(year, month, day) * US_PER_DAY + sod_us

    z = g.get("z")
    if z is not None:
        zs = z.decode("ascii", "replace")
        if zs in ("Z", "z"):
            off = 0
        else:
            sign = -1 if zs[0] == "-" else 1
            body = zs[1:].replace(":", "")
            if len(body) != 4 or not body.isdigit():
                return _BAD
            off = sign * (int(body[:2]) * 3600 + int(body[2:]) * 60) * US_PER_SEC
        ts = int(utc_to_local_wall_us(np.array([ts - off], dtype=np.int64))[0])

    return ScalarResult(True, ts, month, day, sod_us, m.start())


def extract_scalar(
    chunk: bytes,
    starts: np.ndarray,
    lens: np.ndarray,
    fmt: TimestampFormat,
    pattern: re.Pattern[bytes],
    *,
    search_limit: int = 256,
) -> Extracted:
    """
    Per-line regex extraction. Correct for everything, fast for nothing.

    `search_limit` bounds how far into a line the timestamp may start, so a
    pathological 1 MB line (which the corpus contains) costs a bounded search
    rather than a scan of the whole line.
    """
    n = int(starts.size)
    ts = np.zeros(n, dtype=np.int64)
    valid = np.zeros(n, dtype=bool)
    ts_start = np.zeros(n, dtype=np.int64)
    month = np.zeros(n, dtype=np.int64)
    day = np.zeros(n, dtype=np.int64)
    sod = np.zeros(n, dtype=np.int64)

    for i in range(n):
        a = int(starts[i])
        end = a + min(int(lens[i]), search_limit + 64)
        m = pattern.search(chunk, a, end)
        if m is None or m.start() - a > search_limit:
            continue
        r = scalar_one(m, fmt)
        if not r.ok:
            continue
        valid[i] = True
        ts[i] = r.ts
        ts_start[i] = m.start()
        month[i] = r.month
        day[i] = r.day
        sod[i] = r.sod_us

    parts = None
    if fmt.infer_year:
        parts = Parts(month=month, day=day, sod_us=sod)
    elif fmt.infer_date:
        parts = Parts(sod_us=sod)
    return Extracted(ts, valid, ts_start, parts)
