"""
The two overview strips as widgets -- SPEC 8, FR-32, FR-33, FR-34, D4.

Deliberately thin. Everything a strip *means* -- which pixel covers which rows,
what is in it, what colour that comes out as -- is decided in
`session.overview`, which is Qt-free and tested on its exact pixels. What is
left here is a `QImage` wrapped round that buffer, one `drawImage`, the
viewport box, and the mouse.

That split is what makes SPEC 8's claim literally true: **a repaint is a blit.**
The alternative shape, a `fillRect` per severity segment per pixel row, is up
to 6400 calls for an 800-pixel strip -- a measurable share of a 16 ms frame
(NFR-5) spent on a widget that is not even the one being read. Risk R3 was that
two strips become the frame-budget bottleneck; a blit is how they do not.

## Why these are plain `QWidget`s and not scroll areas

Neither strip scrolls. Both are always the full height of the view and always
show the whole merged stream -- that is what an overview is. The minimap
*drives* the view's scrollbar (SPEC 8.3) and draws a box where the viewport
currently is, but it has no scroll position of its own, so there is nothing for
a `QAbstractScrollArea` to manage.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..index.civil import format_wall_us
from ..index.severity import Severity
from ..session import Session
from ..session.overview import Strip, StripTheme, render_rgba
from . import theme


class OverviewStrip(QWidget):
    """
    Shared behaviour of the two strips: paint a buffer, seek on a drag.

    Subclasses answer three questions -- which aggregation to draw, what a
    pixel means when it is clicked, and what to draw on top -- and nothing
    else.
    """

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self._image: QImage | None = None
        self._buffer: np.ndarray | None = None
        """
        The strip's pixels, kept alive for as long as the `QImage` over them.

        `QImage` built on a buffer does **not** copy it and does not own it, so
        letting this fall out of scope leaves the image pointing at freed memory
        -- which on Windows is a silent wrong-pixels bug most of the time and an
        access violation the rest.
        """
        self._drawn: Strip | None = None
        self._top_row = 0
        self._page_rows = 1
        self._marker_time: int | None = None

        self.setFixedWidth(theme.STRIP_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._box = QColor(theme.STRIP_VIEWPORT_BOX)
        self._fill = QColor(theme.STRIP_VIEWPORT_FILL)
        self._marker = QColor(theme.STRIP_MARKER)
        self._separator = QColor(theme.STRIP_SEPARATOR)
        self._theme = StripTheme()

    # ------------------------------------------------------------------
    # what the subclasses fill in
    # ------------------------------------------------------------------

    reverse = False
    """Whether bands are drawn from the right edge. See `render_rgba`."""

    def strip(self, height: int) -> Strip:
        raise NotImplementedError

    def _seek_to(self, pixel: int) -> None:
        raise NotImplementedError

    def _paint_overlay(self, painter: QPainter, strip: Strip) -> None:
        """Anything drawn over the bands. Nothing, by default."""

    # ------------------------------------------------------------------
    # state the view pushes in
    # ------------------------------------------------------------------

    def set_viewport(self, top_row: int, page_rows: int) -> None:
        """
        Where the merged view currently is.

        Pushed in rather than read out of the view, so the strips depend on the
        session and not on each other or on `MergedLogView` -- which is what
        lets them be tested with a session and no view at all.
        """
        top_row = max(0, int(top_row))
        page_rows = max(1, int(page_rows))
        if (top_row, page_rows) == (self._top_row, self._page_rows):
            return
        self._top_row, self._page_rows = top_row, page_rows
        # No `refresh()`: the aggregation has not changed, only the box over
        # it, and the overview's memo would return the same Strip anyway. This
        # is the path a scroll takes, so it must not re-aggregate.
        self.update()

    def refresh(self) -> None:
        """The session changed: drop the cached image and repaint."""
        self._image = None
        self._buffer = None
        self._drawn = None
        self.update()

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        painter = QPainter(self)
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(theme.BACKGROUND))
        if h <= 0 or w <= 1:
            return

        bands = max(1, w - 1)      # one column reserved for the separator rule
        if self.session.overview.available:
            strip = self.strip(h)
            self._blit(painter, strip, bands, h)
            self._paint_overlay(painter, strip)

        painter.setPen(self._separator)
        painter.drawLine(w - 1, 0, w - 1, h)

    def _blit(self, painter: QPainter, strip: Strip, width: int, height: int) -> None:
        """
        Render the aggregation into a buffer and draw it in one call.

        The buffer is rebuilt whenever its shape changes or the session has been
        refreshed, and reused otherwise -- so a scroll, which only moves the
        viewport box, costs one `drawImage` and no NumPy at all.
        """
        stale = (self._buffer is None
                 or self._drawn is not strip
                 or self._buffer.shape[:2] != (height, width))
        if stale:
            self._buffer = np.ascontiguousarray(
                render_rgba(strip, width, theme=self._theme, reverse=self.reverse))
            self._image = QImage(self._buffer.data, width, height,
                                 self._buffer.strides[0],
                                 QImage.Format.Format_RGBA8888)
            # Identity, not equality: the overview memoises its aggregations, so
            # the *same* Strip object comes back for as long as the data behind
            # it has not changed. That makes "is this buffer still current" a
            # pointer comparison rather than a diff of two count arrays.
            self._drawn = strip
        if self._image is not None:
            painter.drawImage(0, 0, self._image)

    # ------------------------------------------------------------------
    # the mouse (FR-32, FR-33)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._seek_to(int(event.position().y()))
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """
        Drag to scrub. Click and drag are the same gesture on both strips
        (FR-32), so a user can skim the whole stream without letting go.
        """
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._seek_to(int(event.position().y()))
            event.accept()
        else:
            self.setToolTip(self._tooltip(int(event.position().y())))

    def _tooltip(self, pixel: int) -> str:
        """
        What is under the cursor. The strips are 64 pixels wide, which is no
        room for an axis, so the time and the counts are read out on demand
        instead of labelled permanently.
        """
        h = self.height()
        if h <= 0 or not self.session.overview.available:
            return ""
        strip = self.strip(h)
        p = max(0, min(pixel, strip.height - 1))
        when = format_wall_us(int(strip.times[p]), micros=False)
        total = int(strip.counts[p].sum())
        if not total:
            return f"{when}\nnothing logged"
        parts = [f"{Severity(i).name.lower()} {int(c):,}"
                 for i, c in enumerate(strip.counts[p]) if c]
        where = ""
        if strip.rows is not None:
            where = f"\nrow {int(strip.rows[p]) + 1:,}"
        return f"{when}{where}\n{total:,} entries: " + ", ".join(reversed(parts))


class MinimapStrip(OverviewStrip):
    """
    The row-proportional minimap -- SPEC 8.3, FR-32.

    A scrollbar you can read: pixel `p` covers an equal share of the merged
    rows, so dragging it moves linearly through the stream and the viewport box
    is drawn where the view actually is. This is the strip that drives
    scrolling; the timeline deliberately does not.
    """

    rowSought = Signal("qlonglong")
    """
    `qlonglong`, not `int`. See `TimelineStrip.timeSought` -- the row ceiling
    (NFR-15) is inside a 32-bit int by 7%, which SPEC 9.1 already refuses to
    call a margin for a number this class does arithmetic on.
    """

    def strip(self, height: int) -> Strip:
        return self.session.overview.minimap(height)

    def _seek_to(self, pixel: int) -> None:
        """
        Centre the viewport on the clicked pixel rather than putting it at the
        top.

        Clicking a mark in an overview means "take me there", and there is more
        of "there" above the mark than a top-aligned seek would show. Mapped
        through the strip's own row edges, so the row a click lands on is the
        row that pixel was drawn from.
        """
        h = self.height()
        if h <= 0 or not self.session.total_rows:
            return
        row = self.session.overview.row_at_pixel(pixel, h)
        self.rowSought.emit(max(0, row - self._page_rows // 2))

    def _paint_overlay(self, painter: QPainter, strip: Strip) -> None:
        """
        The viewport box (FR-32).

        Given a minimum height of two pixels: on a billion-row merge a
        sixty-row screenful is a ten-thousandth of a pixel, and a box that
        rounds away is a box that tells the reader nothing about where they are.
        """
        total = self.session.total_rows
        if not total:
            return
        ov = self.session.overview
        h = strip.height
        top = ov.pixel_of_row(self._top_row, h)
        bottom = ov.pixel_of_row(self._top_row + self._page_rows, h)
        box = QRect(0, top, max(1, self.width() - 1), max(2, bottom - top))
        painter.fillRect(box, self._fill)
        painter.setPen(self._box)
        painter.drawRect(box.adjusted(0, 0, -1, -1))


class TimelineStrip(OverviewStrip):
    """
    The wall-clock timeline -- SPEC 8.2, FR-33.

    The vertical axis is time across the merged span, so an idle hour is an
    inch of blank strip and a burst is a dense band. That is the one thing a
    row-proportional minimap cannot show: a row axis has no rows in the gap to
    draw the gap with.

    A seek control only. It does not drive the scrollbar, because a drag down
    the timeline advances through the stream at wildly uneven row rates and a
    scrollbar that behaved that way would be broken rather than informative.
    """

    reverse = True
    """
    Bands grow leftward, away from the minimap beside it.

    The two strips sit side by side with their quiet edges together, so a dense
    band in each reads as one shape growing outward from the middle rather than
    as two columns that happen to be adjacent.
    """

    timeSought = Signal("qlonglong")
    """
    `qlonglong`, because a `wall_us` does not fit in a C++ `int`.

    PySide6 maps `Signal(int)` onto a 32-bit `int`, and a wall time is around
    1.8e15 -- six orders of magnitude past it. The first version of this used
    `Signal(int)` and the seek landed at row 0 every time, with the truncation
    reported only as a `RuntimeWarning` on stderr that a release build would
    not print at all. SPEC 9.1 makes this exact argument about row numbers and
    `QModelIndex.row()`; a timestamp is further over the line than a row.
    """

    def strip(self, height: int) -> Strip:
        return self.session.overview.timeline(height)

    def _seek_to(self, pixel: int) -> None:
        h = self.height()
        if h <= 0 or not self.session.total_rows:
            return
        self.timeSought.emit(self.session.overview.time_at_pixel(pixel, h))

    def _paint_overlay(self, painter: QPainter, strip: Strip) -> None:
        """
        Where the view is, as a line rather than a box.

        A box would be a lie here: the viewport is a row range, and the span of
        *time* it covers is whatever those rows happen to cover -- a second
        during a burst, an hour during a quiet patch. A single line says "you
        are here" without claiming to say how much you can see.
        """
        if self._marker_time is None:
            return
        y = self.session.overview.pixel_of_time(self._marker_time, strip.height)
        painter.setPen(self._marker)
        painter.drawLine(0, y, self.width() - 2, y)

    def set_marker_time(self, wall_us: int | None) -> None:
        if wall_us == self._marker_time:
            return
        self._marker_time = wall_us
        self.update()
