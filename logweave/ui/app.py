"""
Application entry point -- `logweave [files...] [profile.lwprofile]`.

The only module that creates a `QApplication`, so everything else in `ui` can be
constructed inside a test that owns its own one.

A `.lwprofile` named here is opened as a *profile* and not as a log (FR-53):
that is what a double-click in Explorer does once the association is in place,
and it is also the shape of the thing someone types. The split is a function so
a test can ask what an argument list means without starting an application.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from .. import profile as pf
from ..source import poller
from . import settings
from .mainwindow import MainWindow


def split_arguments(args: list[str]) -> tuple[list[Path], list[Path]]:
    """
    Separate the profiles from the logs, in the order they were given.

    By extension, because that is all a double-click tells us and all a shell
    tells us. A file called `gateway.lwprofile` that is really a log is a
    stretch; a profile that has been renamed to `.txt` is a stretch too, and
    neither is worth a sniff of the first bytes before the window is up.
    """
    profiles: list[Path] = []
    logs: list[Path] = []
    for arg in args:
        if arg.startswith("-"):
            continue
        path = Path(arg)
        (profiles if path.suffix.lower() == pf.SUFFIX else logs).append(path)
    return profiles, logs


def _startup(window: MainWindow, argv: list[str]) -> None:
    """
    What to open, having been given the command line and the preferences.

    The command line always wins over "reopen the last profile" (FR-54): a
    reader who double-clicked a profile, or typed a path, has said what they
    want, and a preference that reopened something else first would make the
    window flash through a session they did not ask for.
    """
    profiles, logs = split_arguments(argv[1:])
    if not profiles and not logs and settings.reopen_last():
        recent = settings.recent_profiles()
        if recent:
            profiles = [Path(recent[0])]
    if profiles:
        window.open_profile(profiles[0])
        if len(profiles) > 1:
            # One window, one session (FR-33 gives you a second window for a
            # second session), so the rest are reported in the panel that
            # already exists for "the open did not do all of this" rather than
            # merged silently into the first.
            window.report.report(
                "One profile per window", [f"not opened: {x}"
                                           for x in profiles[1:]],
                can_retry=False)
    if logs:
        window.open_paths(logs)


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
    QTimer.singleShot(0, lambda: _startup(window, argv))

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
