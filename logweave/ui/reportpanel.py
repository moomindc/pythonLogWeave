"""
What a profile open did not manage -- FR-52.

One non-modal panel across the top of the view, listing what was missing,
unmatched or refused, with a Retry that resolves the profile again. It is a
panel and not a dialog on purpose, and the reason is in the feature itself: a
profile written for a rotating set of logs legitimately has an unmatched glob
most mornings, and the session that opened around it is perfectly usable. A
modal would make the reader dismiss a box before they could read the incident
they opened the profile for.

It is the same shape as the find and filter bars for the same reason they are:
a strip that appears when there is something to say, is closed with one click
or Escape, and takes no space when there is nothing.

**Retry re-resolves rather than re-opening.** A glob that matched nothing at
09:00 usually matches at 09:05 because the writer has rotated; what the reader
wants then is the missing file added to the session they are already reading,
not the session torn down and rebuilt.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
)

from . import theme

MAX_LINES = 6
"""
Lines shown before the rest become "and N more", with all of them in the
tooltip. A profile naming two hundred files that have all rotated away should
report that in a strip, not in a wall of text over the log.
"""


class ReportPanel(QFrame):
    """A dismissible summary of an open that did not go entirely to plan."""

    retried = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("reportPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            f"#reportPanel {{ background: {theme.CHROME}; "
            f"border-top: 1px solid {theme.GRID}; }}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Maximum)

        self.title = QLabel()
        self.title.setStyleSheet(f"color: {theme.STATUS_WARN}; font-weight: 600;")
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color: {theme.DIM};")
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        self.retry = QPushButton("&Retry")
        self.retry.setToolTip(
            "Resolve the profile's sources again and open whatever has "
            "appeared since -- without closing what is already open.")
        self.retry.clicked.connect(self.retried)

        self.dismiss = QToolButton()
        self.dismiss.setText("✕")
        self.dismiss.setAutoRaise(True)
        self.dismiss.setToolTip("Close this panel (Esc)")
        self.dismiss.clicked.connect(self._close)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.title, 1)
        head.addWidget(self.retry)
        head.addWidget(self.dismiss)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addWidget(self.detail)
        self.hide()

    # ------------------------------------------------------------------

    def report(self, title: str, lines: list[str], *,
               can_retry: bool = True) -> None:
        """
        Show `lines`, or hide the panel when there is nothing to say.

        Hiding on an empty report is what makes the caller's life simple: an
        open that went perfectly needs no special case, it just reports
        nothing.
        """
        if not lines:
            self.hide()
            return
        self.title.setText(title)
        shown = lines[:MAX_LINES]
        if len(lines) > MAX_LINES:
            shown.append(f"...and {len(lines) - MAX_LINES} more")
        self.detail.setText("\n".join(shown))
        self.detail.setToolTip("\n".join(lines))
        self.retry.setVisible(can_retry)
        self.show()

    def _close(self) -> None:
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event) -> None:          # noqa: N802  (Qt naming)
        if event.key() == Qt.Key.Key_Escape:
            self._close()
            return
        super().keyPressEvent(event)
