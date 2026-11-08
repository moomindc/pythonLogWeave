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

from PySide6.QtCore import QSettings

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
