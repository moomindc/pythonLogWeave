"""
Remembering the view preferences between runs -- FR-38, FR-28, FR-29a.

Small and deliberately separate from the profiles of FR-49 (M8). These are
*application* preferences -- how big the text is, which columns are on -- and
they should survive a restart whatever file is open. A profile is about a
particular set of logs: which files, which skews, which filters. Conflating the
two would mean opening a colleague's profile silently resized your text.

`QSettings` rather than a file of our own: it is the registry on Windows, a
plist on macOS and an ini under `$XDG_CONFIG_HOME` on Linux, each the place a
user of that platform expects, and none of it is ours to get wrong.

Nothing here is required for the application to run. A missing, unreadable or
hand-corrupted value falls back to the shipped default, because an unusable
preference must not be able to stop someone opening a log.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from ..session import timefmt
from . import theme
from .mergedview import FONT_MAX, FONT_MIN

ORGANISATION = "LogWeave"
APPLICATION = "LogWeave"

FONT_POINT = "view/font_point"
SHOW_TIMESTAMP = "view/show_timestamp"
SHOW_ALIAS = "view/show_alias"
SHOW_LINE_NO = "view/show_line_no"
LANE_WIDTH = "view/lane_width"
HIGHLIGHTS = "view/highlights"
SHOW_MINIMAP = "view/show_minimap"
SHOW_TIMELINE = "view/show_timeline"
"""
FR-34: either overview strip can be collapsed and the choice persists.

Two keys rather than one tri-state, because the two strips are independent --
D4's point is that they answer different questions, so "minimap only" and
"timeline only" are both ordinary configurations rather than steps on a scale.
"""


TIME_MODE = "view/time_mode"
TIME_PATTERN = "view/time_pattern"
"""
FR-35: how the timestamp column reads, and in absolute mode with what pattern.

The **zero point** of elapsed mode is deliberately not here. It is a position
in the log that is open -- "measure from the deploy" -- and carrying last
week's zero point into this morning's incident would be meaningless; worse, it
would be invisible, because every row would simply read a few days out. The
mode persists, the origin resets to the first row (`timefmt.ZERO_FIRST_ROW`)
and a profile (FR-49) is where a *named* reading of a *named* set of files
belongs.
"""


USER_RULES = "highlight/user_rules"
"""
FR-31: the user's own highlight rules, as the JSON `highlight.store` writes.

An *application* preference and not a profile one, deliberately. A profile
(FR-49) is about a particular set of logs -- which files, which skews, which
filters -- and a reader's rule for finding timeouts is about the reader. Opening
a colleague's profile must not take their colours with it, and must not lose
yours.

One value rather than a key per rule: the list has an order, which is FR-31's
tiebreak when two rules share a priority, and a registry has none.
"""


FOLLOW = "view/follow"
"""
FR-24: the global follow toggle, remembered between runs.

