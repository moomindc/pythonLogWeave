"""
`MergedLogView` -- the merged log widget. SPEC 9, FR-28, FR-36a, NFR-5.

A `QAbstractScrollArea` with custom painting rather than a `QTableView`. SPEC 9.1
gives the reasons; two are worth restating because they are not stylistic:

* **Row counts.** `QModelIndex.row()` is a C++ `int`. NFR-15's ceiling is
  2x10^9 rows, which is inside int32 by about 7% -- no margin at all for a view
  that also does arithmetic on row numbers. Painting from an int64 row index
  sidesteps the question entirely.
* **The frame budget.** Painting ~60 rows directly is a known, bounded cost.
  An item view's delegate machinery is not, and NFR-5 is 16 ms.

## Uniform row height is a design decision, not a shortcut

SPEC 9.1 anticipated variable-height rows for multi-line entries. Implementing
that would require a prefix sum of row heights to answer "which row is at pixel
y" -- 8 bytes a row, which is *precisely* the structure the checkpointed merge
exists to avoid (SPEC 6.1: 16 GB at the row ceiling). So rows are a uniform
height, a multi-line entry shows its first line with a marker, and expanding one
is a per-entry affordance rather than a global layout mode. That keeps
row-to-pixel mapping O(1) at any file size.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from ..highlight import RowStyle, Ruleset, Span, Style, default_ruleset
from ..session import Session
from ..session import palette as pal
from ..session.find import Query
from ..session.overview import ORDINARY
from ..session.rowformat import (
    Columns,
    Preset,
    RowFormatter,
    SourceNaming,
)
from ..session.timefmt import TimeFormatter
from . import theme

SCROLL_UNITS_MAX = 1_000_000_000
"""
Largest value put into a Qt scrollbar.

Qt scrollbars are `int`, so the hard ceiling is 2^31-1. NFR-15's two billion
rows would fit with 7% to spare, which is not enough margin for a widget that
also computes page steps and products internally. Past this, the scrollbar is
scaled and the mapping is done here in Python ints.
"""

LANE_GAP = 1
MARK_GUTTER = 6
PAD = 8

ALIAS_CHARS_MAX = 14
"""
Widest alias column, in characters.

An alias is meant to be a short tag -- the shortest part of the filename that
tells it apart (FR-29) -- so a column wide enough for a full path would be the
feature working badly rather than generously. A longer alias is elided.
"""


FONT_MIN = 6
FONT_MAX = 32
"""
Point-size limits for the user's zoom (FR-38).

Below 6 the fixed-pitch advance rounds to the same integer for several sizes,
so a step does nothing visible; above 32 a screenful is a dozen rows and the
merged view stops being one.
"""

SPAN_LIMIT = 32
"""
Styled spans drawn per row.

A pathological rule -- a single character, or `\\w` -- can match hundreds of
times in one line, and each span is a separate `drawText`. The cap keeps a bad
rule from turning one row into the whole frame budget; the spans that survive
are the highest-priority ones, because they are sorted before this is applied.
"""

COPY_ROW_LIMIT = 200_000
"""
Most rows one Ctrl+C will put on the clipboard.

