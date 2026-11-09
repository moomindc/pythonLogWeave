"""
The find bar -- FR-44.

A strip below the view: a box to type in, two toggles, a hit counter, and the
two buttons that step through the hits. It owns no search state of its own --
the `Hits` come from `ui.findscan` and are pushed in with `set_hits` -- so what
is left here is a layout, a debounce timer and the keys.

## Why a bar and not a dialog

A find over a log is a thing you steer: type three characters, look at the
count, add two more, step through what is left. A modal dialog puts itself over
the rows being searched and has to be dismissed before they can be read, which
is the opposite of what FR-44's "incremental" is for.

## The debounce

Every keystroke could start a scan of every byte of every open file. At the
measured rates (SPEC 14.1g) that is seconds on a large set, so typing `timeout`
would launch seven of them and cancel six. `TYPING_PAUSE_MS` is the pause that
means "that is the word I meant" -- long enough not to fire mid-word, short
enough that it still feels like the view is answering as you type. The Enter
key bypasses it, because pressing Enter *is* saying you have finished typing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QToolButton,
    QWidget,
)

from ..session.find import Hits, Query
from . import theme

TYPING_PAUSE_MS = 180
"""Keystroke silence before a scan starts. See the module docstring."""


class FindBar(QWidget):
    """One line of chrome that drives the search and steps through its hits."""

    queryChanged = Signal(object)
    """A `Query` to run. Debounced; emitted with an empty text when cleared."""

    stepped = Signal(int)
    """+1 for next, -1 for previous."""

    closed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._hits = Hits()
        self._scanning = False
        self._current = -1

        self._box = QLineEdit()
        self._box.setPlaceholderText("Find in merged view")
        self._box.setClearButtonEnabled(True)
        self._box.textChanged.connect(self._typed)
        self._box.returnPressed.connect(self._enter)

        self._case = self._toggle("Aa", "Match case")
        self._regex = self._toggle(".*", "Regular expression")
        for button in (self._case, self._regex):
            button.toggled.connect(lambda _on: self._emit(now=True))

        self._count = QLabel("")
        self._count.setMinimumWidth(110)
        self._count.setStyleSheet(f"color: {theme.DIM};")

        self._prev = self._button("↑", "Previous match (Shift+F3)",
                                  lambda: self.stepped.emit(-1))
        self._next = self._button("↓", "Next match (F3)",
                                  lambda: self.stepped.emit(1))
        self._shut = self._button("✕", "Close the find bar (Esc)",
                                  self.closed.emit)

        self._progress = QProgressBar()
        self._progress.setMaximumWidth(90)
        self._progress.setTextVisible(False)
        self._progress.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(4)
        layout.addWidget(QLabel("Find"))
        layout.addWidget(self._box, 1)
        layout.addWidget(self._case)
        layout.addWidget(self._regex)
        layout.addWidget(self._progress)
        layout.addWidget(self._count)
        layout.addWidget(self._prev)
        layout.addWidget(self._next)
        layout.addWidget(self._shut)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TYPING_PAUSE_MS)
        self._timer.timeout.connect(lambda: self._emit(now=True))
        self._refresh_count()

    # ------------------------------------------------------------------

    def _toggle(self, text: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _button(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    # ------------------------------------------------------------------
    # what the window asks of it
    # ------------------------------------------------------------------

    def query(self) -> Query:
        return Query(text=self._box.text(),
                     regex=self._regex.isChecked(),
                     case_sensitive=self._case.isChecked())

    def focus(self, seed: str = "") -> None:
        """
        Open on the bar, selecting what is there so typing replaces it.

        `seed` pre-fills it -- what "find the word under the cursor" would hand
        in. Selecting rather than clearing is what makes Ctrl+F twice in a row
        a way to search for something else without reaching for the mouse.
        """
        if seed:
            self._box.setText(seed)
        self._box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._box.selectAll()

    def set_hits(self, hits: Hits, current_row: int = -1) -> None:
        """The result to report. Pushed in; this widget never searches."""
        self._hits = hits
        self._current = current_row
        self._refresh_count(current_row)

    def set_scanning(self, scanning: bool) -> None:
        self._scanning = bool(scanning)
        self._progress.setVisible(self._scanning)
        if self._scanning:
            self._progress.setRange(0, 0)       # indeterminate until a figure
        self._refresh_count()

    def set_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self._progress.setRange(0, 1000)
        self._progress.setValue(int(1000 * min(1.0, done / total)))

    # ------------------------------------------------------------------

    def _typed(self, _text: str) -> None:
        """
        Start (or restart) the debounce, and update the label now.

        The label cannot wait for the scan. A bad regex never produces a
        result, so a complaint that only arrived with one would never arrive at
        all -- the box would sit there looking like a search that found
        nothing. The count is also cleared here, because the number on screen
        describes the term that *was* typed and is wrong the moment it changes.
        """
        self._timer.start()
        self._refresh_count(self._current)

    def _enter(self) -> None:
        """
        Enter means "search now, and if that is what is already showing, step".

        Two jobs on one key because they are the same intent a moment apart:
        the first press ends the typing, and every press after it is "the next
        one please".
        """
        if self._timer.isActive() or self._hits.query != self.query():
            self._timer.stop()
            self._emit(now=True)
            return
        self.stepped.emit(1)

    def _emit(self, *, now: bool) -> None:
        if now:
            self._timer.stop()
        self._refresh_count(self._current)
        self.queryChanged.emit(self.query())

    def _refresh_count(self, current_row: int = -1) -> None:
        hits, query = self._hits, self.query()
        if query.empty:
            self._count.setText("")
        elif (why := query.validate()):
            self._count.setText(why[:40])
        elif hits.query != query:
            self._count.setText("searching…" if self._scanning else "")
        elif not hits.count:
            self._count.setText("no matches" + ("…" if not hits.complete else ""))
        else:
            ordinal = hits.ordinal(current_row) if current_row >= 0 else 0
            where = f"{ordinal} of " if ordinal else ""
            suffix = "" if hits.complete else "…"
            if hits.truncated:
                suffix = "+"
            hidden = f" ({hits.hidden:,} hidden)" if hits.hidden else ""
            self._count.setText(f"{where}{hits.count:,}{suffix}{hidden}")
        bad = bool(query.validate())
        self._count.setStyleSheet(
            f"color: {theme.STATUS_WARN if bad else theme.DIM};")
        can_step = bool(hits.count) and hits.query == query
        self._prev.setEnabled(can_step)
        self._next.setEnabled(can_step)

    # ------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        """
        Esc closes, Shift+Enter steps back.

        Handled here rather than as window shortcuts: while the box has focus
        these keys belong to the bar, and a window-level Esc would also fire
        with the focus in the log view where it should do nothing.
        """
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.closed.emit()
            event.accept()
            return
        if (key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.stepped.emit(-1)
            event.accept()
            return
        super().keyPressEvent(event)
