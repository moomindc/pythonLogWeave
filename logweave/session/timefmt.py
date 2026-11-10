"""
How a timestamp is displayed -- FR-35. Qt-free, layer 4.

The view, the clipboard's `aligned` preset and the export all have to agree on
what a timestamp looks like, so the decision lives here and each of them asks.
`index.civil.format_wall_us` stays exactly as it is: it is the one *canonical*
rendering, the thing an annotated copy cites and a sidecar header writes, and
that must not move because a reader changed a column.

## Three modes, because readers ask three different questions

* **Absolute** -- "when did this happen?" A pattern, so a reader whose logs are
  all from one afternoon can drop the date and spend the width on the message.
* **Elapsed** -- "how long after the thing I care about?" Signed, from a zero
  point the reader sets on a row. This is what turns a log into a timeline of
  an incident: the deploy is `+0`, and the first timeout is `+1:04.882`.
* **Delta** -- "where did the time go?" Signed, from the previous row *in the
  view*. A 40-second stall between two lines is invisible in absolute mode and
  unmissable here, which is the whole reason FR-35 asks for a relative mode.

Elapsed and delta are two uses of one subtraction and share their renderer.

## Fixed width is a requirement, not a nicety

The view reserves the timestamp column once per font change (`_ts_w`) and
freezes it against horizontal scroll. A format whose rendered width varied by
row would make the message column move as you scrolled, so every mode here
renders **exactly** the width of `TimeFormatter.sample`, padding with spaces
rather than letting a narrow value shrink the column.

For the relative modes that width depends on the data: a log spanning four
minutes needs `+1:23.456789` and one spanning a week needs a day group as
well. The formatter is therefore built against the session's span
(`for_span`), and the window rebuilds it when the span changes rather than
reserving room for a week nobody opened.

## The pattern syntax

A documented subset of `strftime`, not a pass-through to `time.strftime`:

    %Y  2026    %y  26      %m  03      %d  09
    %H  14      %I  02      %M  22      %S  08
    %f  113456  %L  113     %b  Mar     %a  Mon
    %p  PM      %j  068     %%  a literal %

Compiled once into segments, with the date half of a pattern pre-rendered per
day (`_day_template`), so a row costs one `str.format` over the clock fields.
Anything outside the table is **reported** rather than printed raw, for the
same reason `rowformat.validate_template` enumerates its fields: a typo in a
format should say so, not put `%Q` in every row of a 40M-row file.

`%L` is the one extension. Log timestamps are overwhelmingly millisecond
resolution -- log4j, Serilog, Python's own `asctime` -- and C `strftime` has no
directive for them; `%f` is itself a Python extension, so this is the same kind
of thing rather than a new idea. It truncates rather than rounds: a displayed
timestamp must never read later than the one in the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from ..index.civil import (
    US_PER_DAY,
    US_PER_SEC,
    civil_from_days_scalar,
    days_from_civil_scalar,
)

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS = ("Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed")
"""
Indexed by `days % 7`, so day 0 of the civil epoch (1970-01-01) is a Thursday.

Rotated rather than cleverly computed: the epoch's weekday is a fact about the
calendar, and writing it down is easier to read than an offset in an
expression.
"""


class TimeMode(StrEnum):
    ABSOLUTE = "absolute"
    ELAPSED = "elapsed"
    DELTA = "delta"


FULL = "%Y-%m-%d %H:%M:%S.%f"
"""The default, and the same rendering `format_wall_us` has always produced."""

PRESETS: tuple[tuple[str, str], ...] = (
    (FULL, "Date and time, microseconds"),
    ("%Y-%m-%d %H:%M:%S", "Date and time"),
    ("%H:%M:%S.%f", "Time only, microseconds"),
    ("%H:%M:%S.%L", "Time only, milliseconds"),
    ("%H:%M:%S", "Time only"),
    ("%m-%d %H:%M:%S.%L", "Month and day, milliseconds"),
    ("%Y-%m-%dT%H:%M:%S.%f", "ISO 8601"),
    ("%b %d %H:%M:%S", "Syslog"),
)
"""
Offered in the settings dialog, with the reason each exists in its label.

Patterns and no presets would be the worse control for the common case -- a
reader who wants the date gone should not have to learn `strftime` -- and
presets with no custom pattern the worse one for the uncommon case. FR-35 asks
for configurable, so both.
"""

ZERO_FIRST_ROW = -1
"""
`TimeDisplay.zero_us` meaning "the first row of the merged view".

