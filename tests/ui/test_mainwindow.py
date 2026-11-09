"""
The main window -- FR-1, FR-9, FR-19, M3.

Drag-and-drop is tested by synthesising the drop event rather than driving the
window manager: what matters is that a `text/uri-list` of local paths becomes
open, indexed sources, and that is the same code either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.ui.mainwindow import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def window(app):
    """
    A window that opens files synchronously.

    Most of what is tested here is what opening *produces* -- rows, the dock,
    the status bar -- not that it happens on a thread. Driving an event loop in
    every one of those tests would make them slow and flaky to no purpose; the
    background path has its own tests at the end of this file.
    """
    w = MainWindow(background=False)
    w.resize(1200, 800)
    yield w
    w.session.close()
    w.deleteLater()


_KEEPALIVE: list[QMimeData] = []
"""
`QDropEvent` stores a raw pointer to its `QMimeData` and does not own it.

In a real drag Qt keeps the payload alive for the duration of the drop; in a test
the `QMimeData` is an ordinary Python object, so letting it fall out of scope
leaves the event pointing at freed memory and the next access is an access
violation that takes the whole interpreter down -- not an exception. Hence the
explicit references.
"""


def mime_with(paths: list[Path] | None = None, text: str = "") -> QMimeData:
    mime = QMimeData()
    if paths is not None:
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    if text:
        mime.setText(text)
    _KEEPALIVE.append(mime)
    return mime


def drop_event(paths: list[Path]) -> QDropEvent:
    return QDropEvent(
        QPoint(10, 10), Qt.DropAction.CopyAction, mime_with(paths),
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )


def test_a_new_window_is_empty_and_says_so(window):
    assert window.session.is_empty
    assert window.source_list.count() == 0
    assert "No logs open" in window._status_msg.text()


def test_the_window_accepts_drops(window):
    assert window.acceptDrops()


def test_dropping_files_opens_and_merges_them(window, corpus):
    paths = sorted((corpus / "basic").glob("*.log"))
    window.dropEvent(drop_event(paths))

    assert len(window.session.sources) == len(paths)
    assert window.session.total_rows == sum(
        s.index.n for s in window.session.sources
    )
    assert "3 opened" in window._status_msg.text()


def test_dropping_a_directory_opens_the_files_in_it(window, corpus):
    """
    A directory is a reasonable thing to drag at a log viewer. One level only --
    recursing a log tree can mean thousands of files with no way to undo it.
    """
    window.dropEvent(drop_event([corpus / "basic"]))
    opened = {Path(s.meta.origin).name for s in window.session.sources}
    assert opened == {"app.log", "iis.log", "SOA.log"}
    assert "manifest.json" not in opened, (
        "a directory drop should not pull in the json sitting beside the logs"
    )


def test_an_explicitly_dropped_file_is_never_filtered_by_extension(window, corpus):
    """
    Dragging one file is a decision about that file. Filtering it would make an
    unusual extension impossible to open, which is the opposite of helpful.
    """
    odd = corpus / "basic" / "manifest.json"
    window.dropEvent(drop_event([odd]))
    assert len(window.session.sources) == 1
    assert Path(window.session.sources[0].meta.origin).name == "manifest.json"


def test_dropping_more_files_adds_to_the_merge_rather_than_replacing_it(window, corpus):
    first = [corpus / "basic" / "app.log"]
    window.dropEvent(drop_event(first))
    rows_after_one = window.session.total_rows

    window.dropEvent(drop_event([corpus / "basic" / "SOA.log"]))
    assert len(window.session.sources) == 2
    assert window.session.total_rows > rows_after_one


def test_a_drop_with_no_urls_is_ignored(window):
    event = QDropEvent(
        QPoint(0, 0), Qt.DropAction.CopyAction, mime_with(text="not a file"),
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    assert window.session.is_empty


def test_drag_enter_is_accepted_for_files_and_refused_for_text(window):
    ev = QDragEnterEvent(
        QPoint(0, 0), Qt.DropAction.CopyAction, mime_with([Path("x.log")]),
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(ev)
    assert ev.isAccepted()

    ev2 = QDragEnterEvent(
        QPoint(0, 0), Qt.DropAction.CopyAction, mime_with(text="hello"),
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    ev2.setAccepted(False)
    window.dragEnterEvent(ev2)
    assert not ev2.isAccepted()


# ---------------------------------------------------------------------------
# the source dock (FR-28, FR-29, FR-9)
# ---------------------------------------------------------------------------

def test_the_dock_lists_every_source_with_its_entry_count(window, corpus):
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert window.source_list.count() == 3
    for i, s in enumerate(window.session.sources):
        item = window.source_list.item(i)
        assert s.alias in item.text()
        assert f"{s.index.n:,}" in item.text()
        assert s.index.format_name in item.toolTip()


def test_every_source_gets_its_own_lane_colour(window, corpus):
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    colours = [s.colour for s in window.session.sources]
    assert len(set(colours)) == len(colours)


def test_unchecking_a_source_hides_its_rows(window, corpus):
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    total = window.session.total_rows
    hidden = window.session.sources[0].index.n

    window.source_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert window.session.total_rows == total - hidden

    window.source_list.item(0).setCheckState(Qt.CheckState.Checked)
    assert window.session.total_rows == total


def test_rebuilding_the_dock_does_not_toggle_anything(window, corpus):
    """
    `itemChanged` fires while the list is being repopulated, so the reload must
    block signals -- otherwise opening a fourth file silently hides the first.
    """
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    window.source_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    reduced = window.session.total_rows

    window.open_paths([corpus / "formats" / "ymd_hms_dot_f.log"])
    assert window.session.sources[1].meta.enabled is False
    assert window.session.total_rows == reduced + window.session.sources[-1].index.n
    assert window.source_list.item(1).checkState() == Qt.CheckState.Unchecked


# ---------------------------------------------------------------------------
# reporting (FR-19)
# ---------------------------------------------------------------------------

def test_the_status_bar_reports_the_time_span(window, corpus):
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert " to " in window._status_span.text()


def test_the_status_bar_cites_the_file_and_line_of_the_current_row(window, corpus):
    """D9 again: the position readout is the user's first sight of attribution."""
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    window.view.set_current_row(42)
    text = window._status_pos.text()
    assert "row 43" in text
    row = window.session.rows(42, 1)[0]
    assert f"{window.session.sources[row.source].alias}:{row.line_no}" in text