Defaults **off**. Following is a thing you ask for -- it starts a watcher
thread, re-indexes on every append, and drags the view to the bottom -- and
having that happen to a log someone opened to read a stack trace in would be
a surprise, not a convenience. Per-file follow flags belong to a profile
(FR-49), not here.
"""


def store() -> QSettings:
    return QSettings(ORGANISATION, APPLICATION)


def read_int(key: str, default: int, *, low: int, high: int) -> int:
    """
    An integer from the store, clamped to a usable range.

    Clamped rather than trusted: the store is a registry key or a text file a
    user may edit, and a font size of 0 or 4000 would produce a window that
    cannot be used to fix itself.
    """
    try:
        value = int(store().value(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def read_bool(key: str, default: bool) -> bool:
    raw = store().value(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        # QSettings ini files round-trip bools as the strings "true"/"false".
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return default


def write(key: str, value: object) -> None:
    store().setValue(key, value)


def font_point() -> int:
    """The remembered text size, defaulting to what the application ships."""
    return read_int(FONT_POINT, theme.FONT_POINT, low=FONT_MIN, high=FONT_MAX)


def lane_width() -> int:
    """FR-28: the source lanes are configurable between 4 and 12 pixels."""
    return read_int(LANE_WIDTH, theme.LANE_WIDTH_DEFAULT,
                    low=theme.LANE_WIDTH_MIN, high=theme.LANE_WIDTH_MAX)


def time_display() -> timefmt.TimeDisplay:
    """
    FR-35: the remembered timestamp format, or the shipped one.

    Both values are checked rather than trusted, for the reason this module's
    docstring gives: a mode this version does not have, or a pattern someone
    hand-edited into nonsense, falls back to the default instead of producing a
    column of `%Q`. The one thing a preference must never do is make the window
    unusable for fixing the preference.
    """
    raw_mode = store().value(TIME_MODE, timefmt.TimeMode.ABSOLUTE.value)
    try:
        mode = timefmt.TimeMode(str(raw_mode))
    except ValueError:
        mode = timefmt.TimeMode.ABSOLUTE
    pattern = str(store().value(TIME_PATTERN, timefmt.FULL) or timefmt.FULL)
    if timefmt.validate_pattern(pattern):
        pattern = timefmt.FULL
    return timefmt.TimeDisplay(mode=mode, pattern=pattern)


def write_time_display(display: timefmt.TimeDisplay) -> None:
    write(TIME_MODE, display.mode.value)
    write(TIME_PATTERN, display.pattern)


# ---------------------------------------------------------------------------
# recent files and recent profiles (FR-54)
# ---------------------------------------------------------------------------

REOPEN_LAST = "session/reopen_last"
"""
FR-54: reopen the last profile on start, off by default.

Off, because a log viewer that opens four hundred megabytes of yesterday's
logs because it was once pointed at them is a slow start nobody asked for.
A reader who works the same investigation for a week turns it on.
"""

RECENT_LOGS = "recent/logs"
RECENT_PROFILES = "recent/profiles"
"""
Two lists, not one.

A reader reopens a *profile* to get back to an investigation and reopens a
*log* to glance at one file, and mixing them puts the thing they want behind
nine of the other kind. They are also stored per machine and never written to a
profile: SPEC 12.3 keeps "where this user's files live" out of a file meant to
be shared, and a recent list is a small history of someone's week.
"""

RECENT_MAX = 10
"""
How many of each are kept.

Ten because the list is a menu: a reader scans it rather than searching it, and
a menu long enough to need scrolling is slower than the open dialog it was
meant to save.
"""


def _recent(key: str) -> list[str]:
    """
    A stored recent list, with anything that is no longer a file dropped.

    Checked against the disk on the way out rather than on the way in, because
    the interesting case is a path that was fine when it was stored: a log on a
    share that is not mounted this morning, or a profile on a stick. Offering
    it and failing is worse than not offering it.
    """
    raw = store().value(key, [])
    if isinstance(raw, str):
        # A single-element list comes back from the registry as a bare string.
        raw = [raw] if raw else []
    out: list[str] = []
    for item in raw or ():
        text = str(item)
        try:
            if Path(text).is_file():
                out.append(text)
        except OSError:
            continue
    return out[:RECENT_MAX]


def _remember(key: str, paths) -> None:
    """Put `paths` at the top of a recent list, most recent first, no repeats."""
    fresh = [str(Path(p)) for p in paths]
    kept = [p for p in _recent(key)
            if not any(_same(p, f) for f in fresh)]
    write(key, (fresh + kept)[:RECENT_MAX])


def _same(a: str, b: str) -> bool:
    """
    Whether two stored paths name the same file, Windows-style.

    Case-folded, because `C:/Logs/a.log` and `c:/logs/a.log` are one file here
    and a recent list that shows both is a list that is wrong twice.
    """
    return a.casefold() == b.casefold()


def recent_logs() -> list[str]:
    return _recent(RECENT_LOGS)


def recent_profiles() -> list[str]:
    return _recent(RECENT_PROFILES)


def remember_logs(paths) -> None:
    _remember(RECENT_LOGS, paths)


def remember_profile(path) -> None:
    _remember(RECENT_PROFILES, [path])


def forget_recent() -> None:
    """Clear both lists -- the recent lists are a history someone may not want."""
    write(RECENT_LOGS, [])
    write(RECENT_PROFILES, [])


def reopen_last() -> bool:
    """FR-54: whether to reopen the most recent profile on start."""
    return read_bool(REOPEN_LAST, False)
