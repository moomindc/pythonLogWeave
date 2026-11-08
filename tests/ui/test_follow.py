"""
The Qt side of following -- FR-21, FR-22, FR-24, FR-25, FR-26.

Driven with a hand-stepped `Poller`, like the engine tests: nothing here sleeps
or spins an event loop waiting for an interval. `MainWindow` takes the poller as
a constructor argument for exactly this reason.

The notifier's signal is not exercised across a real thread boundary here --
that is the one part Qt itself is responsible for -- but the slot it lands on is,
because that slot is where every mistake about ordering would live.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.session.session import Session  # noqa: E402
from logweave.source.poller import Poller  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402


def line(src: str, second: int, i: int) -> str:
    h, rest = divmod(int(second), 3600)
    m, sec = divmod(rest, 60)
    return f"2026-11-08 {h:02d}:{m:02d}:{sec:02d}.000 INFO  {src} line {i}\n"


def write(path, src, seconds) -> None:
    path.write_text("".join(line(src, s, s) for s in seconds), encoding="utf-8")


def add(path, src, seconds) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for s in seconds:
            handle.write(line(src, s, s))


@pytest.fixture
def poller():
    p = Poller(interval=3600.0)
    yield p
    p.stop()


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def window(app, tmp_path, poller):
    """
    A window over one 60-line log, watched through a poller the test steps.

    Not shown. `isVisibleTo` is used instead of `isVisible` for the same reason
    the strip menu items need it (see `MainWindow._build_menus`): a child of an
    unmapped window reports `isVisible()` False however many times it has been
    shown, so asserting on `isVisible` here would pass for the wrong reason.
    """
    path = tmp_path / "svc.log"
    write(path, "svc", range(60))
    session = Session(use_cache=False)
    session.add_files([path])
    win = MainWindow(session, background=False, poller=poller)
    win.resize(900, 400)
    yield win, path
    win.follow.stop()
    win.session.close()
    win.deleteLater()


def tick(win, poller):
    """One watcher pass, then the slot the notifier's signal lands on."""
    poller.poll_once()
    win._follow_woken()


# ---------------------------------------------------------------------------
# FR-24: the switch
# ---------------------------------------------------------------------------

def test_following_is_off_until_the_menu_item_is_ticked(window, poller):
    win, path = window
    assert not win.follow_act.isChecked()
    assert poller.watching == 0

    add(path, "svc", [100])
    tick(win, poller)
    assert win.session.total_rows == 60


