"""
The timestamp-format catalogue and the layout model -- SPEC 4.1, 4.3, 4.4.

The catalogue of recognised formats is adopted from `logmerger` by Paul McGuire
(MIT); see NOTICE. The detection *strategy* (SPEC 4.2) and the vectorised
extraction this feeds (SPEC 4.4) are original.

## Why layouts are synthesised rather than written out

A format's regex says what *may* appear; a file says what *does*. logmerger's
patterns allow `\\d{1,6}` fractional digits, so the field width is not knowable
from the pattern -- but any one file almost always uses a single consistent
width. Rather than hand-write a fixed layout per format and guess, detection
measures each named group's offset and width across a sample and synthesises a
`Layout` only when they agree (SPEC 4.3). One generic kernel then serves every
format, and it adapts to the file in front of it.

That also means a new format costs one regex with named groups, not a new kernel.

## Group names

    Y  4-digit year        m  numeric month       b  3-letter month name
    d  day                 H  hour                M  minute
    S  second              f  fractional digits   z  timezone offset or Z
    epoch  seconds / milliseconds / float seconds since the epoch
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

# ---------------------------------------------------------------------------
# flags bits (SPEC 2.3)
# ---------------------------------------------------------------------------

FLAG_SEVERITY_MASK = 0b0000_0111
FLAG_MULTILINE = 0b0000_1000
FLAG_TS_INFERRED = 0b0001_0000
FLAG_SCALAR_FALLBACK = 0b0010_0000

TS_NONE = np.iinfo(np.int64).min
"""Sentinel for "no timestamp established" (SPEC 2.2)."""


class Semantics(StrEnum):
    CIVIL = "civil"
    EPOCH_S = "epoch_s"
    EPOCH_MS = "epoch_ms"


@dataclass(frozen=True)
class Span:
    """A numeric field, positioned relative to the start of the timestamp."""

    pos: int
    width: int
    space_padded: bool = False
    """True where a leading space stands in for a zero, as in syslog's `Oct  4`."""


@dataclass(frozen=True)
class TimestampFormat:
    """A candidate format. Detection turns a match of this into a `Layout`."""

    name: str
    pattern: str
    semantics: Semantics = Semantics.CIVIL
    specificity: int = 100
    line_anchored: bool = True
    """True if the timestamp is expected at the very start of the line."""
    anchor_byte: str = ""
    """
    For formats whose timestamp follows variable-length leading content (an IP
    address, a PID), the byte it follows. Detection locates the timestamp per
    line from the first occurrence of this byte, which stays vectorisable.
    """
    infer_year: bool = False
    infer_date: bool = False
    has_tz: bool = False
    space_padded_day: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# month names -> numbers, vectorisably
# ---------------------------------------------------------------------------

MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _month_key(name: str) -> int:
    u = name.upper()
    return (ord(u[0]) << 16) | (ord(u[1]) << 8) | ord(u[2])


_pairs = sorted((_month_key(n), i + 1) for i, n in enumerate(MONTH_ABBR))
MONTH_KEYS = np.array([k for k, _ in _pairs], dtype=np.int32)
MONTH_VALUES = np.array([v for _, v in _pairs], dtype=np.int32)
"""Sorted 3-byte keys and their month numbers, for `searchsorted` lookup."""


def month_from_bytes(c0: np.ndarray, c1: np.ndarray, c2: np.ndarray):
    """
    Map three uppercase-folded bytes to a month number, vectorised.

    A 12-entry `searchsorted` over a packed 3-byte key, then a verify so an
    unknown name is rejected rather than silently mapped to a neighbour.
    """
    key = (c0.astype(np.int32) << 16) | (c1.astype(np.int32) << 8) | c2.astype(np.int32)
    idx = np.searchsorted(MONTH_KEYS, key)
    np.clip(idx, 0, MONTH_KEYS.size - 1, out=idx)
    ok = MONTH_KEYS[idx] == key
    return MONTH_VALUES[idx], ok


def fold_upper(c: np.ndarray) -> np.ndarray:
    return np.where((c >= 97) & (c <= 122), c - 32, c).astype(np.uint8)


# ---------------------------------------------------------------------------
# the catalogue
# ---------------------------------------------------------------------------

_Y = r"(?P<Y>\d{4})"
_MO = r"(?P<m>\d{2})"
_D = r"(?P<d>\d{2})"
_H = r"(?P<H>\d{2})"
_MI = r"(?P<M>\d{2})"
_S = r"(?P<S>\d{2})"
_B = r"(?P<b>[A-Za-z]{3})"
_Z = r"\s?(?P<z>Z|[+-]\d{2}:?\d{2})"


def _iso_family() -> list[TimestampFormat]:
    """
    The 12 ISO-ish variants: ` ` or `T` separator, `.` / `,` / no fraction,
    with or without a zone.

    Generated rather than written out, because they differ only in two bytes and
    a hand-written set of twelve is twelve chances to make a typo. Specificity
    ranks a zone-bearing variant above its zone-less twin, and a
    fraction-bearing variant above its fraction-less twin, so the candidate that
    consumes more of the line is preferred (SPEC 4.3 step 5).
    """
    out: list[TimestampFormat] = []
    for sep_name, sep in (("space", " "), ("t", "T")):
        for frac_name, frac in (("dot", "."), ("comma", ","), ("nofrac", "")):
            for tz in (True, False):
                body = f"{_Y}-{_MO}-{_D}{sep}{_H}:{_MI}:{_S}"
                if frac:
                    body += rf"\{frac}(?P<f>\d{{1,9}})"
                if tz:
                    body += _Z
                spec = 100 + (10 if frac else 0) + (20 if tz else 0)
                out.append(TimestampFormat(
                    name=f"iso_{sep_name}_{frac_name}{'_z' if tz else ''}",
                    pattern=body, specificity=spec, has_tz=tz,
                ))
    return out


