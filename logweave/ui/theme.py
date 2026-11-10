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

STRIP_WIDTH = 64
"""
Width of each overview strip in pixels (SPEC 8, FR-32, FR-33).

Wide enough that a stacked severity band is readable -- eight classes at the
two-pixel minimum need sixteen before any proportion shows at all -- and narrow
enough that both strips together cost less than a tenth of a 1280px window.
"""

STRIP_VIEWPORT_BOX = "#8c99ad"
STRIP_VIEWPORT_FILL = "#59243850"
"""
The minimap's viewport box (FR-32): a light outline over a translucent fill.

Translucent, not opaque: the box sits *over* the severity bands and the point
of the minimap is that you can see what is inside the box as well as where it
is.

`#AARRGGBB`, which is Qt's order for an eight-digit colour and not the
`#RRGGBBAA` of CSS -- written the CSS way it is a 14%-opaque teal rather than a
35%-opaque navy, which looks enough like a deliberate choice to survive review.
"""

STRIP_MARKER = "#d4d7dd"
"""Where the view currently is, on the timeline. A single bright line."""

STRIP_SEPARATOR = "#2a2e36"
"""One-pixel rule between the strips, so two dark columns read as two."""

STRIP_EXCLUDED = "#9916181d"
"""
Time a filter has put aside, on the timeline (FR-45, FR-48).

`#AARRGGBB` like `STRIP_VIEWPORT_FILL`, and for the same reason translucent:
the shade has to say "these rows are not in the view" while still letting the
reader see how busy the part they excluded was -- which is how they decide
whether the window they dragged is the one they meant.

Shade over what is *out* rather than a box round what is in: the rows outside
the window are gone from the view, so the timeline is the only place left that
can show them at all.
"""

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

FOLLOW_ACTIVE = "#6fbf73"
"""
The colour of "following", in the status bar and on the new-lines indicator.

Green rather than the accent blue the selection uses, because following is a
*state of the world* -- lines are arriving -- and not a thing the user has
selected. A reader glancing at the window should be able to tell at once
whether what they are looking at is still moving.
"""

FIND_MARK = "#ffc857"
"""
A search hit -- in the view, and on both strips (FR-44).

Amber, and used for nothing else. It has to be distinguishable from every
severity colour and from any rule a user has written, because a find is
transient and the rules are not: a reader hunting a string needs to tell the
thing they just typed from the colours that were already there.
"""

FIND_CURRENT = "#ff8c42"
"""
The hit the cursor is on, as against the others. One step warmer than
`FIND_MARK`, so "which of these is the one I am on" is answerable at a glance
without a second mechanism.
"""

BOOKMARK = "#b48ead"
"""
A bookmarked row -- on both strips and in the status bar (FR-37).

Violet, which nothing else in this window uses. A bookmark has to be told from
a find hit at a glance, because the two are used together: a reader searches,
marks the three lines that matter, and then searches again. If the marks and
the hits were both amber the second search would erase the record of the first
without changing a pixel.
"""

FILTER_ACTIVE = "#5fa8d3"
"""
The status-bar colour for "a filter is in force" (FR-45).

Blue, where following is green: both are states of the world rather than
severities, and a reader who is looking at a *subset* of the log needs that
fact to be visible without reading the text. A view that is quietly hiding rows
is the one place this application can mislead someone badly.
"""

STALE = "#8a6a4a"
"""
FR-25: a source that is gone or unreachable. Muted brown, not red.

Red is for a severity in the log, and a vanished share is not an error in the
file -- the content already read is still perfectly good. It needs to read as
"this one has stopped" rather than as "something is wrong with this line".
"""

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
/* The find and filter bars (FR-44, FR-45). Styled here rather than in the
   widgets, so they look like the rest of the window without each one carrying
   its own stylesheet -- and so a light theme is one substitution away. */
QLineEdit { background: $bg; color: $fg; border: 1px solid $grid;
            border-radius: 3px; padding: 2px 4px; selection-background-color: $sel; }
QLineEdit:focus { border: 1px solid $handle_hover; }
QComboBox { background: $bg; color: $fg; border: 1px solid $grid;
            border-radius: 3px; padding: 1px 4px; }
QComboBox QAbstractItemView { background: $chrome; color: $fg;
            selection-background-color: $sel; }
QPushButton { background: $chrome; color: $fg; border: 1px solid $grid;
            border-radius: 3px; padding: 2px 8px; }
QPushButton:hover { background: $sel; }
QPushButton:checked { background: $sel; border: 1px solid $handle_hover; }
QPushButton:disabled { color: $dim; }
QToolButton { background: transparent; color: $fg; border: none; padding: 2px 5px; }
QToolButton:hover { background: $sel; border-radius: 3px; }
QToolButton:checked { background: $sel; border-radius: 3px; }
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
