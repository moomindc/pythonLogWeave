"""
Application entry point -- `logweave [files...]`.

The only module that creates a `QApplication`, so everything else in `ui` can be
constructed inside a test that owns its own one.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from ..source import poller
from .mainwindow import MainWindow


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # Qt reads this before the platform plugin loads, so it has to be set before
    # QApplication exists rather than on the window.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(argv)
    app.setApplicationName("LogWeave")
    app.setApplicationDisplayName("LogWeave")
    app.setOrganizationName("LogWeave")

    window = MainWindow()
    window.show()

    # Files named on the command line are opened from the event loop, not before
    # it: show() only queues a paint, so indexing here would hold the window back
    # until it finished. Deferring by one tick means the frame is on screen first
    # (NFR-1) and the data arrives into it.
    paths = [Path(a) for a in argv[1:] if not a.startswith("-")]
    if paths:
        QTimer.singleShot(0, lambda: window.open_paths(paths))

    try:
        return app.exec()
    finally:
        # The follow poller is a daemon thread, so the process would exit
        # without it -- but it holds a reference to a callback that reaches
        # into a window Qt is in the middle of tearing down, and a probe
        # landing there during shutdown is a crash on the way out of an
        # otherwise clean run. Stopping it explicitly is two lines.
        poller.shutdown_default()


if __name__ == "__main__":
    raise SystemExit(main())
