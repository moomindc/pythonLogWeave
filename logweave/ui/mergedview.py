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

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from ..index.civil import format_wall_us
from ..index.severity import Severity
from ..session import Session
from ..session import palette as pal
from ..session.rowformat import RowFormatter
from . import theme

SCROLL_UNITS_MAX = 1_000_000_000
"""
Largest value put into a Qt scrollbar.

Qt scrollbars are `int`, so the hard ceiling is 2^31-1. NFR-15's two billion
rows would fit with 7% to spare, which is not enough margin for a widget that
also computes page steps and products internally. Past this, the scrollbar is
scaled and the mapping is done here in Python ints.
"""

LANE_WIDTH = 7
LANE_GAP = 1
MARK_GUTTER = 6
PAD = 8

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
    currentRowChanged = Signal(int)
    copied = Signal(int)
    copyRefused = Signal(int)
    timestampVisibilityChanged = Signal(bool)

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self._current_row = 0
        self._anchor_row = 0
        self._top_row = 0
        self._paint_count = 0
        self._last_painted = 0
        self._show_timestamp = True
        self._lane_colours: list[QColor] = []

        font = QFont(theme.MONO_FAMILY, theme.FONT_POINT)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.setFont(font)
        self._metrics = QFontMetrics(font)
        self._row_h = self._metrics.height() + theme.ROW_PADDING
        self._char_w = self._metrics.horizontalAdvance("0")
        # The timestamp column is a fixed width because the format is fixed;
        # measuring the widest rendered stamp once avoids reflow while scrolling.
        self._ts_w = self._metrics.horizontalAdvance("2026-10-04 14:22:08.113456") + PAD

        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.viewport().setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._cache_colours()
        self.refresh()

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------

    def _cache_colours(self) -> None:
        self._bg = QColor(theme.BACKGROUND)
        self._fg = QColor(theme.FOREGROUND)
        self._dim = QColor(theme.DIM)
        self._grid = QColor(theme.GRID)
        self._sel = QColor(theme.SELECTION)
        self._stamp = QColor(theme.TIMESTAMP)
        self._stamp_inferred = QColor(theme.TIMESTAMP_INFERRED)
        self._sev = {k: QColor(v) for k, v in pal.SEVERITY_COLOURS.items()}
        # A row with no severity word is ordinary text, so it is painted in the
        # ordinary foreground. The palette's NONE grey is for the severity mark
        # and the minimap, where it means "nothing here"; applied to the message
        # it makes every unclassified line -- most of a syslog -- look disabled.
        self._sev[int(Severity.NONE)] = QColor(theme.FOREGROUND)
        self._sev_bg = {k: QColor(v) for k, v in pal.SEVERITY_BACKGROUNDS.items()}

    @property
    def row_height(self) -> int:
        return self._row_h

    def visible_rows(self) -> int:
        return max(1, self.viewport().height() // self._row_h)

    def _lane_area(self) -> int:
        k = len(self.session.sources)
        return k * (LANE_WIDTH + LANE_GAP) + PAD if k else 0

    @property
    def message_left(self) -> int:
        """Where the message column starts. Everything left of it is frozen."""
        return MARK_GUTTER + PAD + (self._ts_w if self._show_timestamp else 0)

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
        return max(0, self.viewport().width() - self._lane_area() - self.message_left)

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

    def copy_selection(self) -> None:
        """
        Copy the selected rows with attribution (D9, FR-46a).

        Capped, because "select all" on a billion-row merge would otherwise try
        to decode the whole thing into one clipboard string. The cap is a
        deliberate refusal rather than a silent truncation -- M7's export
        (FR-47) is the right tool for a whole view, and it streams.
        """
        lo, hi = self.selection()
        count = hi - lo + 1
        if count > COPY_ROW_LIMIT:
            self.copyRefused.emit(count)
            return
        rows = self.session.rows(lo, count)
        text = RowFormatter(self.session).format(rows)
        if text:
            QGuiApplication.clipboard().setText(text)
            self.copied.emit(len(rows))

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

        lane_area = self._lane_area()
        text_right = self.viewport().width() - lane_area
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

        # Pass 1 -- row backgrounds and the severity mark, which is a solid bar
        # in the left gutter so severity is findable by eye without reading the
        # level word (FR-11).
        painter.setClipRect(QRect(0, 0, text_right, height))
        for i, r in enumerate(rows, start=first):
            y = i * row_h
            if sel_lo <= r.row <= sel_hi:
                painter.fillRect(QRect(0, y, text_right, row_h), self._sel)
            else:
                tint = self._sev_bg.get(r.severity)
                if tint is not None:
                    painter.fillRect(QRect(0, y, text_right, row_h), tint)
            if r.severity:
                painter.fillRect(
                    QRect(0, y, MARK_GUTTER - 2, row_h),
                    self._sev.get(r.severity, self._dim),
                )

        # Pass 2 -- timestamps, frozen against horizontal scroll. Dimmed when the
        # timestamp was inferred rather than read from the file (SPEC 4.5),
        # because that is a real difference in confidence.
        if self._show_timestamp:
            painter.setClipRect(QRect(0, 0, msg_left, height))
            stamp_x = MARK_GUTTER + PAD
            normal, inferred = self._stamp, self._stamp_inferred
            for i, r in enumerate(rows, start=first):
                painter.setPen(inferred if r.inferred else normal)
                painter.drawText(
                    stamp_x, i * row_h + baseline_adjust, format_wall_us(r.ts),
                )

        # Pass 3 -- messages, the only thing that scrolls horizontally (FR-36a).
        painter.setClipRect(QRect(msg_left, 0, msg_width, height))
        trim = self.trims_timestamp()
        for i, r in enumerate(rows, start=first):
            painter.setPen(self._sev.get(r.severity, self._fg))
            painter.drawText(
                msg_x, i * row_h + baseline_adjust,
                self._row_text(r, trim=trim)[skip:skip + span],
            )

        # Pass 4 -- source lanes, right-hand gutter, also frozen (FR-28, 9.2).
        painter.setClipping(False)
        lane_x = text_right + PAD
        for i, r in enumerate(rows, start=first):
            y = i * row_h
            for s_i, colour in enumerate(lanes):
                x = lane_x + s_i * (LANE_WIDTH + LANE_GAP)
                if s_i == r.source:
                    painter.fillRect(QRect(x, y, LANE_WIDTH, row_h), colour)
                else:
                    painter.fillRect(
                        QRect(x, y + row_h // 2, LANE_WIDTH, 1), self._grid
                    )

        painter.end()

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
