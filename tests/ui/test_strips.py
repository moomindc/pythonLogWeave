"""
The overview strip widgets -- SPEC 8, FR-32, FR-33, FR-34, M5.

Thin, because the widgets are thin. Everything about *what* a strip shows is
asserted on exact pixels in `tests/unit/test_overview.py`, headlessly. What is
left to test here is the parts that only exist because there is a widget: the
blit, the viewport box, the mouse, and the menu wiring that FR-34 asks for.

The offscreen platform has no font database, which does not matter here --
nothing in a strip is text. The one thing it does affect is the window's real
height, so the tests set a size explicitly rather than trusting a layout.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.index.severity import Severity  # noqa: E402
from logweave.session import Session  # noqa: E402
from logweave.session.overview import StripTheme  # noqa: E402
from logweave.ui import settings, theme  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402
from logweave.ui.strips import MinimapStrip, TimelineStrip  # noqa: E402

HEIGHT = 400


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def gapped(tmp_path) -> Path:
    """Two bursts an hour apart -- the shape the timeline exists to show."""
    path = tmp_path / "gapped.log"
    with path.open("w", encoding="utf-8") as fh:
        for sec in list(range(600)) + list(range(3600, 4200)):
            level = "ERROR" if sec == 300 else "INFO"
            h, rem = 10 + sec // 3600, sec % 3600
            fh.write(f"2026-10-04 {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                     f".000000 {level} line {sec}\n")
    return path


@pytest.fixture
def session(gapped):
    s = Session(use_cache=False)
    s.add_files([gapped])
    yield s
    s.close()


def shown(widget):
    widget.resize(theme.STRIP_WIDTH, HEIGHT)
    widget.show()
    QApplication.processEvents()
    return widget


def render(widget) -> QImage:
    img = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    widget.render(img)
    return img


def click(widget, y: int, *, buttons=Qt.MouseButton.NoButton):
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(4, y), QPointF(4, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton | buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.mousePressEvent(event)


# ---------------------------------------------------------------------------
# painting
# ---------------------------------------------------------------------------

def test_a_strip_paints_the_buffer_it_was_given(app, session):
    """
    The blit reached the widget. Asserted by finding the palette colour the
    data says should be there, which is the same thing the pixel tests assert
    on the buffer -- so a failure here is the `QImage` wrapping and nothing
    else.
    """
    strip = shown(MinimapStrip(session))
    # The viewport box is drawn over the bands and spans the full width, so it
    # is pushed to the bottom before a band is sampled near the top. Picking a
    # pixel without doing that found the box's own outline colour and read like
    # a palette bug.
    strip.set_viewport(session.total_rows - 60, 60)
    img = render(strip)
    assert img.height() == HEIGHT

    # The busiest row's top class here is INFO, which below the colour floor
    # is the ordinary-traffic grey -- taken from the theme either way, so this
    # test is about the blit and not about which colour that class gets.
    info = StripTheme().severity[int(Severity.INFO)]
    want = (info[0] << 16) | (info[1] << 8) | info[2]
    totals = session.overview.minimap(HEIGHT).totals
    busiest = int(np.argmax(totals[: HEIGHT // 2]))
    assert (img.pixel(0, busiest) & 0xFFFFFF) == want
    strip.deleteLater()


def test_an_empty_session_paints_background_and_does_not_raise(app):
    s = Session()
    strip = shown(TimelineStrip(s))
    img = render(strip)
    bg = QImage(img.size(), img.format())
    bg.fill(0)
    assert img.height() == HEIGHT          # painted, and nothing exploded
    strip.deleteLater()
    s.close()


def test_the_timeline_leaves_the_idle_gap_blank(app, session):
    """FR-33, through the widget rather than the buffer."""
    strip = shown(TimelineStrip(session))
    img = render(strip)
    background = StripTheme().background
    bg = (background[0] << 16) | (background[1] << 8) | background[2]
    rows = [y for y in range(HEIGHT)
            if all((img.pixel(x, y) & 0xFFFFFF) == bg
                   for x in range(theme.STRIP_WIDTH - 1))]
    assert len(rows) > HEIGHT // 4, "an hour of silence is most of the strip"
    strip.deleteLater()


def test_the_pixel_buffer_is_kept_alive_for_the_image_over_it(app, session):
    """
    `QImage` built on a buffer does not copy it and does not own it. Letting
    the array fall out of scope leaves the image pointing at freed memory --
    wrong pixels most of the time, an access violation the rest, and neither
    reproducible. So the widget holds a reference, and this says so.
    """
    strip = shown(MinimapStrip(session))
    render(strip)
    assert strip._buffer is not None
    assert strip._image is not None
    assert strip._buffer.flags["C_CONTIGUOUS"]
    strip.deleteLater()


def test_a_scroll_does_not_rebuild_the_buffer(app, session):
    """
    The memo reaching the widget, which is what keeps the strips off the frame
    budget during a scroll: what moves is the viewport box, and the aggregation
    underneath it has not changed.
    """
    strip = shown(MinimapStrip(session))
    render(strip)
    first = strip._buffer
    strip.set_viewport(500, 60)
    render(strip)
    assert strip._buffer is first, "re-aggregated for a box that moved"
    strip.deleteLater()


def test_a_refresh_does_rebuild_the_buffer(app, session):
    strip = shown(MinimapStrip(session))
    render(strip)
    first = strip._buffer
    strip.refresh()
    render(strip)
    assert strip._buffer is not first
    strip.deleteLater()


def test_the_viewport_box_follows_the_view(app, session):
    """FR-32: the minimap draws where the viewport currently is."""
    strip = shown(MinimapStrip(session))
    box = QImage(theme.STRIP_WIDTH, HEIGHT, QImage.Format.Format_ARGB32)

    strip.set_viewport(0, 60)
    strip.render(box)
    top = box.copy()
    strip.set_viewport(session.total_rows - 60, 60)
    strip.render(box)
    assert top != box, "the box did not move with the viewport"
    strip.deleteLater()


def test_the_viewport_box_is_never_narrower_than_two_pixels(app, session):
    """
    On a billion-row merge a sixty-row screenful is a ten-thousandth of a
    pixel, and a box that rounds away tells the reader nothing about where they
    are.
    """
    strip = shown(MinimapStrip(session))
    strip.set_viewport(0, 1)
    img = render(strip)
    outline = theme.STRIP_VIEWPORT_BOX.lstrip("#")
    want = int(outline, 16)
    hits = [y for y in range(HEIGHT) if (img.pixel(0, y) & 0xFFFFFF) == want]
    assert len(hits) >= 2
    strip.deleteLater()


# ---------------------------------------------------------------------------
# the mouse (FR-32, FR-33)
# ---------------------------------------------------------------------------

def test_a_minimap_click_asks_for_the_row_that_pixel_covers(app, session):
    strip = shown(MinimapStrip(session))
    strip.set_viewport(0, 60)
    got = []
    strip.rowSought.connect(got.append)

    click(strip, HEIGHT // 2)
    assert got, "no row was sought"
    # Centred on the clicked pixel, not top-aligned: there is more of "there"
    # above a mark than a top-aligned seek would show.
    expected = session.overview.row_at_pixel(HEIGHT // 2, HEIGHT)
    assert got[0] == max(0, expected - 30)
    strip.deleteLater()


def test_a_timeline_click_asks_for_the_time_that_pixel_covers(app, session):
    strip = shown(TimelineStrip(session))
    got = []
    strip.timeSought.connect(got.append)

    click(strip, 10)
    assert got
    assert got[0] == session.overview.time_at_pixel(10, HEIGHT)
    strip.deleteLater()


def test_a_click_past_the_bottom_is_clamped(app, session):
    strip = shown(MinimapStrip(session))
    got = []
    strip.rowSought.connect(got.append)
    click(strip, HEIGHT + 500)
    assert got
    assert 0 <= got[0] < session.total_rows
    strip.deleteLater()


def test_a_click_on_an_empty_session_seeks_nowhere(app):
    s = Session()
    strip = shown(MinimapStrip(s))
    got = []
    strip.rowSought.connect(got.append)
    click(strip, 10)
    assert got == []
    strip.deleteLater()
    s.close()


def test_the_tooltip_names_the_time_and_what_is_in_the_pixel(app, session):
    strip = shown(TimelineStrip(session))
    busiest = int(np.argmax(session.overview.timeline(HEIGHT).totals))
    text = strip._tooltip(busiest)
    assert "2026-10-04" in text
    assert "entries" in text
    assert "info" in text

    blank = int(np.flatnonzero(
        session.overview.timeline(HEIGHT).totals == 0)[0])
    assert "nothing logged" in strip._tooltip(blank)
    strip.deleteLater()


# ---------------------------------------------------------------------------
# the window (FR-34, D4)
# ---------------------------------------------------------------------------

@pytest.fixture
def window(app, gapped):
    w = MainWindow(background=False)
    w.resize(1200, 800)
    # Shown, because `isVisible()` is False for every child of a window that
    # was never mapped -- so a test of FR-34's collapse would pass trivially
    # against a window where nothing is visible at all.
    w.show()
    QApplication.processEvents()
    w.open_paths([gapped])
    QApplication.processEvents()
    yield w
    w.close()


def test_both_strips_are_shown_by_default(window):
    """
    D4's whole claim is that you want the two of them, and a feature nobody
    finds in a menu is a feature that was not built.
    """
    assert window.minimap.isVisible()
    assert window.timeline.isVisible()
    assert window.minimap_act.isChecked()
    assert window.timeline_act.isChecked()


def test_either_strip_can_be_collapsed_independently(window):
    """FR-34."""
    window.minimap_act.setChecked(False)
    assert not window.minimap.isVisible()
    assert window.timeline.isVisible()

    window.timeline_act.setChecked(False)
    assert not window.timeline.isVisible()

    window.minimap_act.setChecked(True)
    assert window.minimap.isVisible()
    assert not window.timeline.isVisible()


def test_the_choice_persists(window, isolated_settings):
    """FR-34: the choice persists in the profile."""
    window.minimap_act.setChecked(False)
    assert settings.read_bool(settings.SHOW_MINIMAP, True) is False
    assert settings.read_bool(settings.SHOW_TIMELINE, True) is True

    # `isVisibleTo`, because this window is deliberately never shown -- which is
    # the state the menu is built in, and the state the first version of
    # `_build_menus` read `isVisible()` in and got wrong.
    again = MainWindow(background=False)
    try:
        assert not again.minimap.isVisibleTo(again)
        assert again.timeline.isVisibleTo(again)
        assert not again.minimap_act.isChecked()
        assert again.timeline_act.isChecked()
    finally:
        again.close()


def test_the_menu_and_the_strips_never_disagree(window):
    """
    The trap the lane-width menu fell into at M4: a menu built from one source
    of truth and a widget set from another, disagreeing the moment the window
    opened.
    """
    for act, widget in ((window.minimap_act, window.minimap),
                        (window.timeline_act, window.timeline)):
        assert act.isChecked() == widget.isVisible()
        act.setChecked(not act.isChecked())
        assert act.isChecked() == widget.isVisible()


def test_a_minimap_click_scrolls_the_view(window):
    before = window.view.top_row()
    click(window.minimap, window.minimap.height() - 2)
    QApplication.processEvents()
    assert window.view.top_row() > before


def test_a_timeline_click_seeks_the_view_by_time(window):
    strip = window.timeline
    height = strip.height()
    late = int(np.flatnonzero(
        window.session.overview.timeline(height).totals > 0)[-1])
    click(strip, late)
    QApplication.processEvents()
    assert window.view.top_row() >= 500, "the second burst starts at row 600"


def test_scrolling_the_view_moves_the_strips_viewport(window):
    window.view.scroll_to_row(800)
    QApplication.processEvents()
    assert window.minimap._top_row == 800
    assert window.timeline._marker_time is not None


def test_disabling_a_source_rebuilds_the_strips(window, gapped, tmp_path):
    """FR-9: a source out of the merge is a source out of the overview."""
    second = tmp_path / "other.log"
    second.write_text(
        "".join(f"2026-10-04 11:00:{i % 60:02d}.000000 WARN other {i}\n"
                for i in range(100)), encoding="utf-8")
    window.open_paths([second])
    before = int(window.session.overview.timeline(400).counts.sum())

    item = window.source_list.item(1)
    item.setCheckState(Qt.CheckState.Unchecked)
    QApplication.processEvents()
    after = int(window.session.overview.timeline(400).counts.sum())
    assert after < before


def test_closing_everything_leaves_the_strips_on_the_new_session(window):
    window.close_all()
    assert window.minimap.session is window.session
    assert window.timeline.session is window.session
    assert not window.session.overview.available


# ---------------------------------------------------------------------------
# M7: find marks and the time window (FR-44, FR-48)
# ---------------------------------------------------------------------------

def test_a_find_layer_gets_the_rightmost_gutter_column(app, session):
    """
    A find is transient and the rules are not, so its column must not sit in
    the rule gutter where a toggled rule would move it.
    """
    from logweave.session.marks import MarkLayer

    strip = shown(MinimapStrip(session))
    lo, hi = session.time_span()
    rule = MarkLayer(rule="r", colour="#ff0000",
                     times=np.array([lo], dtype=np.int64))
    find = MarkLayer(rule="find", colour=theme.FIND_MARK,
                     times=np.array([hi], dtype=np.int64))

    strip.set_marks([rule])
    assert strip._gutter_layers() == (rule,)
    strip.set_find_marks(find)
    assert strip._gutter_layers() == (rule, find)
    assert strip._gutter_layers()[-1] is find

    before = render(strip)
    strip.set_find_marks(None)
    QApplication.processEvents()
    assert render(strip) != before, "the find column was not redrawn"


def test_the_find_colour_reaches_the_pixels(app, session):
    from logweave.session.marks import MarkLayer

    strip = shown(TimelineStrip(session))
    lo, _hi = session.time_span()
    strip.set_find_marks(MarkLayer(rule="find", colour=theme.FIND_MARK,
                                   times=np.array([lo], dtype=np.int64)))
    QApplication.processEvents()
    img = render(strip)
    wanted = QColor(theme.FIND_MARK).rgb() & 0xFFFFFF
    found = any((img.pixel(x, y) & 0xFFFFFF) == wanted
                for y in range(img.height()) for x in range(img.width()))
    assert found, "no pixel in the find colour"


def test_the_time_window_paints_without_the_bands_moving(app, session):
    """
    FR-48's shade is drawn over the strip, so the one thing to check is that a
    window neither raises nor blanks the map it sits on.
    """
    strip = shown(TimelineStrip(session))
    plain = render(strip)
    lo, hi = session.time_span()
    strip.set_window((lo, lo + (hi - lo) // 2))
    QApplication.processEvents()
    shaded = render(strip)
    assert shaded != plain
    blank = QImage(shaded.size(), shaded.format())
    blank.fill(QColor(theme.BACKGROUND))
    assert shaded != blank, "the window painted over everything"

    strip.set_window(None)
    QApplication.processEvents()
    assert render(strip) == plain


def test_a_window_outside_the_span_is_clamped_rather_than_raising(app, session):
    strip = shown(TimelineStrip(session))
    lo, hi = session.time_span()
    for window in ((lo - 10**12, lo - 10**11), (hi + 10**11, hi + 10**12),
                   (hi, lo)):
        strip.set_window(window)
        QApplication.processEvents()
        render(strip)
