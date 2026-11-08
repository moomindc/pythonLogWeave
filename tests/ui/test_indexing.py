"""
Opening without freezing the window -- NFR-5, SPEC 5.3.

The point of the background path is not speed, it is that the window answers
while a large drop indexes. So these tests are about the *sequence*: the
progress widgets appear, the event loop keeps turning, the files end up open,
and a cancel leaves the session consistent rather than half-built.

They drive a real event loop, which the rest of `tests/ui` deliberately avoids,
so they live apart from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.session import Session  # noqa: E402
from logweave.ui.indexing import IndexWorker  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402

TIMEOUT_MS = 30_000


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    w = MainWindow(Session(cache_root=tmp_path / "cache"))
    w.resize(1000, 700)
    yield w
    # Joined, not just cancelled: a QThread destroyed while running aborts the
    # process, and the window owns it.
    w.cancel_indexing()
    settle(w)
    w._join_thread()
    w.session.close()
    w.deleteLater()


def settle(window: MainWindow, timeout_ms: int = TIMEOUT_MS) -> bool:
    """
    Run the event loop until indexing finishes. Returns False on timeout.

    A spin on `processEvents` would be simpler and would also pass while the
    worker never ran at all, which is the one failure worth catching here.
    """
    if window._worker is None:
        QApplication.processEvents()
        return True

    loop = QEventLoop()
    window._worker.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()
    QApplication.processEvents()
    return timer.isActive()


def test_opening_in_the_background_ends_with_the_files_open(window, corpus):
    paths = sorted((corpus / "basic").glob("*.log"))
    window.open_paths(paths)

    assert settle(window), "indexing did not finish"
    assert len(window.session.sources) == len(paths)
    assert window.session.total_rows > 0
    assert window.source_list.count() == len(paths)


def test_the_window_answers_while_indexing(window, corpus):
    """
    NFR-5 in one assertion: the event loop runs during the work. If the open
    were still synchronous, nothing would be queued and this would see zero
    ticks.
    """
    ticks = 0

    def tick():
        nonlocal ticks
        ticks += 1

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(1)

    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert settle(window)
    timer.stop()
    assert ticks > 0


def test_the_progress_widgets_show_and_then_go_away(window, corpus):
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert window._progress.isVisibleTo(window)
    assert window._cancel_btn.isVisibleTo(window)

    assert settle(window)
    assert not window._progress.isVisibleTo(window)
    assert not window._cancel_btn.isVisibleTo(window)


def test_progress_counts_every_file(window, corpus):
    seen: list[tuple[int, int]] = []
    paths = sorted((corpus / "basic").glob("*.log"))
    window.open_paths(paths)
    window._worker.progress.connect(lambda d, t, n, s: seen.append((d, t)))

    assert settle(window)
    assert seen, "no progress was reported"
    assert seen[-1] == (len(paths), len(paths))


def test_a_second_drop_while_indexing_is_queued_not_raced(window, corpus):
    """
    Two pools on one machine fight for the same disk and the same cores and
    finish later than either alone -- and a second `add_files` partway through
    the first would rebuild the merge from an inconsistent set.
    """
    first = [corpus / "basic" / "app.log"]
    rest = [p for p in sorted((corpus / "basic").glob("*.log")) if p not in first]
    window.open_paths(first)
    window.open_paths(rest)

    assert window._queued
    assert settle(window)
    assert settle(window), "the queued batch did not run"
    assert len(window.session.sources) == len(first) + len(rest)


def test_cancelling_keeps_whatever_finished(window, corpus):
    paths = sorted((corpus / "basic").glob("*.log"))
    window.open_paths(paths)
    window.cancel_indexing()
    assert settle(window)

    # Cancellation is checked between files, so how many landed is a race --
    # but it must never be more than asked for, and the session must agree
    # with itself whatever the number turns out to be.
    assert len(window.session.sources) <= len(paths)
    assert window.session.total_rows >= 0
    assert window.source_list.count() == len(window.session.sources)
    assert not window._queued


def test_cancelling_clears_the_queue_too(window, corpus):
    window.open_paths([corpus / "basic" / "app.log"])
    window.open_paths(sorted((corpus / "formats").glob("*.log")))
    assert window._queued

    window.cancel_indexing()
    assert not window._queued
    assert settle(window)


def test_opening_nothing_says_so(window, tmp_path):
    window.open_paths([tmp_path / "empty-dir-that-is-a-file"])
    assert window._worker is None or settle(window)

    empty_dir = tmp_path / "no-logs-here"
    empty_dir.mkdir()
    window.open_paths([empty_dir])
    assert window._worker is None
    assert "nothing to open" in window._status_msg.text()


def test_with_the_cache_off_opening_stays_synchronous(app, corpus):
    """
    There is nothing to warm without a cache, so starting a thread would add
    latency and buy nothing.
    """
    w = MainWindow(Session(use_cache=False))
    try:
        w.open_paths([corpus / "basic" / "app.log"])
        assert w._worker is None
        assert w.session.total_rows > 0
    finally:
        w.session.close()
        w.deleteLater()


def test_the_worker_reports_a_failure_rather_than_raising(app, tmp_path):
    """
    `IndexWorker.run` is the top of a thread's stack: an exception escaping it
    is an unhandled exception in a Qt thread, which on Windows is a silent
    abort rather than a traceback.
    """
    worker = IndexWorker([Path("no-such-file.log")], root=tmp_path / "cache")
    got: list = []
    worker.finished.connect(got.append)
    worker.run()

    assert got
    assert [f.status for f in got[0].files] == ["failed"]


def test_clearing_the_cache_reports_what_went(window, corpus):
    """NFR-22: it is the user's disk, so they get a number they can act on."""
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert settle(window)

    window.clear_cache()
    text = window._status_msg.text()
    assert "cached index" in text
    assert "MB freed" in text


def test_clearing_an_empty_cache_says_so(window):
    window.clear_cache()
    assert "already empty" in window._status_msg.text()


def test_the_files_still_open_survive_clearing_the_cache(window, corpus):
    """
    Their arrays are mapped from the sidecars that are being deleted. On
    Windows the delete simply fails for those; on POSIX the file goes but the
    mapping stays valid. Either way the rows must still be readable.
    """
    window.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert settle(window)
    before = window.session.total_rows
    rows = window.session.rows(0, 5)

    window.clear_cache()
    assert window.session.total_rows == before
    assert [r.text for r in window.session.rows(0, 5)] == [r.text for r in rows]
