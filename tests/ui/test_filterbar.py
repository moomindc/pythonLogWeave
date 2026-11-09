"""
The filter bar, the timeline's range drag and the window's filter wiring --
FR-45, FR-48.

The evaluation is tested headlessly in `tests/unit/test_filter.py`. What is
tested here is the part only the window can get wrong: that a tier-1 filter is
applied **in the same turn** as the click (NFR-9's whole point), that the view
keeps the reader's place in time across it, that the count is on screen
whenever rows are missing, and that closing the bar does not leave a filter
behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.index.severity import Severity  # noqa: E402
from logweave.session.filters import Filter  # noqa: E402
from logweave.session.nav import AT_LEAST  # noqa: E402
from logweave.ui.filterbar import FilterBar  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402
from logweave.ui.strips import DRAG_PIXELS  # noqa: E402


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def window(app, corpus: Path):
    w = MainWindow(background=False)
    w.resize(1200, 800)
    w.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert w.session.total_rows > 0
    yield w
    # `close()`, not just `session.close()`: the window owns two scan threads
    # now, and a running `QThread` destroyed with its parent aborts the process
    # rather than raising. `closeEvent` cancels and joins both.
    w.close()
    w.deleteLater()


# ---------------------------------------------------------------- the bar


def test_the_bar_builds_a_filter_from_its_controls(app):
    bar = FilterBar()
    bar.set_sources(["app", "soa"])
    bar._include.setText("order")
    bar._exclude.setText("health")
    bar._case.setChecked(True)
    got = bar.filter()
    assert got.include == "order" and got.exclude == "health"
    assert got.case_sensitive and not got.regex


def test_every_severity_selected_means_no_severity_filter(app):
    """
    All eight is the same view as none, and saying so keeps the filter out of
    tier 1 entirely -- a mask per source that keeps everything is work with no
    effect.
    """
    bar = FilterBar()
    bar.set_severities({int(s) for s in Severity})
    assert bar.filter().severities == frozenset()
    assert not bar.filter().active


def test_a_quick_pick_sets_the_whole_band(app):
    bar = FilterBar()
    bar.set_severities(AT_LEAST[int(Severity.ERROR)])
    assert bar.filter().severities == AT_LEAST[int(Severity.ERROR)]
    assert "ERROR+" in bar._severity_button.text()


def test_clearing_puts_the_bar_back_to_passing_everything(app):
    bar = FilterBar()
    bar.set_sources(["a"])
    bar.set_severities(AT_LEAST[int(Severity.WARNING)])
    bar._include.setText("x")
    bar.set_window(10, 20)
    bar.clear()
    assert not bar.filter().active


def test_a_bad_regex_is_reported_rather_than_run(app):
    bar = FilterBar()
    bar._regex.setChecked(True)
    bar._include.setText("(unclosed")
    assert bar.filter().validate()


# ------------------------------------------------------- the window wiring


def test_a_severity_filter_applies_in_the_same_turn(window):
    """
    NFR-9: no thread, no wait. Tier 1 is masks plus derived checkpoints, so by
    the time `_filter_changed` returns the view is already filtered.
    """
    before = window.session.total_rows
    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.ERROR)]))

    assert window.session.is_filtered
    assert 0 < window.session.total_rows < before
    assert window.session.unfiltered_rows == before


def test_the_filter_count_is_on_screen_in_both_places(window):
    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.WARNING)]))
    assert window.filterbar._count.text()
    assert window._status_filter.text(), "the status bar does not repeat it"


def test_clearing_the_filter_restores_every_row(window):
    before = window.session.total_rows
    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.ERROR)]))
    assert window.session.total_rows < before

    window.clear_filter()
    assert not window.session.is_filtered
    assert window.session.total_rows == before
    assert window._status_filter.text() == ""


def test_closing_the_bar_drops_the_filter_with_it(window):
    """
    A filter still in force with nothing on screen saying so is the one way
    this view can mislead someone badly, so the ✕ clears as well as hides.
    """
    window.open_filter()
    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.ERROR)]))
    window.close_filter()
    assert not window.session.is_filtered
    assert not window.filterbar.isVisibleTo(window)


def test_the_reader_keeps_their_place_in_time(window):
    """
    Row numbers move when a filter changes and times do not, so the view is put
    back by time. Without this, filtering while reading row 4,000,000 lands you
    wherever four million rows now happens to be.
    """
    window.view.set_current_row(window.session.total_rows // 2)
    was = window.session.row_to_time(window.view.current_row())

    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.INFO)]))
    now = window.session.row_to_time(window.view.current_row())
    # Within the filter's own resolution: the row that was being read may
    # itself have been filtered out, in which case the nearest kept row in time
    # is the right answer.
    assert abs(now - was) < 60_000_000


def test_a_time_window_narrows_the_view_and_shades_the_timeline(window):
    lo, hi = window.session.time_span()
    middle = lo + (hi - lo) // 2
    before = window.session.total_rows

    window._filter_changed(Filter(since=lo, until=middle))
    assert window.session.total_rows < before
    assert window.timeline._window == (lo, middle)


def test_dragging_the_timeline_sets_the_time_filter(window):
    """FR-48, as the gesture actually happens: press, move, release."""
    strip = window.timeline
    strip.resize(64, 400)
    press = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(10, 40),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    move = QMouseEvent(QMouseEvent.Type.MouseMove,
                       QPointF(10, 40 + DRAG_PIXELS * 10),
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    release = QMouseEvent(QMouseEvent.Type.MouseButtonRelease,
                          QPointF(10, 40 + DRAG_PIXELS * 10),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)

    ranges: list[tuple[int, int]] = []
    strip.rangeSelected.connect(lambda a, b: ranges.append((a, b)))
    strip.mousePressEvent(press)
    strip.mouseMoveEvent(move)
    strip.mouseReleaseEvent(release)

    assert len(ranges) == 1
    since, until = ranges[0]
    assert since < until
    assert window.session.is_filtered, "the drag did not reach the filter"
    assert window.filterbar.isVisibleTo(window), "the bar stayed hidden"
    assert window.filterbar.filter().since == since


def test_a_click_on_the_timeline_still_only_seeks(window):
    """A click is a seek and a drag is a selection; a click must not filter."""
    strip = window.timeline
    strip.resize(64, 400)
    ranges: list[tuple[int, int]] = []
    strip.rangeSelected.connect(lambda a, b: ranges.append((a, b)))

    for kind, buttons in ((QMouseEvent.Type.MouseButtonPress,
                           Qt.MouseButton.LeftButton),
                          (QMouseEvent.Type.MouseButtonRelease,
                           Qt.MouseButton.NoButton)):
        event = QMouseEvent(kind, QPointF(10, 120), Qt.MouseButton.LeftButton,
                            buttons, Qt.KeyboardModifier.NoModifier)
        if kind == QMouseEvent.Type.MouseButtonPress:
            strip.mousePressEvent(event)
        else:
            strip.mouseReleaseEvent(event)

    assert ranges == []
    assert not window.session.is_filtered


def test_the_strips_use_the_filtered_histograms(window):
    """
    SPEC 8.1: a filtered view has to have filtered overviews, or the strips
    advertise rows the view will not show. The aggregation is memoised, so what
    matters here is that the memo was dropped.
    """
    window.minimap.resize(64, 200)
    before = window.session.overview.minimap(200)
    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.ERROR)]))
    after = window.session.overview.minimap(200)
    assert after is not before
    assert int(after.counts.sum()) < int(before.counts.sum())


def test_a_hidden_source_is_not_offered_rows_by_the_filter_bar(window):
    names = [s.alias for s in window.session.sources]
    window.filterbar.set_sources(names)
    assert len(window.filterbar._source_acts) == len(names)

    window.filterbar.set_source_set({0})
    assert window.filterbar.filter().sources == frozenset({0})
    window._filter_changed(window.filterbar.filter())
    assert window.session.total_rows == int(
        window.session.sources[0].index.n)


def test_find_hits_survive_a_filter_as_pairs(window):
    """
    `Hits.locate`: the `(source, entry)` pairs outlive a filter and the row
    numbers do not, so the rows are re-derived rather than the scan re-run.
    """
    from logweave.session.find import Query, search

    query = Query(text="INFO")
    hits = search(window.session, query)
    window.finder.result = hits
    window._hits_ready(hits)
    before_found, before_count = hits.found, hits.count

    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.ERROR)]))
    assert hits.found == before_found, "a hit was thrown away"
    assert hits.count <= before_count
    assert hits.hidden == hits.found - hits.count


def test_the_goto_dialog_parses_and_seeks(window, monkeypatch):
    from logweave.ui import mainwindow as mw

    lo, _hi = window.session.time_span()

    class Fake:
        def __init__(self, *_a, **_k):
            pass

        def exec(self):
            return mw.QDialog.DialogCode.Accepted

        def wall_us(self):
            return lo

    monkeypatch.setattr(mw, "GoToTimeDialog", Fake)
    window.view.set_current_row(window.session.total_rows - 1)
    window.go_to_time()
    assert window.view.current_row() == window.session.time_to_row(lo)


def test_jumping_by_severity_lands_on_that_severity(window):
    window.view.set_current_row(0)
    window._jump_severity(int(Severity.WARNING))
    row = window.session.rows(window.view.current_row(), 1)[0]
    assert row.severity >= int(Severity.WARNING)


def test_jumping_by_file_stays_in_that_file(window):
    window.view.set_current_row(0)
    first = window.session.rows(0, 1)[0].source
    window._jump_source()
    got = window.session.rows(window.view.current_row(), 1)[0]
    assert got.source == first
    assert window.view.current_row() > 0


def test_a_jump_with_nowhere_to_go_says_so_rather_than_wrapping(window):
    window.view.set_current_row(0)
    window._jump_severity(int(Severity.CRITICAL), back=True)
    assert window.view.current_row() == 0
    assert "No" in window._status_msg.text()


def test_the_find_and_follow_shortcuts_do_not_collide(window):
    """
    M7 gave Ctrl+F to the find bar and moved following to Ctrl+Shift+F. Both
    have to be true at once, which is why this asserts on both.
    """
    assert window.find_act.shortcut().toString() == "Ctrl+F"
    assert window.follow_act.shortcut().toString() == "Ctrl+Shift+F"
    assert window.find_act.shortcut() != window.follow_act.shortcut()


def test_the_view_is_told_about_the_hit_it_is_standing_on(window):
    from logweave.session.find import Query, search

    query = Query(text="INFO")
    hits = search(window.session, query)
    window.finder.result = hits
    window._hits_ready(hits)
    assert window.view._find_row == window._current_hit
    assert window.timeline._find_time is not None


def test_the_sources_menu_follows_the_session(window):
    """A filter naming source 3 of a two-source session would hide one."""
    window.filterbar.set_sources(["a", "b", "c"])
    window.filterbar.set_source_set({2})
    window.filterbar.set_sources(["a", "b"])
    assert window.filterbar.filter().sources == frozenset()