A sentinel rather than 0, because 0 is a real `wall_us` (1970-01-01) and a
profile that stored it should not silently become "the start of the log".
"""

_FIELDS: dict[str, tuple[str, str]] = {
    # directive: (format spec, which value it names)
    "Y": ("{:04d}", "year"),
    "y": ("{:02d}", "year2"),
    "m": ("{:02d}", "month"),
    "d": ("{:02d}", "day"),
    "H": ("{:02d}", "hour"),
    "I": ("{:02d}", "hour12"),
    "M": ("{:02d}", "minute"),
    "S": ("{:02d}", "second"),
    "f": ("{:06d}", "micro"),
    "L": ("{:03d}", "milli"),
    "b": ("{}", "month_name"),
    "a": ("{}", "weekday"),
    "p": ("{}", "meridiem"),
    "j": ("{:03d}", "yearday"),
}

_TIME_INDEX: dict[str, int] = {
    "hour": 0, "hour12": 1, "minute": 2, "second": 3, "micro": 4,
    "milli": 5, "meridiem": 6,
}
"""
The fields that change within a day, and where each sits in the clock tuple.

Everything not in here is a property of the *date*, which is what lets a whole
day of rows share one half-rendered template (`_day_template`). `meridiem` is a
time field although it reads like a label, because it is a function of the hour.
"""

_WIDEST: dict[str, object] = {
    "year": 2026, "year2": 26, "month": 12, "day": 28, "hour": 23,
    "hour12": 12, "minute": 59, "second": 59, "micro": 999_999, "milli": 999,
    "month_name": "Sep", "weekday": "Wed", "meridiem": "PM", "yearday": 365,
}
"""
A value per field that renders at its maximum width, for `sample`.

Every one of them is a fixed width anyway -- that is what the `02d` specs are
for -- so this exists to produce a *representative* sample rather than to
discover a width. The three names are three characters by construction.
"""


class _Seg(NamedTuple):
    """
    One piece of a compiled pattern: a literal, a date field or a time field.

    A literal keeps its raw text *and* a brace-escaped copy, because it is used
    both ways: raw when building the sample, escaped when building the
    `str.format` template a row is rendered through.
    """

    kind: str                 # "lit" | "date" | "time"
    text: str = ""            # literals: the characters themselves
    escaped: str = ""         # literals: with { and } doubled
    spec: str = ""            # fields: the format spec, e.g. "{:02d}"
    name: str = ""            # fields: the value it names
    idx: int = -1             # time fields: position in the clock tuple


def validate_pattern(pattern: str) -> str:
    """Empty if `pattern` is usable, else what is wrong with it."""
    if not pattern.strip():
        return "the format is empty"
    i, bad, fields = 0, [], 0
    while i < len(pattern):
        if pattern[i] != "%":
            i += 1
            continue
        if i + 1 >= len(pattern):
            return "the format ends with a lone %"
        code = pattern[i + 1]
        if code != "%":
            if code in _FIELDS:
                fields += 1
            else:
                bad.append(f"%{code}")
        i += 2
    if bad:
        known = " ".join(f"%{c}" for c in _FIELDS)
        return (f"unknown field{'s' if len(bad) > 1 else ''}: "
                f"{', '.join(bad)}. Known: {known}")
    if not fields:
        return "the format has no time fields in it"
    return ""


@dataclass(frozen=True)
class TimeDisplay:
    """
    What the reader chose. Plain data, so a profile can store it verbatim.

    Frozen: the window swaps one for another rather than mutating, which is
    what lets a changed display be spotted by comparison and the column width
    recomputed only when it has actually moved.
    """

    mode: TimeMode = TimeMode.ABSOLUTE
    pattern: str = FULL
    zero_us: int = ZERO_FIRST_ROW
    """Elapsed mode's origin, or `ZERO_FIRST_ROW`. Ignored by the other two."""

    @property
    def relative(self) -> bool:
        return self.mode is not TimeMode.ABSOLUTE

    @property
    def needs_previous(self) -> bool:
        """Whether rendering a row needs the row before it (delta mode)."""
        return self.mode is TimeMode.DELTA


