"""
Visual constants for the Qt shell -- SPEC 9.

Separate from `session.palette` on purpose. The palette holds the *meaningful*
colours -- severity and source identity -- which are Qt-free so they can be
tested headlessly and later drive an HTML export. This module holds chrome: the
colours and metrics that only matter because there is a widget.
"""

from __future__ import annotations

from string import Template

MONO_FAMILY = "Cascadia Mono, Consolas, DejaVu Sans Mono, Menlo, monospace"
"""
A fallback chain rather than one family.

Cascadia ships with Windows Terminal and recent Windows, Consolas with
everything back to Vista, and the rest cover macOS and Linux. Qt resolves the
first that exists, which keeps the Windows-first focus without making the other
platforms fall back to a proportional font.
"""

FONT_POINT = 10
"""
Default text size, and the one the application ships with.

The user's own size is remembered in `ui.settings`; this stays the fallback and
what "reset" returns to.
"""

LANE_WIDTH_DEFAULT = 7
LANE_WIDTH_MIN = 4
LANE_WIDTH_MAX = 12
"""
Source-lane width in pixels, FR-28's configurable 4-12.

Four is the narrowest that still reads as a colour rather than a line; twelve is
where twenty sources start taking more width than the message can spare.
"""

LANE_COMPRESS_ABOVE = 20
LANE_WIDTH_COMPRESSED = 2
"""
Beyond this many sources the lanes compress and the alias column becomes the
primary cue (SPEC 9.2). Twenty lanes at full width is already 160 pixels; at
forty the gutter would be wider than most of the messages.
"""

ROW_PADDING = 2
"""Pixels added to the font height for the row pitch. Legibility against density."""

WHEEL_LINES = 3
MULTILINE_MARK = "⏎ "   # RETURN SYMBOL, for an entry with continuation lines

ASSUMED_COLUMNS = 400
"""
Horizontal scroll range, in characters, for FR-36a.

The honest number is the longest line in the merged view, which would mean
tracking a maximum over every indexed entry. That is a real cost on the index
for a cosmetic benefit, so instead the range is generous and fixed: stack traces
and SQL statements run long, but 400 columns covers them, and a line past it
simply clips rather than breaking anything.
"""

BACKGROUND = "#16181d"
FOREGROUND = "#d4d7dd"
DIM = "#6b7280"
GRID = "#2a2e36"
SELECTION = "#243043"

TIMESTAMP = "#7f8794"
"""
The parsed timestamp column: grey, and deliberately quieter than the message.

It is the same value on every row and differs only in its last few digits, so
at full contrast it is a column of noise competing with the text the reader is
actually scanning. Grey keeps it readable when looked at and invisible when not.
"""

TIMESTAMP_INFERRED = "#5c6470"
"""
Quieter still, for a timestamp completed from `mtime` rather than read from the
file (SPEC 4.5). A guess and a fact should not look identical.
"""
STATUS_WARN = "#d9a33c"
STATUS_ERROR = "#e06c4f"

CHROME = "#1b1e24"
HANDLE = "#3a3f4a"
HANDLE_HOVER = "#4d535f"

# `string.Template` rather than an f-string or `%`: QSS is mostly braces, so
# `.format()` would need every one of them doubled, and `$name` collides with
# nothing in the QSS grammar.
QSS = Template("""
QMainWindow, QWidget { background: $bg; color: $fg; }
QStatusBar { background: $chrome; color: $dim; border-top: 1px solid $grid; }
QStatusBar::item { border: none; }
QMenuBar { background: $chrome; color: $fg; }
QMenuBar::item:selected { background: $sel; }
QMenu { background: $chrome; color: $fg; border: 1px solid $grid; }
QMenu::item:selected { background: $sel; }
QDockWidget { color: $dim; titlebar-close-icon: none; titlebar-normal-icon: none; }
QListWidget { background: $bg; color: $fg; border: none; outline: none; }
QListWidget::item { padding: 3px 4px; }
QListWidget::item:selected { background: $sel; color: $fg; }
QScrollBar:vertical { background: $bg; width: 13px; margin: 0; }
QScrollBar:horizontal { background: $bg; height: 13px; margin: 0; }
QScrollBar::handle { background: $handle; border-radius: 3px; min-height: 24px; }
QScrollBar::handle:hover { background: $handle_hover; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QToolTip { background: $chrome; color: $fg; border: 1px solid $grid; }
""").substitute(
    bg=BACKGROUND,
    fg=FOREGROUND,
    dim=DIM,
    grid=GRID,
    sel=SELECTION,
    chrome=CHROME,
    handle=HANDLE,
    handle_hover=HANDLE_HOVER,
)
