"""
The filter bar -- FR-45, FR-48.

One line of chrome that builds a `filters.Filter` and says how much of the log
it is letting through. Like the find bar it holds no engine state: it emits a
`Filter`, the window evaluates it (tier 1 inline, tier 2 on a thread) and hands
back a `Filtered` to report.

## Why the count is always on screen

A filtered view is the one state in which this application can mislead someone
badly: every row on screen is true, and the absence of the row they are looking
for is not. So the bar is only ever visible while a filter is in force or being
built, it says `12,043 of 4,112,900 rows` whenever it is, and the status bar
repeats it in `theme.FILTER_ACTIVE` for anyone who has scrolled past the bar.

## Why severity and source are menus rather than a row of checkboxes

Eight severities and up to fifty sources (NFR-14) do not fit on a line, and the
answer to "which are on" is usually "all of them" or "one of them". A menu
button shows the summary -- `Severity: ERROR+` -- and opens the detail only
when the detail is wanted. The common asks have their own items at the top,
because "at least WARNING" is what people mean nine times out of ten.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QToolButton,
    QWidget,
)

from ..index.civil import format_wall_us
from ..index.severity import Severity
from ..session.filters import ALL_SEVERITIES, Filter, Filtered
from ..session.nav import AT_LEAST
from . import theme

TYPING_PAUSE_MS = 250
"""
Keystroke silence before a text filter is evaluated.