class TimeFormatter:
    """
    A compiled `TimeDisplay`, ready to render a row.

    Built per display and per span rather than consulted per row: the pattern
    is compiled once and the relative width settled once, so a render is a
    division, a dictionary hit and a `format`.
    """

    def __init__(self, display: TimeDisplay | None = None, *,
                 span_us: int = 0, zero_us: int | None = None):
        self.display = display or TimeDisplay()
        self._segments = _compile(self.display.pattern)
        self._time_idx = [s.idx for s in self._segments if s.kind == "time"]
        self._day_cache: dict[int, str] = {}
        """
        One half-rendered template per distinct day on screen.

        This is the whole of the absolute path's cost. A pattern's date fields
        cannot change within a day and consecutive log rows are nearly always
        on the same one, so the calendar conversion and the date text happen
        once a day and a row is one `str.format` over the clock. Measured at
        **7.0 us a row** against 15.6 for rendering every field of every row,
        and 6.0 for the fixed `civil.format_wall_us` this replaces in the
        view -- so a configurable format costs 0.06 ms of a 16 ms frame over
        the one it replaces (SPEC 14.1h).
        """
        self._shape = _shape_for(span_us)
        self.zero_us = self.display.zero_us if zero_us is None else zero_us
        """
        Elapsed mode's resolved origin -- a real `wall_us`, never the sentinel.

        Resolved by the caller, because only it knows what the first row of the
        view is. An unresolved sentinel renders every row against 1970, which
        is wrong but *visibly* wrong, and that is better than this class
        guessing at a session it cannot see.
        """

    # ------------------------------------------------------------------

    @property
    def mode(self) -> TimeMode:
        return self.display.mode

    @property
    def needs_previous(self) -> bool:
        return self.display.needs_previous

    @property
    def sample(self) -> str:
        """
        The widest string this formatter can produce, for reserving the column.

        A string rather than a pixel count: only the view knows its font, and
        this module must not learn.
        """
        if self.display.mode is TimeMode.ABSOLUTE:
            return "".join(
                s.text if s.kind == "lit" else s.spec.format(_WIDEST[s.name])
                for s in self._segments)
        return _relative(-self._shape.covers, self._shape)

    def render(self, wall_us: int, previous_us: int | None = None) -> str:
        """
        One row's timestamp, exactly `len(sample)` wide.

        `previous_us` is the row above this one in the view: needed by delta
        mode, ignored by the others. None means there is no row above -- the
        top of the log -- and delta then renders blank rather than zero,
        because a reader scanning for stalls must not read the first row as "no
        gap".
        """
        mode = self.display.mode
        if mode is TimeMode.ABSOLUTE:
            return self.absolute(wall_us)
        if mode is TimeMode.ELAPSED:
            return _relative(int(wall_us) - int(self.zero_us), self._shape)
        if previous_us is None:
            return " " * len(self.sample)
        return _relative(int(wall_us) - int(previous_us), self._shape)

    def absolute(self, wall_us: int) -> str:
        """
        The absolute rendering, whatever mode is in force.

        Always available, because a relative column still has to be
        explainable: the status bar says what `+1:04.882` is in clock time, and
        a reader who had to switch modes to find that out would have been given
        a worse tool than they had before.
        """
        wall = int(wall_us)
        days, rem = divmod(wall, US_PER_DAY)
        template = self._day_cache.get(days)
        if template is None:
            if len(self._day_cache) > 512:
                # Bounded for the same reason `civil._DATE_CACHE` is: a 10 GB
                # merge spans thousands of days, and this exists to serve the
                # handful on screen rather than to remember the log.
                self._day_cache.clear()
            template = self._day_template(days)
            self._day_cache[days] = template
        secs, us = divmod(rem, US_PER_SEC)
        h, rest = divmod(secs, 3600)
        mi, sec = divmod(rest, 60)
        clock = (h, (h % 12) or 12, mi, sec, us, us // 1000,
                 "AM" if h < 12 else "PM")
        return template.format(*[clock[i] for i in self._time_idx])

    def _day_template(self, days: int) -> str:
        """
        The pattern with this day's date in it and the clock fields left open.

        A `str.format` template, which is why the literals arrive
        brace-escaped: a reader's own format may legitimately contain a brace,
        and a `{` reaching `format` unescaped raises inside a paint.
        """
        year, month, day, month_name, weekday, yearday = _date_fields(days)
        values: dict[str, object] = {
            "year": year, "year2": year % 100, "month": month, "day": day,
            "month_name": month_name, "weekday": weekday, "yearday": yearday,
        }
        return "".join(
            s.escaped if s.kind == "lit"
            else s.spec if s.kind == "time"
            else s.spec.format(values[s.name])
            for s in self._segments)


def for_span(display: TimeDisplay, *, span_us: int,
             first_row_us: int = 0) -> TimeFormatter:
    """
    Build a formatter for a session: resolves the zero point and the width.

    The two things a `TimeDisplay` cannot settle on its own, both of them facts
    about the log that is open rather than about what the reader asked for.
    """
    zero = display.zero_us
    if zero == ZERO_FIRST_ROW:
        zero = int(first_row_us)
    return TimeFormatter(display, span_us=span_us, zero_us=zero)


# ---------------------------------------------------------------------------
# compiling a pattern
# ---------------------------------------------------------------------------