def test_ticking_the_menu_item_starts_watching_and_tailing(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    assert poller.watching == 1
    assert win.view.tailing
    assert win.view.at_tail()


def test_the_global_toggle_is_remembered(window, poller, isolated_settings):
    from logweave.ui import settings

    win, _path = window
    win.follow_act.setChecked(True)
    assert settings.read_bool(settings.FOLLOW, False) is True
    win.follow_act.setChecked(False)
    assert settings.read_bool(settings.FOLLOW, True) is False


def test_turning_following_off_stops_watching_and_leaves_the_rows_alone(
        window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    add(path, "svc", [100, 101])
    tick(win, poller)
    rows = win.session.total_rows

    win.follow_act.setChecked(False)
    assert poller.watching == 0
    add(path, "svc", [102])
    tick(win, poller)
    assert win.session.total_rows == rows


# ---------------------------------------------------------------------------
# FR-21 and FR-22: the lines arrive, and the view goes with them
# ---------------------------------------------------------------------------

def test_an_append_shows_up_and_the_view_follows_it(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    before = win.session.total_rows

    add(path, "svc", range(100, 110))
    tick(win, poller)

    assert win.session.total_rows == before + 10
    assert win.view.at_tail()
    assert win._new_lines.count == 0


def test_scrolling_up_pauses_auto_scroll_without_stopping_ingestion(window, poller):
    """
    FR-22 is explicit that the pause is about the viewport and not about
    reading: the lines must still arrive, they just must not drag the screen.
    """
    win, path = window
    win.follow_act.setChecked(True)
    win.view.scroll_to_row(0)
    assert not win.view.tailing

    before = win.session.total_rows
    top_before = win.view.top_row()
    add(path, "svc", range(100, 112))
    tick(win, poller)

    assert win.session.total_rows == before + 12       # still ingesting
    assert win.view.top_row() == top_before            # but not moving
    assert not win.view.tailing


def test_end_resumes_following(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    win.view.scroll_to_row(0)
    assert not win.view.tailing

    QTest.keyClick(win.view, Qt.Key.Key_End)
    assert win.view.tailing
    assert win.view.at_tail()


def test_shift_end_selects_without_turning_following_back_on(window, poller):
    """
    Shift+End is a selection to the end of the log. It lands at the tail, so the
    resume-on-arrival rule would fire -- and the next append would then scroll
    the selection the user is still making off the top of the screen. A
    selection spanning more than one row therefore suppresses the resume.
    """
    win, _path = window
    win.follow_act.setChecked(True)
    win.view.scroll_to_row(0)
    win.view.set_current_row(0)

    QTest.keyClick(win.view, Qt.Key.Key_End, Qt.KeyboardModifier.ShiftModifier)
    assert not win.view.tailing
    assert win.view.selection() == (0, win.session.total_rows - 1)


def test_scrolling_back_down_to_the_bottom_resumes_following(window, poller):
    win, _path = window
    win.follow_act.setChecked(True)
    win.view.scroll_to_row(0)
    assert not win.view.tailing

    bar = win.view.verticalScrollBar()
    bar.setValue(bar.maximum())
    assert win.view.tailing


def test_auto_scroll_moving_the_view_does_not_count_as_a_user_scroll(window, poller):
    """
    The trap this guards. Auto-scroll moves the scrollbar, and the only evidence
    FR-22 has for "the user scrolled up" is a scrollbar that moved -- so without
    a guard, following would switch itself off on the very first append it
    handled.
    """
    win, path = window
    win.follow_act.setChecked(True)
    for start in range(100, 160, 10):
        add(path, "svc", range(start, start + 10))
        tick(win, poller)
        assert win.view.tailing, f"stopped following after the append at {start}"


# ---------------------------------------------------------------------------
# FR-26: "N new lines below"
# ---------------------------------------------------------------------------

def test_the_indicator_counts_lines_that_arrived_while_paused(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    win.view.scroll_to_row(0)
    assert not win._new_lines.isVisibleTo(win)

    add(path, "svc", range(100, 107))
    tick(win, poller)
    assert win._new_lines.count == 7
    assert "7 new lines below" in win._new_lines.text()

    add(path, "svc", range(200, 203))
    tick(win, poller)
    assert win._new_lines.count == 10          # accumulates across ticks


def test_the_indicator_is_singular_for_one_line(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    win.view.scroll_to_row(0)
    add(path, "svc", [100])
    tick(win, poller)
    assert "1 new line below" in win._new_lines.text()


def test_pressing_the_indicator_goes_to_the_end_and_clears_it(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    win.view.scroll_to_row(0)
    add(path, "svc", range(100, 105))
    tick(win, poller)
    assert win._new_lines.count == 5

    win._new_lines.click()

    assert win.view.tailing
    assert win.view.at_tail()
    assert win._new_lines.count == 0
    assert not win._new_lines.isVisibleTo(win)


def test_the_indicator_stays_hidden_while_following(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    add(path, "svc", range(100, 110))
    tick(win, poller)
    assert win._new_lines.count == 0
    assert not win._new_lines.isVisibleTo(win)


# ---------------------------------------------------------------------------
# FR-25 and FR-23: stale sources and rotation, in the dock
# ---------------------------------------------------------------------------

def test_a_following_source_is_marked_in_the_source_list(window, poller):
    win, _path = window
    win.follow_act.setChecked(True)
    item = win.source_list.item(0)
    assert "●" in item.text()
    assert "following" in item.toolTip()


def test_a_vanished_source_is_marked_stale_and_stays_in_the_list(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    rows = win.session.total_rows

    os.remove(path)
    tick(win, poller)

    item = win.source_list.item(0)
    assert "⊘" in item.text()
    assert "stale" in item.toolTip()
    assert win.source_list.count() == 1
    assert win.session.total_rows == rows        # still readable
    assert "stale" in win._status_msg.text()


def test_a_rotation_reports_itself_and_lists_both_files(window, poller, tmp_path):
    win, path = window
    win.follow_act.setChecked(True)

    os.replace(path, tmp_path / "svc.log.1")
    write(path, "svc", [500, 501])
    tick(win, poller)

    assert win.source_list.count() == 2
    assert [win.source_list.item(i).text().split("   ")[0] for i in range(2)] == [
        "svc (rotated)", "svc"]
    assert "rotated" in win._status_msg.text()


def test_a_truncation_reports_itself(window, poller):
    win, path = window
    win.follow_act.setChecked(True)
    write(path, "svc", [900])
    tick(win, poller)
    assert "truncated" in win._status_msg.text()
    assert win.session.total_rows == 1


def test_the_per_source_toggle_leaves_the_file_in_the_merge(window, poller):
    """
    FR-9 and FR-24 are different switches. Turning follow off for a file must
    not take its rows out of the view, which is what the checkbox beside it does.
    """
    win, path = window
    win.follow_act.setChecked(True)
    rows = win.session.total_rows

    win._source_follow_toggled(0, False)

    assert poller.watching == 0
    assert win.session.total_rows == rows
    assert win.session.sources[0].meta.enabled is True
    assert "follow is off for this file" in win.source_list.item(0).toolTip()


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def test_closing_everything_stops_the_watchers(window, poller):
    """
    The watchers hold a callback that reaches this window and states keyed by
    source id, so both have to go before the session does -- otherwise a file
    written to a moment later wakes a tick that looks up sources that are gone.
    """
    win, path = window
    win.follow_act.setChecked(True)
    assert poller.watching == 1

    win.close_all()

    assert poller.watching == 0
    assert win.follow.states == {}
    assert win.session.total_rows == 0
    add(path, "svc", [100])
    tick(win, poller)                       # must not raise
    assert win.session.total_rows == 0


def test_a_file_opened_while_following_is_followed(window, poller, tmp_path):
    win, _path = window
    win.follow_act.setChecked(True)
    other = tmp_path / "other.log"
    write(other, "other", range(20))

    win.open_paths([other])

    assert poller.watching == 2
    add(other, "other", [100])
    tick(win, poller)
    assert any("other line 100" in r.text
               for r in win.session.rows(win.session.total_rows - 1, 1))


def test_the_status_bar_says_what_following_is_doing(window, poller):
    win, path = window
    assert win._status_follow.text() == ""

    win.follow_act.setChecked(True)
    assert "following 1" in win._status_follow.text()

    win.view.scroll_to_row(0)
    assert "paused" in win._status_follow.text()

    os.remove(path)
    tick(win, poller)
    assert "stale" in win._status_follow.text()