A clipboard is not an export target: at ~100 bytes a row this is already a 20 MB
string, and the row ceiling is 2x10^9. Past this the copy is refused and says so,
rather than appearing to work and silently giving back part of the selection.
"""


class MergedLogView(QAbstractScrollArea):
    """The merged, time-ordered view of every open source."""

    rowsChanged = Signal()
    currentRowChanged = Signal("qlonglong")
    """
    `qlonglong`: a row number is int64 here on purpose (SPEC 9.1), and
    `Signal(int)` would narrow it back to the C++ `int` this view exists to
    avoid -- silently, at the row ceiling, through a signal connection.
    """
    copied = Signal(int)
    copyRefused = Signal(int)
    tailingChanged = Signal(bool)
    """FR-22: auto-scroll started or stopped following the tail."""
    timestampVisibilityChanged = Signal(bool)
    fontPointChanged = Signal(int)

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self._current_row = 0
        self._anchor_row = 0
        self._top_row = 0
        self._paint_count = 0
        self._last_painted = 0
        self._show_timestamp = True
        self._show_alias = False
        self._show_line_no = False
        self._lane_width = theme.LANE_WIDTH_DEFAULT
        self._lane_colours: list[QColor] = []
        self._tailing = False
        """FR-22: whether a follow append should drag the view to the bottom."""
        self.verticalScrollBar().valueChanged.connect(self._user_scrolled)
        self._self_scroll = 0
        """
        Depth of auto-scrolls in progress -- see `scroll_to_tail`.

        FR-22 wants scrolling *up* to pause auto-scroll, and the only evidence
        available is a scrollbar value that moved. Auto-scroll moves it too, so
        without this counter following the tail would switch itself off on the
        first append it handled.
        """
        self._aliases: list[str] = []
        self._alias_w = 0
        self._line_no_w = 0

        self._find: re.Pattern[str] | None = None
        """
        The find term, compiled against *displayed* text (FR-44).

        Deliberately a second match rather than a reuse of `find.Hits`. The
        hits are durable `(source, entry)` pairs -- which entry matched -- and
        painting needs something they do not carry: where in the decoded,
        timestamp-trimmed, possibly elided line the characters are. Re-matching
        is one regex over the ~48 rows on screen, which is nothing next to the
        bytes a scan reads, and it stays correct when the display changes
        (timestamp column toggled, multi-line row collapsed) without the scan
        being told.
        """
        self._find_row = -1
        """The row holding the hit the user is standing on, or -1."""

        self.highlights: Ruleset = default_ruleset()
        """
        The rules the message text is painted with (FR-30, FR-31).

        Replaceable wholesale, because that is what applying an edited rule list
        is: `view.highlights = new_ruleset` then `refresh()`. The view holds no
        derived state from it other than the colour cache, which is keyed by the
        hex strings the rules carry.
        """

        self._time = TimeFormatter()
        """
        How the timestamp column is rendered (FR-35).

        Held as a built formatter rather than as a `TimeDisplay`, because the
        relative modes need the session's span to settle their width and the
        view is not the thing that knows when that has changed -- the window
        builds one with `timefmt.for_span` and hands it over. Set before the
        font, since the column's width is measured from `sample`.
        """

        self._font_point = theme.FONT_POINT
        self._apply_font(theme.FONT_POINT)

        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.viewport().setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._cache_colours()
        self.refresh()

    # ------------------------------------------------------------------
    # font and geometry
    # ------------------------------------------------------------------

    def _apply_font(self, point: int) -> None:
        """
        Set the font and recompute everything measured from it.

        Four faces are built, not one: a `bold` or `italic` highlight rule needs
        a face to draw with, and building them per row would put font
        construction in the frame budget.

        **Bold is only used if it does not change the advance width.** Spans are
        positioned arithmetically from `_char_w` -- the only reason painting is
        independent of line length -- so a bold face a fraction wider would
        shift every span after the first by a growing fraction of a character.
        Most monospace families keep the advance; the ones that do not get
        colour and no weight, which is a far better failure than creeping
        misalignment.
        """
        base = QFont(theme.MONO_FAMILY, point)
        base.setStyleHint(QFont.StyleHint.Monospace)
        base.setFixedPitch(True)
        self.setFont(base)
        # Explicitly on the viewport too. A scroll area's viewport inherits the
        # font when it is first created and then stops: with a style sheet on
        # the window, Qt marks the viewport's font as its own and later
        # propagation is ignored, so a zoom reached `self` and nothing else.
        self.viewport().setFont(base)

        self._font = base
        self._metrics = QFontMetrics(base)
        self._char_w = max(1, self._metrics.horizontalAdvance("0"))
        self._row_h = self._metrics.height() + theme.ROW_PADDING
        # The timestamp column is a fixed width because every format renders to
        # a fixed width (FR-35); measuring the widest rendered stamp once
        # avoids reflow while scrolling.
        self._ts_w = self._metrics.horizontalAdvance(self._time.sample) + PAD

        bold = QFont(base)
        bold.setBold(True)
        italic = QFont(base)
        italic.setItalic(True)
        bold_italic = QFont(bold)
        bold_italic.setItalic(True)
        self._faces = {
            (False, False): base,
            (True, False): bold,
            (False, True): italic,
            (True, True): bold_italic,
        }
        self._can_bold = (
            QFontMetrics(bold).horizontalAdvance("0") == self._char_w
        )
        self._can_italic = (
            QFontMetrics(italic).horizontalAdvance("0") == self._char_w
        )

    def _face(self, bold: bool, italic: bool) -> QFont:
        return self._faces[(bold and self._can_bold, italic and self._can_italic)]

    @property
    def font_point(self) -> int:
        return self._font_point

    def set_font_point(self, point: int) -> None:
        """
        Change the text size, keeping the current row where it is (FR-38).

        The row the user was looking at is restored afterwards rather than the
        scroll offset: fewer rows fit at a larger size, so preserving the pixel
        offset would move the content they were reading off the screen.
        """
        point = max(FONT_MIN, min(FONT_MAX, int(point)))
        if point == self._font_point:
            return
        anchor = self.top_row()
        self._font_point = point
        self._apply_font(point)
        self.refresh()
        self.scroll_to_row(anchor)
        self.fontPointChanged.emit(point)

    def zoom(self, steps: int) -> None:
        self.set_font_point(self._font_point + steps)

    def reset_font(self) -> None:
        self.set_font_point(theme.FONT_POINT)

    def _cache_colours(self) -> None:
        self._bg = QColor(theme.BACKGROUND)
        self._fg = QColor(theme.FOREGROUND)
        self._dim = QColor(theme.DIM)
        self._grid = QColor(theme.GRID)
        self._sel = QColor(theme.SELECTION)
        self._stamp = QColor(theme.TIMESTAMP)
        self._stamp_inferred = QColor(theme.TIMESTAMP_INFERRED)
        # Two maps, because the mark and the message are different surfaces,
        # and both stop colouring at `pal.COLOUR_FROM`.
        #
        # `_sev` paints the severity bar in the left gutter. Below the floor it
        # is the strips' `ORDINARY` grey, and that is not a coincidence: the
        # gutter mark is the per-row form of a minimap band, so the two have to
        # agree -- an INFO row whose mark was saturated blue beside a minimap
        # that draws INFO as ordinary traffic would make the map disagree with
        # the rows it is a map of. The mark is still *drawn* for those rows, so
        # "this line says something about its level" is still visible; it just
        # stops shouting.
        #
        # `_sev_text` is what the *message* is painted in, and below the floor
        # it is the ordinary foreground rather than a grey: a message is read,
        # not scanned, and dimming most of a file would make it look disabled.
        quiet = QColor(*ORDINARY)
        self._sev = {
            k: (QColor(v) if pal.is_loud(k) else quiet)
            for k, v in pal.SEVERITY_COLOURS.items()
        }
        self._sev_text = {
            k: (QColor(v) if pal.is_loud(k) else QColor(theme.FOREGROUND))
            for k, v in pal.SEVERITY_COLOURS.items()
        }
        self._sev_bg = {k: QColor(v) for k, v in pal.SEVERITY_BACKGROUNDS.items()}
        # Rules carry colours as hex strings, so each distinct one is turned
        # into a `QColor` once and kept. A `QColor` per span per row per frame
        # would be thousands of allocations a second for no gain.
        self._rule_colours: dict[str, QColor] = {}

    def _colour(self, hex_str: str) -> QColor:
        got = self._rule_colours.get(hex_str)
        if got is None:
            got = QColor(hex_str)
            self._rule_colours[hex_str] = got
        return got

    @property
    def row_height(self) -> int:
        return self._row_h

    def visible_rows(self) -> int:
        return max(1, self.viewport().height() // self._row_h)

    def _lane_area(self) -> int:
        k = len(self.session.sources)
        return k * (self.lane_pitch + LANE_GAP) + PAD if k else 0

    @property
    def lane_pitch(self) -> int:
        """
        The width one lane actually gets (FR-28, SPEC 9.2).

        Compressed to a hairline past `LANE_COMPRESS_ABOVE` sources, where the
        gutter would otherwise take more room than the messages and the colours
        stop being distinguishable anyway. The alias column is then the way to
        tell files apart, which is what it is for.
        """
        if len(self.session.sources) > theme.LANE_COMPRESS_ABOVE:
            return theme.LANE_WIDTH_COMPRESSED
        return self._lane_width

    @property
    def lane_width(self) -> int:
        return self._lane_width

    def set_lane_width(self, px: int) -> None:
        px = max(theme.LANE_WIDTH_MIN, min(theme.LANE_WIDTH_MAX, int(px)))
        if px == self._lane_width:
            return
        self._lane_width = px
        self.refresh()

    @property
    def show_alias(self) -> bool:
        return self._show_alias

    def set_show_alias(self, show: bool) -> None:
        """
        The source alias as a text column (FR-29).

        On the right, beside the lanes, because that is where the question it
        answers lives: the lane says *which* file in colour, the alias says it
        in words. Off by default -- the lanes already answer it in less space,
        and this is the fallback for when there are too many of them, or when a
        screenshot has to survive being printed in grey.
        """
        show = bool(show)
        if show == self._show_alias:
            return
        self._show_alias = show
        self.refresh()

    @property
    def show_line_no(self) -> bool:
        return self._show_line_no

    def set_show_line_no(self, show: bool) -> None:
        """
        Each entry's line number *within its own file* (FR-29a).

        Not its position in the merged view, which is a number about the viewer
        rather than about the log. Shown on the left, before the timestamp,
        because that is where a line number goes -- an editor gutter, `grep -n`,
        a compiler error. Off by default per FR-29a; the value is always in the
        index and always goes out with an annotated copy, so turning this on
        reveals it rather than enabling it.
        """
        show = bool(show)
        if show == self._show_line_no:
            return
        self._show_line_no = show
        self.refresh()

    def _right_area(self) -> int:
        """Everything frozen on the right: alias, lanes."""
        return self._lane_area() + self._alias_w

    @property
    def _left_gutter(self) -> int:
        """
        Everything frozen on the left, before the timestamp.

        The severity mark, then the line-number column when it is on. A line
        number belongs on the left, where every editor and every `grep -n` puts
        it, and where the eye looks for it without being told.
        """
        return MARK_GUTTER + PAD + self._line_no_w

    @property
    def message_left(self) -> int:
        """Where the message column starts. Everything left of it is frozen."""
        return self._left_gutter + (self._ts_w if self._show_timestamp else 0)

    @property
    def show_timestamp(self) -> bool:
        return self._show_timestamp

    def set_show_timestamp(self, show: bool) -> None:
        """
        Show or hide the parsed timestamp column (SPEC 9.5).

        Worth hiding when every line already carries its own timestamp in a
        consistent position: the column is then a second copy of something
        already on the row, and the width is better spent on the message.
        """
        show = bool(show)
        if show == self._show_timestamp:
            return
        self._show_timestamp = show
        self.refresh()
        self.timestampVisibilityChanged.emit(show)

    @property
    def time_format(self) -> TimeFormatter:
        return self._time

    def set_time_format(self, formatter: TimeFormatter) -> None:
        """
        Change how the timestamp column reads (FR-35).

        Re-measures the column, because a format is allowed to be a different
        width from the last one -- that is most of the reason to change it --
        and the message column starts where this one ends.
        """
        self._time = formatter
        self._ts_w = self._metrics.horizontalAdvance(formatter.sample) + PAD
        self.refresh()

    def trims_timestamp(self) -> bool:
        """
        Whether a repeated leading timestamp is trimmed from the message.

        Tied to the column rather than being a setting of its own, and that
        interlock is the point: trimming only ever removes a *duplicate*, so it
        has to stop the moment the column holding the other copy goes away.
        Otherwise hiding a redundant column would hide the timestamp entirely,
        which is not something hiding a redundant column should ever do.
        """
        return self._show_timestamp

    def message_width(self) -> int:
        """The only part of a row that scrolls horizontally (FR-36a)."""
        return max(0, self.viewport().width() - self._right_area()
                   - self.message_left)

    @property
    def _scroll_scale(self) -> int:
        total = self.session.total_rows
        if total <= SCROLL_UNITS_MAX:
            return 1
        return -(-total // SCROLL_UNITS_MAX)   # ceil

    def refresh(self) -> None:
        """Re-read the row count from the session and resize the scrollbars."""
        total = self.session.total_rows
        # Hoisted out of the paint loop: with k sources the lanes are painted
        # k times a row, so constructing QColors inside would make painting
        # O(rows x sources) in allocations for a palette that never changes.
        self._lane_colours = [QColor(s.colour) for s in self.session.sources]
        self._measure_columns()
        page = self.visible_rows()
        scale = self._scroll_scale

        vbar = self.verticalScrollBar()
        # Rounded *up*, not down. Flooring it means the maximum scrollbar value
        # maps to a top row below `total - page`, which leaves the last
        # `(total - page) % scale` rows of the file unreachable -- you can scroll
        # to within seven rows of the end of an eight-billion-row merge and no
        # further. Rounding up costs at most a partly blank final screen, which
        # `rows()` already handles by returning short.
        last_top = max(0, total - page)
        vbar.setRange(0, -(-last_top // scale))
        vbar.setPageStep(max(1, page // scale))
        vbar.setSingleStep(1)

        # Horizontal scrolling moves the message column alone (FR-36a), so the
        # range is message content against message width -- not against the whole
        # viewport, which would let you scroll a column-width past the end.
        content = self._char_w * theme.ASSUMED_COLUMNS
        hbar = self.horizontalScrollBar()
        hbar.setRange(0, max(0, content - self.message_width()))
        hbar.setPageStep(max(1, self.message_width()))
        hbar.setSingleStep(self._char_w)

        self._top_row = min(self._top_row, max(0, total - 1))
        self.viewport().update()
        self.rowsChanged.emit()

    def _measure_columns(self) -> None:
        """
        Fix the width of the line-number and alias columns once per session
        change, not once per frame.

        Both are measured from the *sources*, not from the visible rows: a
        column that resized as you scrolled past a file with a longer name, or
        into six-figure line numbers, would make the message column jitter and
        reflow the whole row on a wheel step.
        """
        sources = self.session.sources
        self._aliases = [s.alias for s in sources]
        if self._show_alias and self._aliases:
            chars = min(ALIAS_CHARS_MAX, max(len(a) for a in self._aliases))
            self._alias_w = chars * self._char_w + PAD
        else:
            self._alias_w = 0

        if self._show_line_no and sources:
            # Sized to the largest line number in the whole session, so the
            # column is as wide as it will ever need to be from the first frame.
            biggest = max((int(s.index.line_no[-1]) if s.index.n else 0)
                          for s in sources)
            self._line_no_w = (len(f"{biggest:,}")) * self._char_w + PAD
        else:
            self._line_no_w = 0

    def resizeEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        super().resizeEvent(event)
        self.refresh()

    # ------------------------------------------------------------------
    # scrolling
    # ------------------------------------------------------------------

    def top_row(self) -> int:
        return int(self.verticalScrollBar().value()) * self._scroll_scale

    def scroll_to_row(self, row: int) -> None:
        row = max(0, min(int(row), max(0, self.session.total_rows - 1)))
        self.verticalScrollBar().setValue(row // self._scroll_scale)
        self.viewport().update()

    # ------------------------------------------------------------------
    # FR-22: following the tail
    # ------------------------------------------------------------------

    @property
    def tailing(self) -> bool:
        return self._tailing

    def set_tailing(self, on: bool, *, scroll: bool = True) -> None:
        """
        Start or stop auto-scrolling to the tail.

        Turning it on scrolls there at once, because a user who asks to watch
        the end of the log means now and not on the next line written -- a quiet
        log would otherwise leave the request looking ignored.
        """
        on = bool(on)
        if on and scroll:
            self.scroll_to_tail()
        if on == self._tailing:
            return
        self._tailing = on
        self.tailingChanged.emit(on)

    def scroll_to_tail(self) -> None:
        """
        Put the last row on screen, without moving the cursor.

        **The only scroll that is exempt from FR-22's pause**, and the guard is
        here rather than on `scroll_to_row` for a reason: pressing Up at the top
        of the screen, or clicking the minimap, also scrolls programmatically,
        and those are the user asking to look somewhere else. Exempting every
        programmatic scroll would make auto-scroll impossible to pause by any
        means except the scrollbar.
        """
        total = self.session.total_rows
        if not total:
            return
        self._self_scroll += 1
        try:
            self.scroll_to_row(max(0, total - self.visible_rows()))
        finally:
            self._self_scroll -= 1

    def at_tail(self) -> bool:
        """Whether the last row is already on screen."""
        total = self.session.total_rows
        return not total or self.top_row() + self.visible_rows() >= total

    def _user_scrolled(self, value: int) -> None:
        """
        A scrollbar move that this widget did not make -- FR-22.

        Scrolling **up** pauses auto-scroll; scrolling back down to the bottom
        resumes it. Resuming on arrival rather than only on `End` is what makes
        the pause feel like a pause instead of a mode: dragging the scrollbar to
        the end is the same gesture as pressing `End`, and a reader who does it
        is plainly asking to watch the tail again.

        Ingestion is not affected either way. FR-22 is explicit that pausing
        auto-scroll must not stop lines arriving, which is why this touches
        nothing but a flag -- the follow engine never consults it.
        """
        if self._self_scroll:
            return
        if self._tailing and not self.at_tail():
            self._tailing = False
            self.tailingChanged.emit(False)
        elif not self._tailing and self.at_tail() and not self._selecting:
            self._tailing = True
            self.tailingChanged.emit(True)

    @property
    def _selecting(self) -> bool:
        """
        Whether a selection spanning more than one row is in hand.

        Resuming auto-scroll is suppressed while one is: Shift+End, and
        Shift+Down held to the bottom, both land at the tail, and resuming there
        would let the next append scroll the selection the user is still making
        off the top of the screen. Pausing on the way up is not suppressed --
        that is the whole of FR-22 and it has to work during a selection too.
        """
        return self._anchor_row != self._current_row

    def scroll_to_time(self, wall_us: int) -> None:
        """
        NFR-6. The seek is `time_to_row`, which costs ~27 µs and no block replay,
        so the timeline strip (M5) can drag this continuously.
        """
        self.scroll_to_row(self.session.time_to_row(wall_us))

    def current_row(self) -> int:
        return self._current_row

    def set_current_row(self, row: int, *, extend: bool = False) -> None:
        row = max(0, min(int(row), max(0, self.session.total_rows - 1)))
        if not extend:
            self._anchor_row = row
        if row == self._current_row:
            self.viewport().update()
            return
        self._current_row = row
        top = self.top_row()
        page = self.visible_rows()
        if row < top:
            self.scroll_to_row(row)
        elif row >= top + page:
            self.scroll_to_row(row - page + 1)
        self.viewport().update()
        self.currentRowChanged.emit(row)

    def selection(self) -> tuple[int, int]:
        """The selected row range, inclusive, low first."""
        a, b = self._anchor_row, self._current_row
        return (a, b) if a <= b else (b, a)

    def select_all(self) -> None:
        self._anchor_row = 0
        self.set_current_row(max(0, self.session.total_rows - 1), extend=True)

    # ------------------------------------------------------------------
    # the find term (FR-44)
    # ------------------------------------------------------------------

    def set_find(self, query: Query | None, current_row: int = -1) -> None:
        """
        Mark a query's matches in the message text, and emphasise one of them.

        Pushed in by the window alongside `find.Hits`: the hits say which rows
        to visit, this says what to paint. A query that cannot compile marks
        nothing rather than raising -- the find bar is already reporting why.
        """
        pattern = None
        if query is not None and not query.empty and not query.validate():
            body = query.text if query.regex else re.escape(query.text)
            try:
                pattern = re.compile(
                    body, 0 if query.case_sensitive else re.IGNORECASE)
            except re.error:
                pattern = None
        changed = (pattern.pattern if pattern else None) != (
            self._find.pattern if self._find else None)
        self._find = pattern
        self._find_row = int(current_row)
        if changed or current_row >= 0:
            self.viewport().update()

    def set_current_hit(self, row: int) -> None:
        """Which hit is the current one, for the brighter mark. -1 for none."""
        if int(row) != self._find_row:
            self._find_row = int(row)
            self.viewport().update()

    def _with_find(self, st, text: str, row: int):
        """
        `st` with the find's matches laid over its spans.

        Over, not beside: a hit inside a word a rule has already coloured has
        to win, or searching for a string that a built-in rule paints would
        appear to find nothing. So the rule spans are **clipped** around the
        hits rather than merged with them -- `_paint_segments` requires
        disjoint spans in order, and drawing one over another is the
        overdraw this view measured at +10.6 ms a frame.
        """
        found = [(m.start(), m.end()) for m in self._find.finditer(text)
                 if m.end() > m.start()]
        if not found:
            return st
        del found[SPAN_LIMIT:]
        style = Style(bg=theme.FIND_CURRENT if row == self._find_row
                      else theme.FIND_MARK,
                      fg=theme.BACKGROUND, bold=row == self._find_row)
        marks = [Span(a, b, style, rule="find") for a, b in found]

        kept: list[Span] = []
        for sp in st.spans:
            at = sp.start
            for a, b in found:
                if b <= at or a >= sp.end:
                    continue
                if a > at:
                    kept.append(Span(at, a, sp.style, sp.rule))
                at = max(at, b)
            if at < sp.end:
                kept.append(Span(at, sp.end, sp.style, sp.rule))
        spans = tuple(sorted(kept + marks, key=lambda sp: sp.start))
        return RowStyle(line=st.line, spans=spans, rule=st.rule)

    def displayed_columns(self) -> Columns:
        """
        Which columns are on screen, for the `As displayed` preset (FR-46b).

        Asked of the view rather than assembled by the caller, so a copy and an
        export cannot disagree about what "as displayed" means -- and so adding
        a column later is one change here instead of three at the call sites.
        """
        return Columns(timestamp=self._show_timestamp,
                       alias=self._show_alias,
                       line_no=self._show_line_no)

    def copy_as(self, preset: Preset, *, naming: SourceNaming | None = None,
                template: str = "") -> None:
        """
        Copy the selection in one of the M7 presets (FR-46b).

        The columns handed to the formatter are the ones the view is actually
        showing, so `As displayed` pastes what is on screen rather than what
        the default happened to be -- which is the whole point of that preset,
        and the reason `Columns` lives in `rowformat` instead of here.
        """
        lo, hi = self.selection()
        count = hi - lo + 1
        if count > COPY_ROW_LIMIT:
            self.copyRefused.emit(count)
            return
        rows = self.session.rows(lo, count)
        formatter = RowFormatter(
            self.session, preset=preset,
            naming=naming or SourceNaming.BASENAME,
            micros=True, trim_timestamp=self.trims_timestamp(),
            columns=self.displayed_columns(),
            template=template or None,
            time_fmt=self._time)
        text = formatter.format(rows)
        if text:
            QGuiApplication.clipboard().setText(text)
            self.copied.emit(len(rows))

    def copy_selection(self, *, annotated: bool = True) -> None:
        """
        Copy the selected rows, with or without attribution (D9, FR-46a).

        Two modes because they serve different purposes. Annotated carries the
        file and line each row came from, which is the whole reason to merge
        files in the first place -- pasting "line 7 soa.log" into a ticket is
        evidence. Plain gives the lines exactly as they appear in their own
        files, which is what you want when pasting into a parser, a diff, or a
        search box that would choke on the prefix.

        Capped, because "select all" on a billion-row merge would otherwise try
        to decode the whole thing into one clipboard string. The cap is a
        deliberate refusal rather than a silent truncation -- M7's export
        (FR-47) is the right tool for a whole view, and it streams.
        """
        self.copy_as(Preset.ANNOTATED if annotated else Preset.PLAIN)

    def copy_selection_plain(self) -> None:
        """The verbatim lines, no timestamp column and no attribution."""
        self.copy_selection(annotated=False)

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        """
        Blit the viewport and repaint only the strip that was exposed.

        Qt hands this the content delta (old value minus new). Scrolling the
        viewport rather than invalidating it turns a three-row wheel step from a
        56-row repaint into a three-row one -- measured 5.9 ms against 0.4 ms,
        which is the difference between a scroll that is smooth at 4K and one
        that is not (SPEC 14.1).

        It only works when a scrollbar unit is one row and the move is smaller
        than a screen; otherwise there is nothing worth preserving and a full
        repaint is both simpler and no slower.
        """
        if dx or self._scroll_scale != 1 or abs(dy) >= self.visible_rows():
            self.viewport().update()
        else:
            self.viewport().scroll(0, dy * self._row_h)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+wheel zooms, as it does in every editor and browser. Worth
            # having even with the menu items, because changing text size is
            # something people do while reading, not from a menu.
            if steps:
                self.zoom(1 if steps > 0 else -1)
            event.accept()
            return
        lines = int(-steps * theme.WHEEL_LINES)
        if lines:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + lines)
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        extend = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        page = self.visible_rows()
        total = self.session.total_rows

        if ctrl and key == Qt.Key.Key_A:
            self.select_all()
        elif ctrl and key == Qt.Key.Key_C:
            self.copy_selection()
        elif key == Qt.Key.Key_Down:
            self.set_current_row(self._current_row + 1, extend=extend)
        elif key == Qt.Key.Key_Up:
            self.set_current_row(self._current_row - 1, extend=extend)
        elif key == Qt.Key.Key_PageDown:
            self.set_current_row(self._current_row + page, extend=extend)
        elif key == Qt.Key.Key_PageUp:
            self.set_current_row(self._current_row - page, extend=extend)
        elif key == Qt.Key.Key_Home:
            self.set_current_row(0, extend=extend)
        elif key == Qt.Key.Key_End:
            self.set_current_row(total - 1, extend=extend)
            # FR-22 names `End` as the way back to the tail, so it resumes
            # auto-scroll as well as moving the cursor -- but only on a plain
            # press. Shift+End is a selection to the end of the log, and
            # quietly turning following on as a side effect of selecting would
            # then scroll the view out from under the selection being made.
            if not extend:
                self.set_tailing(True)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.set_current_row(self.row_at_point(event.position().y()), extend=extend)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.set_current_row(self.row_at_point(event.position().y()), extend=True)
        event.accept()

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self.viewport())

        # Set before anything is drawn, because **a `QPainter` on a widget does
        # not start with that widget's font**: it starts with the application
        # default, which on Windows is a 9pt proportional UI face. Until this
        # line existed every message was drawn proportionally while every width
        # in this method was computed from the monospace metrics -- and a zoom
        # grew `_row_h` and `_char_w` without changing a single glyph, which is
        # how it was noticed.
        base_face = self._faces[(False, False)]
        painter.setFont(base_face)
        painter.fillRect(event.rect(), self._bg)

        total = self.session.total_rows
        if total == 0:
            self._paint_placeholder(painter)
            return

        # Only the damaged band, not the whole viewport. With `scrollContentsBy`
        # blitting, a wheel step damages three rows and this paints three rows.
        damage = event.rect()
        first = max(0, damage.top() // self._row_h)
        last = damage.bottom() // self._row_h
        top = self.top_row()
        rows = self.session.rows(top + first, last - first + 1)
        self._paint_count += 1
        self._last_painted = len(rows)

        text_right = self.viewport().width() - self._right_area()
        xoff = -int(self.horizontalScrollBar().value())
        baseline_adjust = self._metrics.ascent() + theme.ROW_PADDING // 2
        sel_lo, sel_hi = self.selection()
        lanes = self._lane_colours

        # FR-36a: only the message scrolls horizontally. The marks gutter, the
        # timestamp and the source lanes stay frozen, so the time of a row is
        # still readable when you have scrolled out to column 300 of a JSON
        # record -- which D17 makes an everyday case rather than a corner one.
        msg_left = self.message_left
        msg_width = self.message_width()

        # The visible character window. Legitimate only because the font is
        # fixed-pitch, and it is what makes painting independent of line length:
        # a 4000-character stack trace measured 61 ms a frame drawn whole against
        # 5.6 ms sliced, so this is the difference between meeting NFR-5 and
        # missing it by 4x on data that is entirely ordinary in a log.
        skip = max(0, -xoff // self._char_w)
        span = msg_width // self._char_w + 2
        msg_x = msg_left + xoff + skip * self._char_w

        # Painted in passes, one per clip region, rather than row by row. The
        # timestamp and the message need different clips -- that is what freezing
        # the timestamp means -- and setting a clip per row measured +1.26 ms a
        # frame against setting it per pass. The extra loops cost nothing next to
        # `drawText`; the clip changes did.
        height = self.viewport().height()
        row_h = self._row_h

        # Styled once per row, before the passes. Pass 1 needs the background
        # and pass 3 the foreground, and the scan is memoised per text anyway,
        # but doing it here keeps the two passes in step by construction: a row
        # cannot end up with one rule's background under another rule's text.
        trim = self.trims_timestamp()
        texts = [self._row_text(r, trim=trim) for r in rows]
        styles = [self.highlights.style_of(t) for t in texts]
        if self._find is not None:
            # After the rules, never instead of them: a hit has to be visible
            # on a row a rule has already coloured (see `_with_find`).
            styles = [self._with_find(st, t, r.row)
                      for st, t, r in zip(styles, texts, rows, strict=True)]

        # Pass 1 -- row backgrounds and the severity mark, which is a solid bar
        # in the left gutter so severity is findable by eye without reading the
        # level word (FR-11).
        painter.setClipRect(QRect(0, 0, text_right, height))
        for i, r in enumerate(rows, start=first):
            y = i * row_h
            if sel_lo <= r.row <= sel_hi:
                painter.fillRect(QRect(0, y, text_right, row_h), self._sel)
            else:
                # A rule's own background wins over the severity tint: the user
                # wrote the rule to make these rows stand out, and the tint is
                # a default they are overriding.
                rule_bg = styles[i - first].line.bg
                tint = (self._colour(rule_bg) if rule_bg
                        else self._sev_bg.get(r.severity))
                if tint is not None:
                    painter.fillRect(QRect(0, y, text_right, row_h), tint)
            if r.severity:
                painter.fillRect(
                    QRect(0, y, MARK_GUTTER - 2, row_h),
                    self._sev.get(r.severity, self._dim),
                )

        # Pass 2 -- the line-number gutter: each entry's number within its own
        # file, not its position in the merged view (FR-29a). Right-aligned, so
        # the digits of a six-figure number line up with a three-figure one and
        # the column reads as numbers rather than as text.
        if self._line_no_w:
            painter.setClipRect(QRect(0, 0, msg_left, height))
            painter.setPen(self._dim)
            right_edge = self._left_gutter - PAD
            for i, r in enumerate(rows, start=first):
                digits = f"{r.line_no:,}"
                painter.drawText(right_edge - len(digits) * self._char_w,
                                 i * row_h + baseline_adjust, digits)

        # Pass 3 -- timestamps, frozen against horizontal scroll. Dimmed when the
        # timestamp was inferred rather than read from the file (SPEC 4.5),
        # because that is a real difference in confidence.
        if self._show_timestamp:
            painter.setClipRect(QRect(0, 0, msg_left, height))
            stamp_x = self._left_gutter
            normal, inferred = self._stamp, self._stamp_inferred
            # Delta mode (FR-35) reads the row above the band being painted,
            # which for the top of a damaged band is a row this paint did not
            # fetch. One `row_to_time` for the whole band, served by the merge's
            # block cache, rather than a row of rows nobody is going to draw.
            previous = None
            start_row = top + first
            if self._time.needs_previous and start_row > 0:
                previous = self.session.row_to_time(start_row - 1)
            for i, r in enumerate(rows, start=first):
                painter.setPen(inferred if r.inferred else normal)
                painter.drawText(
                    stamp_x, i * row_h + baseline_adjust,
                    self._time.render(r.ts, previous),
                )
                previous = r.ts

        # Pass 4 -- messages, the only thing that scrolls horizontally (FR-36a),
        # styled by the highlight rules (FR-30).
        painter.setClipRect(QRect(msg_left, 0, msg_width, height))
        for i, r in enumerate(rows, start=first):
            st = styles[i - first]
            text = texts[i - first]
            baseline = i * row_h + baseline_adjust

            # A `line` rule's colour beats the severity colour, for the same
            # reason its background does. With no rule, severity still decides,
            # so a file with highlighting switched off looks as it did at M3.
            pen = (self._colour(st.line.fg) if st.line.fg
                   else self._sev_text.get(r.severity, self._fg))
            face = self._face(st.line.bold, st.line.italic)

            if not st.spans:
                painter.setPen(pen)
                if face is not base_face:
                    painter.setFont(face)
                painter.drawText(msg_x, baseline, text[skip:skip + span])
                if face is not base_face:
                    painter.setFont(base_face)
            else:
                self._paint_segments(painter, st, text, pen, face, base_face,
                                     baseline, msg_x, skip, span, row_h,
                                     i * row_h)

        # Pass 5 -- the frozen right-hand columns: the alias (FR-29) and the
        # source lanes (FR-28, SPEC 9.2). Both answer the same question -- which
        # file is this row from -- which is why they sit together, and why they
        # do not scroll with the message.
        painter.setClipping(False)
        x = text_right

        if self._alias_w:
            painter.setPen(self._dim)
            for i, r in enumerate(rows, start=first):
                alias = self._aliases[r.source] if r.source < len(self._aliases) else ""
                if len(alias) > ALIAS_CHARS_MAX:
                    # Elided from the left: the distinguishing part of a name
                    # is nearly always at the end (`service-2`, `app.3.log`).
                    alias = "\u2026" + alias[-(ALIAS_CHARS_MAX - 1):]
                painter.drawText(x, i * row_h + baseline_adjust, alias)
            x += self._alias_w

        lane_x = x + PAD
        pitch = self.lane_pitch
        for i, r in enumerate(rows, start=first):
            y = i * row_h
            for s_i, colour in enumerate(lanes):
                lx = lane_x + s_i * (pitch + LANE_GAP)
                if s_i == r.source:
                    painter.fillRect(QRect(lx, y, pitch, row_h), colour)
                else:
                    painter.fillRect(
                        QRect(lx, y + row_h // 2, pitch, 1), self._grid
                    )

        painter.end()

    def _paint_segments(self, painter: QPainter, st, text: str, pen, face,
                        base_face, baseline: int, msg_x: int, skip: int,
                        span: int, row_h: int, row_y: int) -> None:
        """
        Draw a row with styled spans, each character exactly once.

        The obvious implementation draws the whole line and then overdraws each
        span on top. It measured **+10.6 ms a frame** on a file averaging six
        exception names a line -- taking a full repaint from 18.6 ms to 29.2 ms
        -- because every span re-rasterises glyphs that were already drawn, and
        M3 established that Qt's raster text cost is per *glyph*, not per call
        (SPEC 9.3). Overdrawing a six-span row therefore pays for that row
        twice.

        So the row is cut into runs instead: the gaps keep the line style, the
        spans get theirs, and the total glyph count is the line length no
        matter how many spans there are. More `drawText` calls, fewer glyphs,
        and M3's measurement says only the glyphs matter.

        Positions are arithmetic from the fixed-pitch advance -- the same
        property that lets the message be sliced to the visible window -- so
        spans outside that window cost nothing beyond being skipped.
        """
        window_end = min(skip + span, len(text))
        if window_end <= skip:
            return

        # Backgrounds first, in one pass: a fill between two `drawText` calls
        # would paint over the text already drawn to its left.
        drawn = 0
        runs: list[tuple[int, int, object]] = []
        at = skip
        for sp in st.spans:
            if drawn >= SPAN_LIMIT:
                break
            start = max(sp.start, skip)
            end = min(sp.end, window_end)
            if end <= start:
                continue
            drawn += 1
            if start > at:
                runs.append((at, start, None))
            runs.append((start, end, sp.style))
            at = end
        if at < window_end:
            runs.append((at, window_end, None))

        # Backgrounds first, and only when one is actually asked for. Walking
        # the runs to fill nothing measured as real time on a row with six
        # spans, and most rules set a colour without a background.
        if any(style is not None and style.bg for _, _, style in runs):
            for start, end, style in runs:
                if style is not None and style.bg:
                    painter.fillRect(
                        QRect(msg_x + (start - skip) * self._char_w, row_y,
                              (end - start) * self._char_w, row_h),
                        self._colour(style.bg),
                    )

        # Pen and font are set only when the run actually changes them. A
        # six-span row is thirteen runs, and most of them want exactly what the
        # run before wanted.
        painter.setPen(pen)
        current_pen, current_face = pen, base_face
        if face is not base_face:
            painter.setFont(face)
            current_face = face

        for start, end, style in runs:
            if style is None:
                run_pen, run_face = pen, face
            else:
                run_pen = self._colour(style.fg) if style.fg else pen
                run_face = self._face(style.bold or st.line.bold,
                                      style.italic or st.line.italic)
            if run_pen is not current_pen:
                painter.setPen(run_pen)
                current_pen = run_pen
            if run_face is not current_face:
                painter.setFont(run_face)
                current_face = run_face
            painter.drawText(msg_x + (start - skip) * self._char_w, baseline,
                             text[start:end])

        if current_face is not base_face:
            painter.setFont(base_face)

    @staticmethod
    def _row_text(row, *, trim: bool = True) -> str:
        """
        What a row shows in the message column.

        Two things happen here. A leading timestamp is trimmed when the column
        beside it is showing that same value (SPEC 9.5). And a multi-line entry
        shows its first physical line with a marker: `drawText` does not wrap on
        a newline, it draws one as a glyph, so passing the whole entry runs the
        continuation lines on to the end of the first, which is both wrong and
        unreadable. Rows are a uniform height by design (SPEC 9.1); the rest of
        an entry is reached by expanding it.
        """
        text = row.message if trim else row.text
        if not row.multiline:
            return text
        head, _, _ = text.partition("\n")
        return theme.MULTILINE_MARK + head.rstrip("\r")

    def _paint_placeholder(self, painter: QPainter) -> None:
        painter.setPen(self._dim)
        rect = self.viewport().rect()
        painter.drawText(
            rect, Qt.AlignmentFlag.AlignCenter,
            "Drop log files here\n\nor use File → Open",
        )
        painter.end()

    # ------------------------------------------------------------------

    def row_at_point(self, y: int) -> int:
        return self.top_row() + int(y) // self._row_h

    def visible_span(self) -> tuple[int, int]:
        return self.top_row(), self.top_row() + self.visible_rows()