def _compile(pattern: str) -> tuple[_Seg, ...]:
    """
    A pattern into the segments a render walks, literals and fields apart.

    An unknown directive is kept **verbatim** here rather than raising: this is
    reached from a paint, and a bad pattern must degrade to a visibly odd
    column instead of an exception sixty times a second. `validate_pattern` is
    what refuses one, at the point where a human can be told about it.
    """
    segs: list[_Seg] = []
    literal: list[str] = []

    def flush() -> None:
        if literal:
            text = "".join(literal)
            segs.append(_Seg("lit", text=text,
                             escaped=text.replace("{", "{{").replace("}", "}}")))
            literal.clear()

    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch != "%":
            literal.append(ch)
            i += 1
            continue
        if i + 1 >= len(pattern):
            literal.append("%")
            break
        code = pattern[i + 1]
        if code == "%":
            literal.append("%")
        elif code in _FIELDS:
            spec, name = _FIELDS[code]
            flush()
            segs.append(_Seg("time" if name in _TIME_INDEX else "date",
                             spec=spec, name=name,
                             idx=_TIME_INDEX.get(name, -1)))
        else:
            literal.append(f"%{code}")
        i += 2
    flush()
    return tuple(segs)


def _date_fields(days: int) -> tuple[int, int, int, str, str, int]:
    y, m, d = civil_from_days_scalar(days)
    jan1 = days_from_civil_scalar(y, 1, 1)
    return (y, m, d, MONTHS[m - 1], WEEKDAYS[days % 7], days - jan1 + 1)


# ---------------------------------------------------------------------------
# the relative modes
# ---------------------------------------------------------------------------

_GROUP_SECONDS = (60, 3600, 86_400)
"""Spans at which the minute, hour and day groups start being needed."""


@dataclass(frozen=True)
class _Shape:
    """
    How wide a relative column has to be for the log that is open.

    `groups` is 1 to 4 -- seconds, minutes, hours, days -- and `day_w` is how
    many digits the day group needs. Both come from the span: reserving the day
    group for a log covering one afternoon costs four characters of message on
    every row, and a fixed two-digit day group would make the column jitter on
    an archive spanning more than a hundred days.
    """

    groups: int = 1
    day_w: int = 1

    @property
    def covers(self) -> int:
        """
        The largest offset this shape renders without widening.

        Computed here rather than in `_relative` because `_relative` is on the
        paint path: checking an offset against one integer is what keeps the
        overflow guard from costing a shape construction per row.
        """
        if self.groups == 1:
            return 60 * US_PER_SEC - 1
        if self.groups == 2:
            return 3600 * US_PER_SEC - 1
        if self.groups == 3:
            return 86_400 * US_PER_SEC - 1
        return (10 ** self.day_w) * 86_400 * US_PER_SEC - 1


def _shape_for(span_us: int) -> _Shape:
    seconds = max(0, int(span_us)) // US_PER_SEC
    groups = 1
    for threshold in _GROUP_SECONDS:
        if seconds >= threshold:
            groups += 1
    return _Shape(groups=groups, day_w=max(1, len(str(seconds // 86_400))))


def _relative(delta_us: int, shape: _Shape) -> str:
    """
    A signed offset, fixed width, with unused leading groups blanked.

    `      +1:23.456789` rather than `+000:00:01:23.456789`: the width is
    constant so the column cannot move, but the leading groups are spaces
    rather than zeros, so the *magnitude* of a gap is legible at a glance down
    the column -- which is the entire reason a reader turns delta mode on. A
    stall shows up as a row whose number starts further left.

    The seconds group is never blanked: it carries the fraction, which is the
    one part of a delta that is always meaningful.

    A value too large for `shape` **widens the column rather than losing its
    top group**. The shape comes from the span, so this should not arise -- no
    delta within a log can exceed the log's own span -- but "should not arise"
    is not a reason to print 83 seconds as `23.456789`. A momentarily ragged
    column is a visible oddity; a quietly truncated interval is a wrong number
    in evidence.
    """
    sign = "-" if delta_us < 0 else "+"
    us = abs(int(delta_us))
    if us > shape.covers:
        # One integer comparison on the path every row takes, and the
        # re-shaping only on the path no row should ever take.
        need = _shape_for(us)
        shape = _Shape(groups=max(need.groups, shape.groups),
                       day_w=max(need.day_w, shape.day_w))
    secs, micro = divmod(us, US_PER_SEC)
    minutes, second = divmod(secs, 60)
    hours, minute = divmod(minutes, 60)
    days, hour = divmod(hours, 24)

    higher: list[tuple[int, int]] = []
    if shape.groups >= 4:
        higher.append((days, shape.day_w))
    if shape.groups >= 3:
        higher.append((hour, 2))
    if shape.groups >= 2:
        higher.append((minute, 2))

    out: list[str] = []
    started = False
    for value, width in higher:
        started = started or value > 0
        out.append(f"{value:0{width}d}:" if started else " " * (width + 1))
    body = "".join(out) + f"{second:02d}.{micro:06d}"

    # The sign goes immediately before the first digit, so it reads as a
    # quantity rather than as a column of plus signs down the left edge.
    stripped = body.lstrip(" ")
    return " " * (len(body) - len(stripped)) + sign + stripped
