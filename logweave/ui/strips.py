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
from ..session.overview import (
    Strip,
    StripTheme,
    mark_gutter,
    render_density_rgba,
    render_mark_gutter,
    render_rgba,
)
from . import theme

DRAG_PIXELS = 4
"""
Movement that turns a click on the timeline into a range selection (FR-48).

Four, because a click with a mouse is rarely a click with *no* movement -- a
one- or two-pixel twitch on the way up is normal, and at a threshold of one
every seek would also set a filter covering a sliver of time.
"""


def _rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _same_layers(a, b) -> bool:
    """
    Whether two mark-layer tuples are the same layers, by identity.

    By identity on purpose. `MarkLayer` is a frozen dataclass holding NumPy
    arrays, so `==` on two distinct but equal layers compares arrays
    element-wise and then asks for the truth of the result -- which raises
    `ValueError: truth value of an array is ambiguous`, inside `paintEvent`.
    Every layer the strips are given is produced once by a scan and assigned
    wholesale, so identity is both safe and exactly the question being asked:
    has the thing I drew been replaced?
    """
    if b is None or len(a) != len(b):
        return False
    return all(x is y for x, y in zip(a, b, strict=True))


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
        self._marks: tuple = ()
        """
        The user-rule mark layers currently drawn (FR-31a).

        A tuple assigned wholesale, never mutated, so "have the marks changed"
        is an identity test -- the same reason `_drawn` compares `Strip` by
        identity rather than diffing two count arrays.
        """
        self._drawn_marks: tuple | None = None
        self._find = None
        """
        The find's hits as a `MarkLayer` (FR-44), or None.

        Held apart from `_marks` rather than appended to it so that the find
        column is always the rightmost one. A find is transient and the rules
        are not: if it shared the rule gutter its column would move whenever a
        rule was toggled, and a mark that moves for an unrelated reason is
        unreadable even when every pixel in it is right.
        """
        self._find_time: int | None = None
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

    def strip(self, height: int) -> Strip:
        raise NotImplementedError

    def _render_pixels(self, strip: Strip, width: int) -> np.ndarray:
        """
        The strip's pixels. The two subclasses encode different quantities --
        see `render_density_rgba` for why -- so this is theirs to answer.

        Underscored, and not called `render`: `QWidget.render` already exists,
        paints the widget into a paint device, and is what the tests and Qt's
        own grab path call. Shadowing it with something of a different shape
        fails somewhere far away from here.
        """
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

    def set_marks(self, layers) -> None:
        """
        Which user rules are marked on this strip (FR-31a).

        Pushed in, like the viewport: the strips know nothing about rules,
        scans or threads, and a `MarkLayer` is a sorted array of merged
        timestamps plus a colour. Takes effect on the next paint.
        """
        layers = tuple(layers)
        if _same_layers(layers, self._marks):
            return
        self._marks = layers
        self.update()

    def set_find_marks(self, layer) -> None:
        """The find's hits, as the rightmost gutter column (FR-44)."""
        if layer is self._find:
            return
        self._find = layer
        self.update()

    def set_find_current(self, wall_us: int | None) -> None:
        """Merged time of the hit the user is standing on, or None."""
        if wall_us == self._find_time:
            return
        self._find_time = wall_us
        self.update()

    def _gutter_layers(self) -> tuple:
        return self._marks + ((self._find,) if self._find is not None else ())

    def _paint_find_current(self, painter: QPainter, strip: Strip) -> None:
        """
        The current hit, as a line across the strip.

        Positioned through `strip.times` rather than through either strip's own
        row or time mapping, because that array is the axis both strips already
        share -- so this one method is correct on a row-proportional minimap
        and on a wall-clock timeline without knowing which it is on.
        """
        if self._find_time is None or strip.height <= 0:
            return
        y = int(np.searchsorted(strip.times, int(self._find_time)))
        y = max(0, min(y, strip.height - 1))
        painter.setPen(QColor(theme.FIND_CURRENT))
        painter.drawLine(0, y, self.width() - 2, y)

    def refresh(self) -> None:
        """The session changed: drop the cached image and repaint."""
        self._image = None
        self._buffer = None
        self._drawn = None
        self._drawn_marks = None
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
        layers = self._gutter_layers()
        stale = (self._buffer is None
                 or self._drawn is not strip
                 or not _same_layers(layers, self._drawn_marks)
                 or self._buffer.shape[:2] != (height, width))
        if stale:
            self._buffer = np.ascontiguousarray(self._compose(strip, width))
            self._image = QImage(self._buffer.data, width, height,
                                 self._buffer.strides[0],
                                 QImage.Format.Format_RGBA8888)
            # Identity, not equality: the overview memoises its aggregations, so
            # the *same* Strip object comes back for as long as the data behind
            # it has not changed. That makes "is this buffer still current" a
            # pointer comparison rather than a diff of two count arrays.
            self._drawn = strip
            self._drawn_marks = layers
        if self._image is not None:
            painter.drawImage(0, 0, self._image)

    def _compose(self, strip: Strip, width: int) -> np.ndarray:
        """
        The strip's own pixels, plus the user-rule gutter on the right.

        The bands are rendered *narrower* rather than being overdrawn, so a
        marked rule costs the map three pixels of width instead of three pixels
        of information. With nothing marked the gutter is zero wide and this is
        the single `_render_pixels` call it was before.
        """
        layers = self._gutter_layers()
        gutter = mark_gutter(layers, width)
        bands = self._render_pixels(strip, width - gutter)
        if not gutter:
            return bands
        return np.concatenate([bands, render_mark_gutter(
            layers, strip.times, strip.height, gutter,
            theme=self._theme, fallback=_rgb(theme.FOREGROUND))], axis=1)

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

    def _render_pixels(self, strip: Strip, width: int) -> np.ndarray:
        """
        A full-width colour map of what kind of entries are where.

        `fill=True`, because this strip's axis is rows: every pixel covers the
        same number of them, so bar length carries no information and scaling
        by it produces nothing but a ragged right edge.
        """
        return render_rgba(strip, width, theme=self._theme, fill=True)

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
        self._paint_find_current(painter, strip)


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

    rangeSelected = Signal("qlonglong", "qlonglong")
    """
    A dragged time window, `(since, until)` in merged `wall_us`.

    `qlonglong` twice, for the reason `timeSought` gives at length.
    """

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(session, parent)
        self._anchor_px: int | None = None
        self._drag_px: int | None = None
        self._window: tuple[int, int] | None = None
        self._shade = QColor(theme.STRIP_EXCLUDED)

    def strip(self, height: int) -> Strip:
        return self.session.overview.timeline(height)

    def _render_pixels(self, strip: Strip, width: int) -> np.ndarray:
        """
        A monochrome bar chart of how many entries fall in each slice of time.

        Left-justified and one colour. The bar is the only variable, and its
        length is the row's entry count against the busiest row's.
        """
        return render_density_rgba(strip, width, theme=self._theme)

    def _seek_to(self, pixel: int) -> None:
        h = self.height()
        if h <= 0 or not self.session.total_rows:
            return
        self.timeSought.emit(self.session.overview.time_at_pixel(pixel, h))

    # ------------------------------------------------------------------
    # selecting a time range (FR-48)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """
        A press is a seek *and* the start of a possible range.

        The two gestures share a button because the strip has only one, and
        because the distinction is unambiguous once the mouse moves: a click
        seeks, a drag selects. The seek happens on the press rather than the
        release so that clicking still feels immediate, and once a drag passes
        `DRAG_PIXELS` the seeking stops -- dragging out a filter must not also
        scrub the view through every time it passes over.

        Scrubbing is what is given up, and it is the right thing to give up
        here: SPEC 8.2 already says this strip is a seek control and
        deliberately does not drive the scrollbar, because a drag down a
        wall-clock axis advances through rows at wildly uneven rates. The
        minimap is the strip to scrub on.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self._anchor_px = int(event.position().y())
            self._drag_px = None
            self._seek_to(self._anchor_px)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._anchor_px is not None):
            at = int(event.position().y())
            if (self._drag_px is not None
                    or abs(at - self._anchor_px) >= DRAG_PIXELS):
                self._drag_px = at
                self.update()
            else:
                self._seek_to(at)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self._anchor_px is not None and self._drag_px is not None):
            lo, hi = sorted((self._anchor_px, self._drag_px))
            self._anchor_px = self._drag_px = None
            self.update()
            h = self.height()
            if h > 0 and self.session.total_rows:
                ov = self.session.overview
                # The *end* of the last pixel, not its start: a one-pixel band
                # covers a slice of time, and a window ending at the start of
                # it would exclude everything the user dragged over.
                since = int(ov.time_at_pixel(lo, h))
                until = int(ov.time_at_pixel(min(hi + 1, h - 1), h))
                if until < since:
                    since, until = until, since
                self.rangeSelected.emit(since, until)
            event.accept()
            return
        self._anchor_px = self._drag_px = None
        super().mouseReleaseEvent(event)

    def set_window(self, window: tuple[int, int] | None) -> None:
        """The time filter in force, drawn as a band (FR-45, FR-48)."""
        if window == self._window:
            return
        self._window = window
        self.update()

    def _paint_window(self, painter: QPainter, strip: Strip) -> None:
        """
        The dragged or committed time window.

        Drawn as *shade over the excluded time* rather than as a box round the
        included time, because that is what the filter does: the rows outside
        the window are gone from the view, and showing them shaded on the
        timeline is the one place a reader can still see how much of the log
        they have put aside.
        """
        band = self._drag_band()
        if band is not None:
            top, bottom = band
        elif self._window is not None:
            times = strip.times
            top = int(np.searchsorted(times, self._window[0]))
            bottom = int(np.searchsorted(times, self._window[1]))
        else:
            return
        top = max(0, min(top, strip.height))
        bottom = max(top, min(bottom, strip.height))
        w = max(1, self.width() - 1)
        painter.fillRect(QRect(0, 0, w, top), self._shade)
        painter.fillRect(QRect(0, bottom, w, strip.height - bottom), self._shade)
        painter.setPen(self._box)
        painter.drawLine(0, top, w, top)
        painter.drawLine(0, bottom, w, bottom)

    def _drag_band(self) -> tuple[int, int] | None:
        if self._anchor_px is None or self._drag_px is None:
            return None
        lo, hi = sorted((self._anchor_px, self._drag_px))
        return lo, hi

    def _paint_overlay(self, painter: QPainter, strip: Strip) -> None:
        """
        Where the view is, as a line rather than a box.

        A box would be a lie here: the viewport is a row range, and the span of
        *time* it covers is whatever those rows happen to cover -- a second
        during a burst, an hour during a quiet patch. A single line says "you
        are here" without claiming to say how much you can see.
        """
        self._paint_window(painter, strip)
        self._paint_find_current(painter, strip)
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
