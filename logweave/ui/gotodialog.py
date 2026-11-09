"""
`Go to timestamp` -- FR-41, FR-43.

Twenty lines of dialog over `nav.parse_time`, which is where the thinking is.
The seek itself is `time_to_row`: one `searchsorted` per source, measured at
27 µs with no block replay (NFR-6), so this is instant at any file size and
there is nothing to show progress for.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..index.civil import format_wall_us
from ..session.nav import parse_time
from . import theme


class GoToTimeDialog(QDialog):
    """Asks for a time and reports it as `wall_us`."""

    def __init__(self, parent: QWidget | None = None, *, anchor_wall_us: int = 0,
                 span: tuple[int, int] = (0, 0)):
        super().__init__(parent)
        self.setWindowTitle("Go to timestamp")
        self._anchor = int(anchor_wall_us)
        self._wall: int | None = None

        self._box = QLineEdit()
        self._box.setPlaceholderText("2026-10-04 14:22:08  ·  14:22  ·  +5m")
        self._box.textChanged.connect(self._reparse)

        self._echo = QLabel("")
        self._echo.setStyleSheet(f"color: {theme.DIM};")

        hint = QLabel(
            "A time alone takes its date from the row you are on; a signed "
            "step (+5m, -30s) moves from there.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.DIM};")

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        if span != (0, 0):
            # The span is on the dialog because "what times are even in this
            # log" is the question behind half the mistyped ones.
            available = QLabel(
                f"The log runs {format_wall_us(span[0], micros=False)} to "
                f"{format_wall_us(span[1], micros=False)}")
            available.setStyleSheet(f"color: {theme.DIM};")
            layout.addWidget(available)
        layout.addWidget(self._box)
        layout.addWidget(self._echo)
        layout.addWidget(hint)
        layout.addWidget(self._buttons)
        self.setMinimumWidth(420)
        self._reparse("")

    # ------------------------------------------------------------------

    def _reparse(self, text: str) -> None:
        self._wall = parse_time(text, self._anchor)
        ok = self._wall is not None
        self._echo.setText(format_wall_us(self._wall) if ok else
                           ("" if not text.strip() else "not a time"))
        self._echo.setStyleSheet(
            f"color: {theme.DIM if ok or not text.strip() else theme.STATUS_WARN};")
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def wall_us(self) -> int | None:
        return self._wall

    def keyPressEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        """Enter accepts only a time that parses, so a typo does not seek."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._wall is not None:
                self.accept()
            event.accept()
            return
        super().keyPressEvent(event)