CATALOGUE: list[TimestampFormat] = [
    *_iso_family(),

    # Apache error log: [Fri Dec 01 00:00:25.933177 2023]
    TimestampFormat(
        name="apache_error",
        pattern=rf"\[(?P<a>[A-Za-z]{{3}}) {_B} {_D} {_H}:{_MI}:{_S}"
                rf"\.(?P<f>\d{{1,9}}) {_Y}\]",
        specificity=150,
        note="day name is present but redundant; the date is explicit",
    ),

    # Apache/IIS access log: 91.194.60.14 - - [16/Sep/2023:19:05:06 +0000] "GET ..."
    TimestampFormat(
        name="apache_access",
        pattern=rf"{_D}/{_B}/{_Y}:{_H}:{_MI}:{_S} (?P<z>[+-]\d{{4}})",
        specificity=145, line_anchored=False, anchor_byte="[", has_tz=True,
        note="follows a variable-length client address; located from the '['",
    ),

    # Python http.server: ::1 - - [22/Sep/2023 21:58:40] "GET /log1.txt HTTP/1.1"
    TimestampFormat(
        name="python_http",
        pattern=rf"{_D}/{_B}/{_Y} {_H}:{_MI}:{_S}",
        specificity=140, line_anchored=False, anchor_byte="[",
    ),

    # dd/Mon/yyyy hh:mm:ss, line anchored
    TimestampFormat(
        name="dmy_hms",
        pattern=rf"{_D}/{_B}/{_Y} {_H}:{_MI}:{_S}",
        specificity=135,
    ),

    # syslog: Oct  4 14:22:08   (no year at all)
    TimestampFormat(
        name="syslog_bdhms",
        pattern=rf"{_B} (?P<d>[ \d]\d) {_H}:{_MI}:{_S}",
        specificity=80, infer_year=True, space_padded_day=True,
        note="yearless; year anchored from mtime and walked back (SPEC 4.5)",
    ),

    # strace with a leading pid: 12345 14:22:08.113456
    TimestampFormat(
        name="strace_pid",
        pattern=rf"(?P<pid>\d+) {_H}:{_MI}:{_S}\.(?P<f>\d{{1,9}})",
        specificity=75, infer_date=True,
    ),

    # strace: 14:22:08.113456   (no date at all)
    TimestampFormat(
        name="strace_hms",
        pattern=rf"{_H}:{_MI}:{_S}\.(?P<f>\d{{1,9}})",
        specificity=70, infer_date=True,
        note="dateless; date anchored from mtime (SPEC 4.5)",
    ),

    # Epoch forms. Ranked so the variant consuming more digits wins: a 10-digit
    # pattern is a prefix of a 13-digit one, which is the ordering bug in
    # logmerger's implicit declaration-order resolution (SPEC 4.2).
    TimestampFormat(
        name="epoch_float", pattern=r"(?P<epoch>\d{10})\.(?P<f>\d{1,9})",
        semantics=Semantics.EPOCH_S, specificity=60,
        note="whole seconds plus a fraction; no float-specific kernel path",
    ),
    TimestampFormat(
        name="epoch_ms", pattern=r"(?P<epoch>\d{13})",
        semantics=Semantics.EPOCH_MS, specificity=55,
    ),
    TimestampFormat(
        name="epoch_s", pattern=r"(?P<epoch>\d{10})",
        semantics=Semantics.EPOCH_S, specificity=50,
    ),
]

BY_NAME: dict[str, TimestampFormat] = {f.name: f for f in CATALOGUE}

assert len(BY_NAME) == len(CATALOGUE), "duplicate format name in CATALOGUE"


# ---------------------------------------------------------------------------
# the synthesised layout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """
    A format pinned to the byte positions a specific file actually uses.

    Produced by `detect.synthesise_layout` from observed match spans, and
    consumed by the single vectorised kernel in `extract.py`. Positions are
    relative to the start of the timestamp, not the start of the line, so the
    same layout serves a line-anchored and an anchor-byte-located format.
    """

    fmt_name: str
    semantics: Semantics
    ts_width: int
    ts_offset: int = 0
    """Bytes from line start. Ignored when `anchor_byte` is set."""
    anchor_byte: int = -1
    anchor_skip: int = 0
    """Bytes from the anchor byte to the first byte of the timestamp."""

    seps: tuple[tuple[int, int], ...] = ()
    year: Span | None = None
    month: Span | None = None
    month_name: int | None = None
    day: Span | None = None
    hour: Span | None = None
    minute: Span | None = None
    second: Span | None = None
    frac: Span | None = None
    tz: Span | None = None
    epoch: Span | None = None

    infer_year: bool = False
    infer_date: bool = False

    @property
    def is_anchored(self) -> bool:
        return self.anchor_byte >= 0

    @property
    def frac_scale(self) -> int:
        """Multiplier taking the fractional digits to whole microseconds."""
        if self.frac is None:
            return 0
        return 10 ** max(0, 6 - self.frac.width)

    @property
    def frac_divisor(self) -> int:
        """Divisor for a fraction finer than microseconds (SPEC 2.2 truncates)."""
        if self.frac is None:
            return 1
        return 10 ** max(0, self.frac.width - 6)

    def describe(self) -> str:
        where = (f"after first {chr(self.anchor_byte)!r}+{self.anchor_skip}"
                 if self.is_anchored else f"offset {self.ts_offset}")
        return f"{self.fmt_name} ({self.ts_width}B at {where})"
