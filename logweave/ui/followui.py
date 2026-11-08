"""
The Qt side of following -- FR-21, FR-22, FR-25, FR-26, SPEC 7.4.

Two small widgets and one object that exists purely to cross a thread
boundary. All of the work is in `session.follow`, which knows nothing about Qt;
this module is the adapter.

## The thread hop, and why it is an object

`FollowEngine`'s watcher callback runs on the poller's thread. Touching a
`QWidget` from there is undefined behaviour, so the callback must do nothing
but wake the UI thread.

Qt's own mechanism for that is a signal with a queued connection, and a signal
needs a `QObject` to live on -- hence `FollowNotifier`. Emitting from a foreign
thread is explicitly supported, and because the receiver lives on the UI thread
Qt queues the call onto its event loop by itself.

The alternative, a `QTimer` polling `has_pending`, was rejected: at the 50ms
interval it would need to keep NFR-8's 200ms budget it burns twenty wake-ups a
second on a log nobody is writing to, and it adds its own interval to the
latency of every append.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QPushButton

from . import theme


class FollowNotifier(QObject):
    """A signal with nothing attached, used to get onto the UI thread."""

    woken = Signal()

    def wake(self) -> None:
        """Safe to call from any thread. Hand this to `FollowEngine(notify=...)`."""
        self.woken.emit()


class NewLinesButton(QPushButton):
    """
    FR-26: "N new lines below", while auto-scroll is paused.

    A button and not a label, because the only thing anyone wants to do when
    they see it is go and look -- so pressing it resumes following, which is
    also what `End` does. It hides itself when the count is zero, so it takes
    no space in the ordinary case.

    The count is *lines arrived while paused*, not lines below the viewport.
    Those are different numbers and the first is the useful one: a reader who
    has scrolled up to study a stack trace wants to know whether anything has
    happened since, not how far from the bottom they are -- which the scrollbar
    already shows.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton {{ color: {theme.FOLLOW_ACTIVE}; border: none; padding: 0 8px; }}"
            f"QPushButton:hover {{ text-decoration: underline; }}"
        )
        self._count = 0
        self.hide()

    @property
    def count(self) -> int:
        return self._count

    def set_count(self, count: int) -> None:
        self._count = max(0, int(count))
        if not self._count:
            self.hide()
            return
        plural = "" if self._count == 1 else "s"
        self.setText(f"▼  {self._count:,} new line{plural} below")
        self.setToolTip("Press to jump to the end and follow it again (End)")
        self.show()

    def clear(self) -> None:
        self.set_count(0)