def test_a_file_that_cannot_be_opened_is_reported_in_the_status_bar(window, tmp_path, corpus):
    """
    FR-19, and no modal dialog: the other files in the batch must still be
    readable without dismissing anything. A modal here used to hang the window.
    """
    missing = tmp_path / "nope.log"
    window.open_paths([corpus / "basic" / "app.log", missing])

    assert len(window.session.sources) == 1
    assert window.session.total_rows > 0
    assert "failed" in window._status_msg.text()
    assert "nope.log" in window._status_msg.text()
    assert "nope.log" in window._status_msg.toolTip()


def test_close_all_empties_the_window_without_destroying_it(window, corpus):
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    window.close_all()
    assert window.session.is_empty
    assert window.source_list.count() == 0
    assert window.view.session is window.session
    assert "No logs open" in window._status_msg.text()

    # and it still works afterwards
    window.open_paths([corpus / "basic" / "app.log"])
    assert window.session.total_rows > 0


def test_no_shortcut_is_claimed_twice(window):
    """
    Qt answers an ambiguous shortcut by doing nothing.

    When two bindings in the same window resolve to one sequence, pressing it
    logs `QAction::event: Ambiguous shortcut overload: F3` to stderr and fires
    neither action -- so the feature is simply dead, with the only evidence on
    a console nobody is reading. M7 shipped exactly that: `StandardKey.FindNext`
    expands to **F3 and Ctrl+G** on Windows, so listing it beside an explicit
    F3 bound F3 twice on one action, and its Ctrl+G collided with Go to
    Timestamp.

    So the whole window is swept here rather than each shortcut being asserted
    where it is set: the defect is a *collision*, which no single action can
    see, and a standard key can quietly start resolving to something new on a
    platform or a Qt version nobody tested on.
    """
    from collections import defaultdict

    from PySide6.QtGui import QAction

    owners: dict[str, list[str]] = defaultdict(list)
    for act in window.findChildren(QAction):
        name = act.text() or act.objectName() or "<unnamed>"
        for seq in act.shortcuts():
            key = seq.toString()
            if key:
                owners[key].append(name)

    clashes = {key: who for key, who in owners.items() if len(who) > 1}
    assert not clashes, f"ambiguous shortcuts: {clashes}"


def test_the_m7_keys_are_bound_where_the_readme_says(window):
    """The ones a reader is told to press, spelled as Qt reports them."""
    expected = {
        "Ctrl+F": window.find_act,
        "F3": window.find_next_act,
        "Shift+F3": window.find_prev_act,
        "Ctrl+Shift+L": window.filter_act,
        "Ctrl+Shift+K": window.clear_filter_act,
        "Ctrl+G": window.goto_act,
        "Ctrl+E": window.export_act,
        "Ctrl+Shift+F": window.follow_act,
    }
    for key, act in expected.items():
        assert key in [s.toString() for s in act.shortcuts()], \
            f"{act.text()} does not answer {key}"
