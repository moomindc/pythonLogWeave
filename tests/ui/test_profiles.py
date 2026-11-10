"""
Profiles and bookmarks in the window -- FR-37, FR-49 to FR-54, M8.

The engine side of both is tested without Qt (`tests/unit/test_profile.py`,
`tests/unit/test_bookmarks.py`): what a profile file means, what a glob
resolves to, how a bookmark survives a reopen. What is tested here is the part
only a window can answer -- that saving produces a file which, opened into a
*different* window, puts that window back the way the first one was, and that
an open which could not do all of it says so instead of pretending.

Everything runs with `background=False`, so an open completes inside the call.
The two halves of applying a profile (`open_profile` and
`_apply_pending_profile`) still both run on that path, which is the point of
splitting it that way: the synchronous route is not a special case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave import profile as pf  # noqa: E402
from logweave.index.severity import Severity  # noqa: E402
from logweave.session.filters import Filter  # noqa: E402
from logweave.session.timefmt import TimeDisplay, TimeMode  # noqa: E402
from logweave.ui import association, settings  # noqa: E402
from logweave.ui.app import split_arguments  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402
from logweave.ui.reportpanel import MAX_LINES, ReportPanel  # noqa: E402


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def window(app):
    w = MainWindow(background=False)
    w.resize(1200, 800)
    yield w
    w.session.close()
    w.deleteLater()


@pytest.fixture
def other(app):
    """A second, independent window -- where a saved profile is opened."""
    w = MainWindow(background=False)
    w.resize(1200, 800)
    yield w
    w.session.close()
    w.deleteLater()


@pytest.fixture
def logs(corpus):
    return sorted((corpus / "basic").glob("*.log"))


# ---------------------------------------------------------------------------
# saving and opening (FR-49, FR-50)
# ---------------------------------------------------------------------------


def test_saving_writes_a_readable_profile_naming_every_open_source(
        window, logs, tmp_path):
    window.open_paths(logs)
    path = window._write_profile(tmp_path / "session.lwprofile") or \
        tmp_path / "session.lwprofile"

    text = path.read_text(encoding="utf-8")
    assert "format_version = 1" in text
    assert text.count("[[source]]") == len(logs)
    # Absolute paths, never globs, never relative (D10, FR-50), and readable:
    # a Windows path is written as a TOML literal string, not doubled.
    assert f"path = '{logs[0]}'" in text
    reread = pf.load(path)
    assert [spec.target for spec in reread.sources] == [str(x) for x in logs]
    assert window.profile_path == path
    assert "saved session.lwprofile" in window._status_msg.text()


def test_nothing_is_saved_when_no_logs_are_open(window, tmp_path):
    window._write_profile(tmp_path / "empty.lwprofile")
    assert not (tmp_path / "empty.lwprofile").exists()
    assert "no logs are open" in window._status_msg.text()


def test_a_saved_profile_reopens_the_same_session_in_another_window(
        window, other, logs, tmp_path):
    window.open_paths(logs)
    window.rename_source(0, "gateway")
    window.session.sources[1].meta.skew_us = -2_500_000
    window.view.set_font_point(13)
    window.time_display = TimeDisplay(mode=TimeMode.ELAPSED)
    window._apply_time_display()
    path = tmp_path / "round-trip.lwprofile"
    window._write_profile(path)

    other.open_profile(path)

    assert [s.alias for s in other.session.sources] == \
        [s.alias for s in window.session.sources]
    assert [s.meta.colour for s in other.session.sources] == \
        [s.meta.colour for s in window.session.sources]
    assert other.session.sources[1].meta.skew_us == -2_500_000
    assert other.session.sources[0].meta.alias_custom
    assert other.view.font_point == 13
    assert other.time_display.mode is TimeMode.ELAPSED
    assert other.session.total_rows == window.session.total_rows
    assert other.windowTitle().endswith("round-trip")


def test_opening_a_profile_closes_what_was_already_open(window, logs, tmp_path):
    """
    A profile is a session, not an import.

    Merging it into the files already on screen would give the reader a stream
    that neither the profile nor what they had describes.
    """
    path = tmp_path / "one.lwprofile"
    window.open_paths([logs[0]])
    window._write_profile(path)

    window.close_all()
    window.open_paths(logs)
    assert len(window.session.sources) == len(logs)

    window.open_profile(path)
    assert len(window.session.sources) == 1


def test_a_profiles_filter_is_restored_and_offered_on_the_menu(
        window, other, logs, tmp_path):
    window.open_paths(logs)
    window.open_filter()
    window.filterbar.set_filter(Filter(severities=frozenset({int(Severity.ERROR)})))
    path = tmp_path / "errors.lwprofile"
    window._write_profile(path)

    other.open_profile(path)

    assert other.session.is_filtered
    assert other.session.filter.filt.severities == {int(Severity.ERROR)}
    assert other.saved_filters
    assert other.saved_filter_menu.isEnabled()
    assert len(other.saved_filter_menu.actions()) == 1


def test_a_saved_filter_naming_a_source_that_is_not_open_is_reported(
        window, logs, tmp_path):
    """
    Not silently widened to "everything".

    A filter that was "these two services" and quietly becomes "all of them"
    shows rows the reader believes they excluded, which is the kind of wrong
    that gets believed.
    """
    profile = pf.Profile(
        sources=[pf.SourceSpec(path=str(logs[0]))],
        filters=[pf.SavedFilter(name="payments", sources=("payments",))],
    )
    path = pf.save(profile, tmp_path / "ghost.lwprofile")

    window.open_profile(path)

    assert window.report.isVisibleTo(window)
    assert "no open source called 'payments'" in window.report.detail.toolTip()


# ---------------------------------------------------------------------------
# an open that could not do all of it (FR-52)
# ---------------------------------------------------------------------------


def test_a_missing_source_is_reported_and_the_rest_still_open(
        window, logs, tmp_path):
    profile = pf.Profile(sources=[
        pf.SourceSpec(path=str(logs[0])),
        pf.SourceSpec(path=str(tmp_path / "gone.log")),
    ])
    path = pf.save(profile, tmp_path / "partly.lwprofile")

    window.open_profile(path)

    assert len(window.session.sources) == 1
    assert window.report.isVisibleTo(window)
    assert "gone.log" in window.report.detail.text()
    assert window.report.retry.isVisibleTo(window.report)


def test_a_glob_that_matches_nothing_is_reported_as_such(window, tmp_path):
    profile = pf.Profile(sources=[pf.SourceSpec(glob=str(tmp_path / "*.log"))])
    path = pf.save(profile, tmp_path / "empty-glob.lwprofile")

    window.open_profile(path)

    assert window.session.is_empty
    assert "matched nothing" in window.report.detail.text()
    assert "nothing to open" in window._status_msg.text()


def test_retry_opens_the_file_that_has_since_appeared(window, logs, tmp_path):
    """
    FR-52's Retry, which is why the panel exists at all.

    A profile written for a rotating set of logs legitimately has an unmatched
    glob most mornings; the file arrives when the writer rotates, and what the
    reader wants then is it added to the session they are already reading.
    """
    folder = tmp_path / "rotating"
    folder.mkdir()
    profile = pf.Profile(sources=[pf.SourceSpec(glob=str(folder / "*.log"))])
    path = pf.save(profile, tmp_path / "rotate.lwprofile")

    window.open_profile(path)
    assert window.session.is_empty

    (folder / "gateway.log").write_bytes(logs[0].read_bytes())
    window.retry_profile_sources()

    assert len(window.session.sources) == 1
    assert "1 source" in window._status_msg.text()


def test_retry_does_not_open_a_file_twice(window, logs, tmp_path):
    profile = pf.Profile(sources=[pf.SourceSpec(path=str(logs[0]))])
    path = pf.save(profile, tmp_path / "once.lwprofile")

    window.open_profile(path)
    window.retry_profile_sources()

    assert len(window.session.sources) == 1
    assert "no new sources" in window._status_msg.text()


def test_a_profile_that_is_not_a_profile_is_reported_not_raised(
        window, tmp_path):
    path = tmp_path / "broken.lwprofile"
    path.write_text("this is not toml = = =\n", encoding="utf-8")

    window.open_profile(path)

    assert window.session.is_empty
    assert window.report.isVisibleTo(window)
    assert not window.report.retry.isVisibleTo(window.report)


def test_save_as_writes_where_the_dialog_pointed(
        window, logs, tmp_path, no_native_dialogs):
    """
    The Save As branch, which until the dialogs were stubbed could not be
    tested at all: it goes through `QFileDialog`, and a real one waits for a
    click. Cancelling it must leave nothing behind, and choosing a path must
    make that path the window's profile from then on.
    """
    window.open_paths(logs)

    window.save_profile_as()            # cancelled: the stub answers ""
    assert no_native_dialogs.asked == ["save"]
    assert window.profile_path is None

    target = tmp_path / "chosen.lwprofile"
    no_native_dialogs.save = str(target)
    window.save_profile_as()

    assert target.exists()
    assert window.profile_path == target
    assert f"saved {target.name}" in window._status_msg.text()


def test_close_all_forgets_the_profile_so_save_cannot_overwrite_it(
        window, logs, tmp_path, no_native_dialogs):
    path = tmp_path / "kept.lwprofile"
    window.open_paths(logs)
    window._write_profile(path)
    before = path.read_text(encoding="utf-8")

    window.close_all()
    assert window.profile_path is None
    assert window.windowTitle() == "LogWeave"
    assert not window.saved_filters

    # Save now has nowhere to save to, so it asks -- and the asking is the
    # assertion: `no_native_dialogs` answers with a cancel, which is what makes
    # this a test and not a window waiting for somebody to click Save.
    window.save_profile()
    assert no_native_dialogs.asked == ["save"]
    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# bookmarks (FR-37)
# ---------------------------------------------------------------------------


def test_toggling_a_bookmark_marks_the_row_and_both_strips(window, logs):
    window.open_paths(logs)
    window.view.set_current_row(4)
    window.toggle_bookmark()

    assert len(window.bookmarks) == 1
    assert "1 bookmark" in window._status_bookmarks.text()
    assert window.minimap._bookmarks is not None
    assert window.timeline._bookmarks is not None

    window.toggle_bookmark()
    assert len(window.bookmarks) == 0
    assert window._status_bookmarks.text() == ""
    assert window.minimap._bookmarks is None


def test_stepping_moves_to_the_next_bookmark_and_stops_at_the_end(window, logs):
    window.open_paths(logs)
    for row in (2, 9):
        window.view.set_current_row(row)
        window.toggle_bookmark()

    window.view.set_current_row(0)
    window.next_bookmark()
    assert window.view.current_row() == 2
    window.next_bookmark()
    assert window.view.current_row() == 9

    # No wrap: a reader walking a set of marks is told when they reach the end.
    window.next_bookmark()
    assert window.view.current_row() == 9
    assert "no bookmark below" in window._status_msg.text()

    window.next_bookmark(back=True)
    assert window.view.current_row() == 2


def test_bookmarks_survive_a_save_and_reopen(window, other, logs, tmp_path):
    window.open_paths(logs)
    window.view.set_current_row(6)
    window.toggle_bookmark()
    marked = window.session.rows(6, 1)[0]
    path = tmp_path / "marked.lwprofile"
    window._write_profile(path)

    other.open_profile(path)

    assert len(other.bookmarks) == 1
    restored = other.bookmarks.rows(other.session)
    assert len(restored) == 1
    row = other.session.rows(int(restored[0]), 1)[0]
    assert (row.ts, row.line_no) == (marked.ts, marked.line_no)


def test_closing_everything_clears_the_bookmarks(window, logs):
    window.open_paths(logs)
    window.view.set_current_row(3)
    window.toggle_bookmark()
    window.close_all()

    assert len(window.bookmarks) == 0
    assert window._status_bookmarks.text() == ""


def test_clearing_says_how_many_went(window, logs):
    window.open_paths(logs)
    for row in (1, 5):
        window.view.set_current_row(row)
        window.toggle_bookmark()
    window.clear_bookmarks()
    assert "cleared 2 bookmarks" in window._status_msg.text()


def test_bookmarking_nothing_is_not_an_error(window):
    window.toggle_bookmark()
    window.next_bookmark()
    assert len(window.bookmarks) == 0
    assert "no bookmarks" in window._status_msg.text()


# ---------------------------------------------------------------------------
# recent lists (FR-54)
# ---------------------------------------------------------------------------


def test_opening_logs_remembers_them_and_opening_a_profile_remembers_that(
        window, logs, tmp_path):
    window.open_paths([logs[0]])
    assert [Path(p).name for p in settings.recent_logs()] == [logs[0].name]
    assert window.recent_logs_menu.isEnabled()

    path = tmp_path / "recent.lwprofile"
    window._write_profile(path)

    assert settings.recent_profiles() == [str(path)]
    assert window.recent_profiles_menu.isEnabled()
    # The profile's own files are not listed again: a profile is remembered as
    # a profile, and eight logs would push everything else out of the menu.
    assert [Path(p).name for p in settings.recent_logs()] == [logs[0].name]


def test_the_recent_menu_drops_a_file_that_has_gone(window, tmp_path):
    victim = tmp_path / "temporary.log"
    victim.write_text("2024-01-01 00:00:00 hello\n", encoding="utf-8")
    window.open_paths([victim])
    assert settings.recent_logs()

    victim.unlink()
    window._refresh_recent_menus()
    assert settings.recent_logs() == []
    assert not window.recent_logs_menu.isEnabled()


def test_clearing_the_recent_lists_empties_both(window, logs, tmp_path):
    window.open_paths([logs[0]])
    window._write_profile(tmp_path / "forgettable.lwprofile")
    window._forget_recent()

    assert settings.recent_logs() == []
    assert settings.recent_profiles() == []
    assert "cleared" in window._status_msg.text()


# ---------------------------------------------------------------------------
# the report panel, on its own
# ---------------------------------------------------------------------------


def test_the_report_panel_hides_itself_when_there_is_nothing_to_say(app):
    panel = ReportPanel()
    panel.report("nothing", [])
    assert panel.isHidden()
    panel.deleteLater()


def test_a_long_report_is_summarised_with_everything_in_the_tooltip(app):
    panel = ReportPanel()
    lines = [f"missing: file{i}.log" for i in range(MAX_LINES + 4)]
    panel.report("many", lines)

    shown = panel.detail.text().splitlines()
    assert len(shown) == MAX_LINES + 1
    assert shown[-1] == "...and 4 more"
    assert panel.detail.toolTip().splitlines() == lines
    panel.deleteLater()


# ---------------------------------------------------------------------------
# the command line and the file association (FR-53)
# ---------------------------------------------------------------------------


def test_a_profile_argument_is_told_from_a_log_argument():
    profiles, files = split_arguments(
        ["a.log", "b.lwprofile", "-v", "C:/logs/c.LWPROFILE"])
    assert [p.name for p in profiles] == ["b.lwprofile", "c.LWPROFILE"]
    assert [p.name for p in files] == ["a.log"]


def test_the_association_command_quotes_the_executable_and_takes_one_file():
    command = association.command()
    assert command.endswith('"%1"')
    assert command.startswith('"')


def test_the_association_state_is_reportable_wherever_the_tests_run():
    assert association.state() in (
        association.OURS, association.ABSENT, association.OTHER,
        association.UNSUPPORTED)
    assert pf.SUFFIX in association.describe()
