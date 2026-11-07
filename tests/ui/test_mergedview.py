"""
The Qt shell -- SPEC 9, M3.

`importorskip` is load-bearing: the engine CI job installs no Qt at all and runs
the whole suite, so this module has to disappear rather than fail there (SPEC 1,
NFR-18).

These tests paint into an offscreen window. That backend has no font database --
every family resolves empty and every glyph is 13x13 -- so it is fine for
geometry, damage-region and selection logic, and useless for text measurement.
Timing lives in `tools/bench.py`, against the real font stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QEvent, QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QImage, QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.session import Session  # noqa: E402
from logweave.ui.mergedview import (  # noqa: E402
    COPY_ROW_LIMIT,
    MARK_GUTTER,
    PAD,
    SCROLL_UNITS_MAX,
    MergedLogView,
)


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def session(corpus: Path):
    s = Session()
    s.add_files(sorted((corpus / "basic").glob("*.log")))
    yield s
    s.close()


@pytest.fixture
def view(app, session):
    v = MergedLogView(session)
    v.resize(1200, 800)
    yield v
    v.deleteLater()


def render(view) -> QImage:
    img = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
    view.viewport().render(img)
    return img


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def test_the_view_shows_a_whole_number_of_uniform_rows(view):
    assert view.row_height > 0
    assert view.visible_rows() == view.viewport().height() // view.row_height


def test_the_scrollbar_covers_every_row_that_is_not_already_on_screen(view, session):
    bar = view.verticalScrollBar()
    assert bar.minimum() == 0
    assert bar.maximum() == session.total_rows - view.visible_rows()
    assert bar.pageStep() == view.visible_rows()


def test_scrolling_to_the_bottom_lands_on_the_last_screenful(view, session):
    view.scroll_to_row(session.total_rows - 1)
    top, bottom = view.visible_span()
    assert bottom >= session.total_rows
    assert top <= session.total_rows - 1


def test_scrolling_past_the_ends_is_clamped_not_an_error(view, session):
    view.scroll_to_row(-500)
    assert view.top_row() == 0
    view.scroll_to_row(session.total_rows * 10)
    assert view.top_row() <= session.total_rows


def test_an_empty_view_paints_a_placeholder_without_asking_for_rows(app):
    """The window exists before anything is dropped on it."""
    v = MergedLogView(Session())
    v.resize(800, 400)
    assert v.session.total_rows == 0
    render(v)                      # must not raise
    assert v.verticalScrollBar().maximum() == 0
    v.deleteLater()


# ---------------------------------------------------------------------------
# the int64 row space (NFR-15)
# ---------------------------------------------------------------------------

def test_small_files_map_one_scrollbar_unit_to_one_row(view):
    assert view._scroll_scale == 1
    view.scroll_to_row(321)
    assert view.top_row() == 321


def test_row_space_beyond_int32_is_scaled_onto_the_scrollbar(view, monkeypatch):
    """
    NFR-15 allows 2x10^9 rows; a Qt scrollbar is an int. Past the cap the view
    must scale rather than overflow, so check it with a row count that would
    not fit.
    """
    huge = 8_000_000_000
    monkeypatch.setattr(type(view.session), "total_rows", property(lambda self: huge))
    view.refresh()

    assert view._scroll_scale > 1
    bar = view.verticalScrollBar()
    assert bar.maximum() <= SCROLL_UNITS_MAX

    # A scaled scrollbar can only put the top row on a multiple of the scale, so
    # the guarantee is not "the target is at the top" -- it is "the target is on
    # screen", which is what a seek has to deliver. In the last screenful it
    # cannot be at the top at all, scaled or not.
    for target in (0, huge // 3, huge - 1):
        view.scroll_to_row(target)
        top, bottom = view.visible_span()
        assert top <= target < bottom, f"{target} not visible in [{top}, {bottom})"


@pytest.mark.parametrize("total", [1, 2, 57, 1_000, 1_000_000_001, 8_000_000_000])
def test_the_last_row_is_always_reachable(view, monkeypatch, total):
    """
    Regression: the scrollbar maximum was floored by the scale, so the final
    `(total - page) % scale` rows of a very large merge could not be scrolled
    to at all -- you could get within seven rows of the end of an
    eight-billion-row file and no further.

    Parameterised across the scale boundary because the bug only shows when the
    remainder is non-zero.
    """
    monkeypatch.setattr(type(view.session), "total_rows", property(lambda self: total))
    view.refresh()
    view.scroll_to_row(total - 1)
    top, bottom = view.visible_span()
    assert top <= total - 1 < bottom, f"last row of {total} unreachable"


# ---------------------------------------------------------------------------
# damage regions -- the scrolling optimisation
# ---------------------------------------------------------------------------

def test_a_full_repaint_paints_every_visible_row(view):
    render(view)
    assert view._last_painted >= view.visible_rows()


def test_a_three_row_band_paints_three_rows_not_a_screenful(view):
    """
    The point of honouring `event.rect()`: a wheel step must not cost a full
    frame. Measured 0.86 ms against 10.5 ms (SPEC 14.1).
    """
    img = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
    band = QRect(0, 0, view.viewport().width(), 3 * view.row_height)
    view.viewport().render(img, QPoint(0, 0), band)
    assert view._last_painted <= 4


def test_painting_is_bounded_by_line_length(view, session, tmp_path):
    """
    A 4000-character stack trace must not cost 4000 characters of shaping. The
    slice to the visible character window is what bounds it -- measured 61 ms
    against 5.6 ms a frame (SPEC 14.1).
    """
    long_log = tmp_path / "long.log"
    long_log.write_text(
        "\n".join(
            f"2026-10-04 14:20:{i:02d}.000000 ERROR " + "x" * 4000 for i in range(60)
        ) + "\n",
        encoding="utf-8",
    )
    s = Session()
    s.add_files([long_log])
    v = MergedLogView(s)
    v.resize(1200, 800)
    render(v)                      # must not raise, and must finish
    span = v.viewport().width() // v._char_w + 2
    assert span < 4000
    v.deleteLater()
    s.close()


# ---------------------------------------------------------------------------
# the timestamp column (SPEC 9.5)
# ---------------------------------------------------------------------------

def leading_ts_row(session):
    return next(r for r in session.rows(0, 200) if r.msg_from and not r.multiline)


def test_the_column_is_shown_by_default_and_the_message_is_trimmed(view, session):
    assert view.show_timestamp
    assert view.trims_timestamp()
    r = leading_ts_row(session)
    assert view._row_text(r, trim=True) == r.message
    assert not view._row_text(r, trim=True).startswith("2026")


def test_hiding_the_column_stops_the_trimming(view, session):
    """
    The interlock. Trimming only ever removes a duplicate, so when the column
    holding the other copy goes away the trim has to stop -- otherwise hiding a
    redundant column would hide the timestamp outright.
    """
    view.set_show_timestamp(False)
    assert not view.trims_timestamp()
    r = leading_ts_row(session)
    assert view._row_text(r, trim=view.trims_timestamp()) == r.text
    assert "2026-10-04" in view._row_text(r, trim=view.trims_timestamp())


def test_the_time_is_always_visible_one_way_or_the_other(view, session):
    """
    The property the interlock exists to guarantee, stated directly: whichever
    way the toggle is set, a row still shows its time somewhere.
    """
    r = leading_ts_row(session)
    for shown in (True, False):
        view.set_show_timestamp(shown)
        painted = view._row_text(r, trim=view.trims_timestamp())
        assert shown or "2026-10-04" in painted


def test_hiding_the_column_gives_its_width_to_the_message(view):
    before = view.message_width()
    view.set_show_timestamp(False)
    assert view.message_left < MARK_GUTTER + PAD + view._ts_w
    assert view.message_width() > before


def test_the_toggle_reports_itself(view):
    seen = []
    view.timestampVisibilityChanged.connect(seen.append)
    view.set_show_timestamp(False)
    view.set_show_timestamp(False)
    view.set_show_timestamp(True)
    assert seen == [False, True]


def test_the_view_paints_with_the_column_hidden(view):
    view.set_show_timestamp(False)
    render(view)
    assert view._last_painted > 0


# ---------------------------------------------------------------------------
# horizontal scrolling (FR-36a)
# ---------------------------------------------------------------------------

def test_only_the_message_column_scrolls_horizontally(view):
    """
    FR-36a: the marks gutter, timestamp and source lanes stay frozen. Pixels are
    the check, because the requirement is about what the user sees -- the
    timestamp band must be identical before and after scrolling right.
    """
    # The first render is what finally applies the widget's geometry, so it is a
    # warm-up: without it the two captures are different sizes and the
    # comparison is meaningless rather than failing honestly.
    render(view)
    view.refresh()

    before = render(view)
    bar = view.horizontalScrollBar()
    assert bar.maximum() > 0, "there should be something to scroll to"
    bar.setValue(bar.maximum() // 2)
    after = render(view)
    assert before.size() == after.size()

    frozen = QRect(0, 0, view.message_left, view.viewport().height())
    assert before.copy(frozen) == after.copy(frozen), "timestamp column moved"

    msg = QRect(view.message_left, 0, view.message_width(),
                view.viewport().height())
    assert before.copy(msg) != after.copy(msg), "message did not scroll"


def test_the_horizontal_range_is_measured_against_the_message_column(view):
    from logweave.ui import theme

    content = view._char_w * theme.ASSUMED_COLUMNS
    assert view.horizontalScrollBar().maximum() == content - view.message_width()


def test_the_message_column_shrinks_as_sources_are_added(view, session, corpus):
    """The lane gutter grows with the source count and takes it from the message."""
    before = view.message_width()
    session.add_files([corpus / "formats" / "apache_error.log"])
    view.refresh()
    assert view.message_width() < before


def test_a_multiline_entry_paints_its_first_line_and_a_marker(view, session):
    """
    `drawText` draws a newline as a glyph rather than wrapping, so passing a
    whole multi-line entry runs its continuation lines on to the end of the
    first. Rows are a uniform height (SPEC 9.1), so only the head is painted.
    """
    from logweave.ui import theme

    row = next(
        r for r in session.rows(0, session.total_rows)
        if r.multiline and "\n" in r.text
    )
    painted = view._row_text(row)
    assert "\n" not in painted
    assert painted.startswith(theme.MULTILINE_MARK)
    assert painted.endswith(row.text.partition("\n")[0])


def test_a_single_line_entry_is_painted_unchanged(view, session):
    row = next(r for r in session.rows(0, 200) if not r.multiline)
    assert view._row_text(row) == row.text


# ---------------------------------------------------------------------------
# selection and copy (D9, FR-46a)
# ---------------------------------------------------------------------------

def test_a_fresh_view_has_a_single_row_selected(view):
    assert view.selection() == (0, 0)
    assert view.current_row() == 0


def test_moving_without_shift_collapses_the_selection(view):
    view.set_current_row(10)
    view.set_current_row(40, extend=True)
    assert view.selection() == (10, 40)
    view.set_current_row(5)
    assert view.selection() == (5, 5)


def test_extending_upwards_gives_a_low_to_high_range(view):
    view.set_current_row(40)
    view.set_current_row(10, extend=True)
    assert view.selection() == (10, 40)


def test_select_all_covers_the_whole_merge(view, session):
    view.select_all()
    assert view.selection() == (0, session.total_rows - 1)


def test_copying_puts_attributed_rows_on_the_clipboard(view, app):
    view.set_current_row(0)
    view.set_current_row(4, extend=True)
    view.copy_selection()
    text = QApplication.clipboard().text()
    lines = [
        line for line in text.splitlines()
        if line and not line.startswith(" ")
    ]
    assert len(lines) == 5
    for line in lines:
        name, _, rest = line.partition(":")
        assert name.endswith(".log")
        assert rest.split()[0].isdigit()


def test_copy_reports_how_many_rows_it_took(view):
    seen = []
    view.copied.connect(seen.append)
    view.set_current_row(0)
    view.set_current_row(9, extend=True)
    view.copy_selection()
    assert seen == [10]


def test_an_oversized_copy_is_refused_rather_than_truncated(view, monkeypatch):
    """
    Silently copying part of a selection is worse than refusing: the user pastes
    it into a ticket and never learns rows were dropped.
    """
    refused = []
    view.copyRefused.connect(refused.append)
    monkeypatch.setattr(
        type(view.session), "total_rows",
        property(lambda self: COPY_ROW_LIMIT * 3),
    )
    view.refresh()
    view.select_all()
    view.copy_selection()
    assert refused and refused[0] > COPY_ROW_LIMIT


# ---------------------------------------------------------------------------
# keyboard
# ---------------------------------------------------------------------------

def key(view, code, mods=Qt.KeyboardModifier.NoModifier):
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, code, mods))


def test_arrow_keys_move_the_current_row(view):
    key(view, Qt.Key.Key_Down)
    assert view.current_row() == 1
    key(view, Qt.Key.Key_Up)
    assert view.current_row() == 0


def test_up_at_the_top_stays_put(view):
    key(view, Qt.Key.Key_Up)
    assert view.current_row() == 0


def test_end_goes_to_the_last_row_and_home_back(view, session):
    key(view, Qt.Key.Key_End)
    assert view.current_row() == session.total_rows - 1
    key(view, Qt.Key.Key_Home)
    assert view.current_row() == 0


def test_page_down_moves_by_a_screenful(view):
    key(view, Qt.Key.Key_PageDown)
    assert view.current_row() == view.visible_rows()


def test_shift_arrow_extends_the_selection(view):
    key(view, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    key(view, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert view.selection() == (0, 2)


def test_moving_the_current_row_scrolls_it_into_sight(view):
    key(view, Qt.Key.Key_End)
    top, bottom = view.visible_span()
    assert top <= view.current_row() < bottom


def test_the_current_row_signal_fires_once_per_move(view):
    seen = []
    view.currentRowChanged.connect(seen.append)
    view.set_current_row(3)
    view.set_current_row(3)
    view.set_current_row(4)
    assert seen == [3, 4]


# ---------------------------------------------------------------------------
# time seeking (NFR-6)
# ---------------------------------------------------------------------------

def test_seeking_by_time_lands_on_the_right_row(view, session):
    rows = session.rows(session.total_rows // 2, 1)
    view.scroll_to_time(rows[0].ts)
    assert view.top_row() == session.time_to_row(rows[0].ts)


def test_seeking_before_the_first_entry_lands_at_the_top(view, session):
    lo, _ = session.time_span()
    view.scroll_to_time(lo - 10_000_000)
    assert view.top_row() == 0