Longer than the find bar's: a tier-2 filter rebuilds the merged view and the
strips as well as reading the bytes, so a half-typed word costs more here than
there.
"""


class FilterBar(QWidget):
    """Builds a `Filter` and reports what it kept."""

    filterChanged = Signal(object)
    closed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._severities: set[int] = set()
        self._sources: set[int] = set()
        self._since: int | None = None
        self._until: int | None = None
        self._names: list[str] = []
        self._scanning = False

        self._severity_button = QToolButton()
        self._severity_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._severity_menu = QMenu(self._severity_button)
        self._severity_button.setMenu(self._severity_menu)
        self._severity_acts: dict[int, QAction] = {}
        self._build_severity_menu()

        self._source_button = QToolButton()
        self._source_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._source_menu = QMenu(self._source_button)
        self._source_button.setMenu(self._source_menu)
        self._source_acts: list[QAction] = []

        self._include = QLineEdit()
        self._include.setPlaceholderText("contains")
        self._include.setMaximumWidth(190)
        self._exclude = QLineEdit()
        self._exclude.setPlaceholderText("does not contain")
        self._exclude.setMaximumWidth(190)
        for box in (self._include, self._exclude):
            box.textChanged.connect(lambda _t: self._timer.start())
            box.returnPressed.connect(self._emit_now)

        self._case = self._toggle("Aa", "Match case")
        self._regex = self._toggle(".*", "Regular expression")
        for button in (self._case, self._regex):
            button.toggled.connect(lambda _on: self._emit_now())

        self._window = QPushButton("")
        self._window.setToolTip(
            "The time window. Drag on the timeline strip to set one (FR-48); "
            "click here to clear it")
        self._window.clicked.connect(self.clear_window)
        self._window.hide()

        self._count = QLabel("")
        self._count.setMinimumWidth(170)

        self._progress = QProgressBar()
        self._progress.setMaximumWidth(90)
        self._progress.setTextVisible(False)
        self._progress.hide()

        self._reset = QPushButton("Clear")
        self._reset.setToolTip("Drop every filter and show the whole log")
        self._reset.clicked.connect(self.clear)
        shut = QToolButton()
        shut.setText("✕")
        shut.setToolTip("Close the filter bar and show the whole log (Esc)")
        shut.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        shut.clicked.connect(self.closed.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(4)
        layout.addWidget(QLabel("Filter"))
        layout.addWidget(self._severity_button)
        layout.addWidget(self._source_button)
        layout.addWidget(self._include)
        layout.addWidget(self._exclude)
        layout.addWidget(self._case)
        layout.addWidget(self._regex)
        layout.addWidget(self._window)
        layout.addStretch(1)
        layout.addWidget(self._progress)
        layout.addWidget(self._count)
        layout.addWidget(self._reset)
        layout.addWidget(shut)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TYPING_PAUSE_MS)
        self._timer.timeout.connect(self._emit_now)
        self._refresh_labels()

    # ------------------------------------------------------------------
    # the menus
    # ------------------------------------------------------------------

    def _toggle(self, text: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _build_severity_menu(self) -> None:
        menu = self._severity_menu
        for level in (Severity.WARNING, Severity.ERROR):
            act = QAction(f"At least {level.name}", self)
            act.triggered.connect(
                lambda _c=False, level=level: self.set_severities(
                    AT_LEAST[int(level)]))
            menu.addAction(act)
        every = QAction("Any severity", self)
        every.triggered.connect(lambda: self.set_severities(frozenset()))
        menu.addAction(every)
        menu.addSeparator()
        # Descending, so the classes that matter are at the top of the menu --
        # the same argument SPEC 8.4's bands lost and had to be corrected on.
        for level in sorted(Severity, reverse=True):
            act = QAction(level.name, self)
            act.setCheckable(True)
            act.toggled.connect(
                lambda on, level=level: self._severity_toggled(int(level), on))
            menu.addAction(act)
            self._severity_acts[int(level)] = act

    def set_sources(self, names: list[str]) -> None:
        """
        Rebuild the source menu. Called whenever the session's file list changes.

        Positions that no longer exist are dropped from the selection rather
        than kept: a filter naming source 3 of a session that now has two would
        otherwise silently hide one of them.
        """
        self._names = list(names)
        self._sources = {i for i in self._sources if i < len(self._names)}
        self._source_menu.clear()
        self._source_acts = []
        every = QAction("Any source", self)
        every.triggered.connect(lambda: self.set_source_set(frozenset()))
        self._source_menu.addAction(every)
        self._source_menu.addSeparator()
        for i, name in enumerate(self._names):
            act = QAction(name, self)
            act.setCheckable(True)
            act.setChecked(i in self._sources)
            act.toggled.connect(
                lambda on, i=i: self._source_toggled(i, on))
            self._source_menu.addAction(act)
            self._source_acts.append(act)
        self._source_button.setEnabled(bool(self._names))
        self._refresh_labels()

    # ------------------------------------------------------------------
    # state in
    # ------------------------------------------------------------------

    def filter(self) -> Filter:
        return Filter(
            severities=frozenset(self._severities),
            sources=frozenset(self._sources),
            since=self._since,
            until=self._until,
            include=self._include.text(),
            exclude=self._exclude.text(),
            regex=self._regex.isChecked(),
            case_sensitive=self._case.isChecked(),
        )

    def set_filter(self, filt: Filter) -> None:
        """
        Show a whole filter at once -- a profile's saved view (FR-45, FR-49).

        One emission, not six. Setting the pieces individually would signal
        after each, and each of those signals starts a scan of every open file:
        a saved filter with a severity, two sources and both text boxes would
        kick off five scans that are thrown away before the sixth is the one
        the reader asked for.
        """
        self._severities = ({int(x) for x in filt.severities}
                            if set(filt.severities) != ALL_SEVERITIES else set())
        self._sources = {int(x) for x in filt.sources}
        self._since, self._until = filt.since, filt.until
        for level, act in self._severity_acts.items():
            with _blocked(act):
                act.setChecked(level in self._severities)
        for i, act in enumerate(self._source_acts):
            with _blocked(act):
                act.setChecked(i in self._sources)
        for box, text in ((self._include, filt.include),
                          (self._exclude, filt.exclude)):
            with _blocked(box):
                box.setText(text)
        for button, on in ((self._case, filt.case_sensitive),
                           (self._regex, filt.regex)):
            with _blocked(button):
                button.setChecked(bool(on))
        self._emit_now()

    def set_severities(self, severities) -> None:
        wanted = {int(s) for s in severities}
        if wanted == ALL_SEVERITIES:
            wanted = set()
        self._severities = wanted
        for level, act in self._severity_acts.items():
            with _blocked(act):
                act.setChecked(level in wanted)
        self._emit_now()

    def set_source_set(self, sources) -> None:
        self._sources = {int(s) for s in sources}
        for i, act in enumerate(self._source_acts):
            with _blocked(act):
                act.setChecked(i in self._sources)
        self._emit_now()

    def set_window(self, since: int | None, until: int | None) -> None:
        """FR-48: a range dragged on the timeline strip lands here."""
        self._since, self._until = since, until
        self._emit_now()

    def clear_window(self) -> None:
        self.set_window(None, None)

    def clear(self) -> None:
        self._severities.clear()
        self._sources.clear()
        self._since = self._until = None
        for act in list(self._severity_acts.values()) + self._source_acts:
            with _blocked(act):
                act.setChecked(False)
        for box in (self._include, self._exclude):
            with _blocked(box):
                box.clear()
        self._emit_now()

    def set_result(self, result: Filtered | None) -> None:
        """What the evaluated filter kept. Pushed in; this widget never scans."""
        if result is None or not result.filt.active:
            self._count.setText("")
            self._count.setStyleSheet(f"color: {theme.DIM};")
            return
        text = result.describe()
        if result.notes:
            text += "  " + result.notes[0]
        self._count.setText(text)
        self._count.setStyleSheet(
            f"color: {theme.STATUS_WARN if result.notes else theme.FILTER_ACTIVE};")

    def set_scanning(self, scanning: bool) -> None:
        self._scanning = bool(scanning)
        self._progress.setVisible(self._scanning)
        if self._scanning:
            self._progress.setRange(0, 0)

    def set_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self._progress.setRange(0, 1000)
        self._progress.setValue(int(1000 * min(1.0, done / total)))

    def focus(self) -> None:
        self._include.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._include.selectAll()

    # ------------------------------------------------------------------

    def _severity_toggled(self, level: int, on: bool) -> None:
        if on:
            self._severities.add(level)
        else:
            self._severities.discard(level)
        self._emit_now()

    def _source_toggled(self, pos: int, on: bool) -> None:
        if on:
            self._sources.add(pos)
        else:
            self._sources.discard(pos)
        self._emit_now()

    def _emit_now(self) -> None:
        self._timer.stop()
        self._refresh_labels()
        self.filterChanged.emit(self.filter())

    def _refresh_labels(self) -> None:
        if not self._severities:
            label = "Severity: any"
        elif self._severities == set(AT_LEAST[int(Severity.WARNING)]):
            label = "Severity: WARNING+"
        elif self._severities == set(AT_LEAST[int(Severity.ERROR)]):
            label = "Severity: ERROR+"
        elif len(self._severities) == 1:
            label = f"Severity: {Severity(next(iter(self._severities))).name}"
        else:
            label = f"Severity: {len(self._severities)} classes"
        self._severity_button.setText(label)

        if not self._sources:
            self._source_button.setText("Sources: all")
        elif len(self._sources) == 1 and self._names:
            pos = next(iter(self._sources))
            name = self._names[pos] if pos < len(self._names) else "?"
            self._source_button.setText(f"Sources: {name}")
        else:
            self._source_button.setText(f"Sources: {len(self._sources)}")

        if self._since is None and self._until is None:
            self._window.hide()
        else:
            self._window.show()
            lo = format_wall_us(self._since, micros=False) if self._since else "start"
            hi = format_wall_us(self._until, micros=False) if self._until else "end"
            # Times only when the window is inside one day, which it nearly
            # always is: `2026-10-04 14:22:08 -> 2026-10-04 14:48:11` is twice
            # the width to say the same thing.
            if lo[:10] == hi[:10]:
                hi = hi[11:]
            self._window.setText(f"⏱ {lo} → {hi}  ✕")

    def keyPressEvent(self, event) -> None:  # noqa: N802  (Qt naming)
        if event.key() == Qt.Key.Key_Escape:
            self.closed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _blocked:
    """
    Set a widget's state without its signal calling back into us.

    A context manager rather than `blockSignals` inline, because every one of
    these updates is a loop waiting to happen: `set_severities` ticks an action,
    the action emits `toggled`, the slot edits the set being iterated.
    """

    def __init__(self, widget):
        self._widget = widget

    def __enter__(self):
        self._was = self._widget.signalsBlocked()
        self._widget.blockSignals(True)
        return self._widget

    def __exit__(self, *_exc) -> None:
        self._widget.blockSignals(self._was)
