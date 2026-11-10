"""
The timestamp column in the window -- FR-35.

`tests/unit/test_timefmt.py` holds the renderings themselves. What is tested
here is the wiring, which is where the two interesting failures live:

* the column's **width** is measured from the format, so a change of format
  has to re-measure it or the message column starts in the wrong place;
* the relative modes are sized from the **session's span** and elapsed mode
  needs a zero point out of the open log, neither of which the view knows --
  the window builds the formatter and hands it over, and it has to do that
  again whenever a file is opened.

The offscreen backend has no font database, so nothing here asserts a pixel
width. What is asserted is that the view re-measured at all, and what reached
the painter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.highlight import default_group  # noqa: E402
from logweave.index.civil import US_PER_SEC, format_wall_us  # noqa: E402
from logweave.session import (
    Session,  # noqa: E402
    timefmt,  # noqa: E402
)
from logweave.session.rowformat import Preset  # noqa: E402
from logweave.session.timefmt import TimeDisplay, TimeMode  # noqa: E402
from logweave.ui import settings  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402
from logweave.ui.settingsdialog import SettingsDialog, Settled  # noqa: E402


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def log(tmp_path) -> Path:
    """
    Two hundred rows over three and a bit minutes, with one deliberate stall.

    The stall is what delta mode exists for, so it is in the fixture rather
    than in one test: a log with perfectly even spacing would let a broken
    delta column look plausible.
    """
    path = tmp_path / "svc.log"
    with path.open("w", encoding="utf-8") as fh:
        for i in range(200):
            # Rows step a second each; row 100 also skips 40, so the step
            # into it is 41 seconds -- the stall plus the ordinary step.
            sec = i + (40 if i >= 100 else 0)
            fh.write(f"2026-10-04 10:{sec // 60:02d}:{sec % 60:02d}.000000 "
                     f"INFO served request {i}\n")
    return path


@pytest.fixture
def window(app, log):
    session = Session(use_cache=False)
    session.add_files([log])
    win = MainWindow(session, background=False)
    win.resize(1200, 700)
    yield win
    win.marks.stop()
    win.follow.stop()
    session.close()
    win.deleteLater()


def stamps(window, first: int = 0, count: int = 6) -> list[str]:
    """What the timestamp column renders for a band of rows."""
    fmt = window.view.time_format
    rows = window.session.rows(first, count)
    previous = (window.session.row_to_time(first - 1) if fmt.needs_previous
                and first > 0 else None)
    out = []
    for r in rows:
        out.append(fmt.render(r.ts, previous))
        previous = r.ts
    return out


# ---------------------------------------------------------------------------
# the format reaching the view
# ---------------------------------------------------------------------------

def test_the_column_defaults_to_the_rendering_everything_else_cites(window):
    row = window.session.rows(0, 1)[0]
    assert stamps(window, 0, 1)[0] == format_wall_us(row.ts)


def test_changing_the_pattern_re_measures_the_column(window):
    """
    The message column starts where this one ends, so a format that is not
    re-measured leaves the message overlapping or indented by the old width.
    """
    before = window.view.message_left
    window.time_display = TimeDisplay(pattern="%H:%M:%S")
    window._apply_time_display()
    assert window.view.message_left != before
    assert stamps(window, 0, 1)[0] == "10:00:00"


def test_delta_mode_measures_from_the_row_above(window):
    window._set_time_mode(TimeMode.DELTA)
    got = stamps(window, 99, 3)
    assert got[1].strip() == "+41.000000", "the stall at row 100"
    assert got[2].strip() == "+01.000000"


def test_the_top_row_of_a_delta_column_is_blank(window):
    window._set_time_mode(TimeMode.DELTA)
    assert stamps(window, 0, 1)[0].strip() == ""


def test_elapsed_mode_starts_at_the_first_row_by_default(window):
    window._set_time_mode(TimeMode.ELAPSED)
    assert stamps(window, 0, 1)[0].strip() == "+00.000000"
    assert stamps(window, 10, 1)[0].strip() == "+10.000000"


def test_the_zero_point_is_the_row_the_cursor_is_on(window):
    """
    FR-35's "± from a chosen zero point". Setting it also switches the mode,
    because setting one in clock-time mode would change nothing on screen.
    """
    window.view.set_current_row(10)
    window._set_zero_here()
    assert window.time_display.mode is TimeMode.ELAPSED
    assert stamps(window, 10, 1)[0].strip() == "+00.000000"
    assert stamps(window, 0, 1)[0].strip() == "-10.000000"


def test_setting_a_zero_point_with_nothing_open(app):
    """A command with no rows to act on reports, rather than raising."""
    win = MainWindow(background=False)
    try:
        win._set_zero_here()
        assert "No rows" in win._status_msg.text()
    finally:
        win.follow.stop()
        win.deleteLater()


def test_the_relative_column_is_sized_from_the_open_log(window):
    """
    Sized from the span, so a log covering three minutes does not reserve the
    hour and day groups: four characters of message on every row.
    """
    window._set_time_mode(TimeMode.DELTA)
    lo, hi = window.session.time_span()
    assert (hi - lo) < 3600 * US_PER_SEC
    assert window.view.time_format.sample == "-59:59.999999"


def test_opening_another_file_resizes_the_relative_column(window, tmp_path):
    """
    The span is a fact about what is open, and the window is the only thing
    that notices it has changed -- the view holds a built formatter.
    """
    window._set_time_mode(TimeMode.DELTA)
    narrow = window.view.time_format.sample

    later = tmp_path / "next-day.log"
    later.write_text("2026-10-06 11:00:00.000000 INFO much later\n",
                     encoding="utf-8")
    window.open_paths([later])
    assert window.view.time_format.sample != narrow
    assert window.view.time_format.sample.count(":") == 3, "a day group now"


# ---------------------------------------------------------------------------
# how the choice is remembered, and how it is explained
# ---------------------------------------------------------------------------

def test_the_mode_and_pattern_are_remembered_but_the_zero_point_is_not(window, app):
    """
    A zero point is a row in the log that is open -- "measure from the
    deploy" -- so carrying one into tomorrow's incident would be meaningless
    and, worse, invisible.
    """
    window.view.set_current_row(10)
    window._set_zero_here()
    window.time_display = TimeDisplay(mode=window.time_display.mode,
                                      pattern="%H:%M:%S",
                                      zero_us=window.time_display.zero_us)
    settings.write_time_display(window.time_display)

    restored = settings.time_display()
    assert restored.mode is TimeMode.ELAPSED
    assert restored.pattern == "%H:%M:%S"
    assert restored.zero_us == timefmt.ZERO_FIRST_ROW


def test_a_corrupt_stored_pattern_falls_back_rather_than_breaking_the_view(app):
    settings.write(settings.TIME_PATTERN, "%Q nonsense")
    settings.write(settings.TIME_MODE, "sideways")
    got = settings.time_display()
    assert got.pattern == timefmt.FULL
    assert got.mode is TimeMode.ABSOLUTE


def test_a_relative_column_still_says_the_clock_time_of_the_cursor(window):
    """
    FR-35 gives the reader offsets; the status bar keeps them anchored, so
    nobody has to switch modes to find out when something actually happened.
    """
    window._set_time_mode(TimeMode.DELTA)
    window.view.set_current_row(5)
    window._update_position(5)
    row = window.session.rows(5, 1)[0]
    assert format_wall_us(row.ts) in window._status_pos.text()


def test_the_menu_ticks_follow_the_mode(window):
    window._set_time_mode(TimeMode.DELTA)
    window._sync_setting_menus()
    ticked = [a.data() for a in window.time_acts if a.isChecked()]
    assert ticked == [TimeMode.DELTA.value]


# ---------------------------------------------------------------------------
# the dialog, and what a copy does with the choice
# ---------------------------------------------------------------------------

def test_the_dialog_round_trips_a_format(app):
    d = SettingsDialog(default_group(),
                       current=Settled(font_point=11, lane_width=6,
                                       time_display=TimeDisplay(pattern="%H:%M:%S")))
    assert d.appearance.time.pattern.text() == "%H:%M:%S"
    seen = []
    d.changed.connect(seen.append)
    d.appearance.time.mode.setCurrentIndex(
        d.appearance.time.mode.findData(TimeMode.DELTA.value))
    d.apply()
    assert seen[0].time_display.mode is TimeMode.DELTA
    assert seen[0].time_display.pattern == "%H:%M:%S"


def test_the_dialog_refuses_a_pattern_that_renders_nothing(app):
    d = SettingsDialog(default_group(),
                       current=Settled(font_point=11, lane_width=6))
    d.appearance.time.pattern.setText("%Q")
    d.appearance.time._pattern_edited()
    assert "%Q" in d.appearance.time.advice.text()
    # Settled anyway, because a `Settled` is installed wholesale and an
    # unusable pattern must not reach the paint path.
    assert d.appearance.time.settled().pattern == timefmt.FULL


def test_applying_the_dialog_keeps_the_zero_point_the_menu_set(window):
    """
    The dialog has no control for the zero point -- it is a row in the open
    log -- so it must not clear one by being applied.
    """
    window.view.set_current_row(10)
    window._set_zero_here()
    zero = window.time_display.zero_us
    assert zero != timefmt.ZERO_FIRST_ROW

    settled = Settled(font_point=window.view.font_point,
                      lane_width=window.view.lane_width,
                      time_display=TimeDisplay(mode=TimeMode.ELAPSED,
                                               pattern="%H:%M:%S"))
    window._settings_changed(settled)
    assert window.time_display.zero_us == zero


def test_as_displayed_copies_the_column_the_reader_was_looking_at(window):
    """
    FR-46b's `aligned` means what is on screen, and in delta mode what is on
    screen is an offset.
    """
    window._set_time_mode(TimeMode.DELTA)
    window.view.set_current_row(100)
    window.view.set_current_row(101, extend=True)
    window.view.copy_as(Preset.ALIGNED)
    text = QApplication.clipboard().text()
    assert "+41.000000" in text


def test_an_attributed_copy_keeps_clock_time(window):
    """
    Every other preset prints the time the file states: `+40.000000` in a
    ticket cites nothing a colleague can find, and a CSV of deltas cannot be
    sorted back into time order.
    """
    window._set_time_mode(TimeMode.DELTA)
    window.view.set_current_row(100)
    window.view.set_current_row(101, extend=True)
    window.view.copy_as(Preset.ANNOTATED)
    text = QApplication.clipboard().text()
    assert "+41.000000" not in text
    assert format_wall_us(window.session.rows(100, 1)[0].ts) in text
