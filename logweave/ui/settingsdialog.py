"""
The settings window -- FR-31, FR-38, FR-28, FR-34, and most of FR-52.

Brought forward from M8, where the roadmap had the highlight-rule editor and a
preferences screen together. The pieces it drives were all built at M4 and M5
and were reachable only from menus until now: the shipped rulesets had three
checkboxes under View, a user's own rules had nowhere at all, and nothing could
say *where* a colour should appear.

## Why the View menu keeps its items

This dialog is not a replacement for them and does not try to be. A menu item
is the right control for something you flip while reading -- text size, a
column, a strip -- and a dialog is the right control for something you sit down
and set up, which is what a list of rules is. Both write to the same
`ui.settings` keys and both read back from the same window, so there is one
state and two doors to it.

## What it does and does not own

It owns a **working copy** and nothing else. Nothing is applied until Apply or
OK, and Cancel is genuinely free -- which matters more here than in most
dialogs, because marking a rule on the strips starts a scan of every open file
and a dialog that applied as you typed would launch one per keystroke.

`changed` carries the whole settled state outward; `MainWindow` is the only
thing that knows how to install it. That keeps the dialog testable without a
session: a test builds one over a plain `RulesetGroup`, drives the list, and
reads what came out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import profile as pf
from ..highlight.builtins import USER_SET
from ..highlight.rules import HighlightRule, Ruleset, RulesetGroup
from ..session import timefmt
from ..session.timefmt import TimeDisplay, TimeMode
from . import association, theme
from .mergedview import FONT_MAX, FONT_MIN
from .ruledialog import RuleDialog, colour_swatch, where_label

COLUMNS = ("Rule", "Pattern", "Match", "Colour in", "")
"""
The rule list's columns.

`Match` folds regex, case and whole-word into one readable cell rather than
three checkbox columns: they are facts about the pattern, and a reader scanning
the list wants to know *what kind of* pattern it is, not to audit three flags.
The last column is the enabled checkbox and carries no header, because the tick
is its own label.
"""


@dataclass
class Settled:
    """
    Everything the dialog decided, as plain data.

    A dataclass rather than a pile of signal arguments, so that adding a
    preference does not change a signature that `MainWindow` and three tests
    all name.
    """

    font_point: int = 0
    lane_width: int = 0
    show_timestamp: bool = True
    show_alias: bool = False
    show_line_no: bool = False
    show_minimap: bool = True
    show_timeline: bool = True
    time_display: TimeDisplay = field(default_factory=TimeDisplay)
    """
    FR-35. Carried whole rather than as a mode and a pattern, so the dialog
    cannot settle one without the other and the window has one thing to install.
    """
    reopen_last: bool = False
    """FR-54: reopen the most recent profile on start."""
    associate: bool | None = None
    """
    FR-53: whether `.lwprofile` should open this LogWeave.

    Three-valued, and `None` means "do not touch it": the association is an
    operating-system fact rather than a preference of this window, and a
    platform that has none must settle something the window can tell apart
    from "the reader unticked it".
    """
    active_sets: set[str] = field(default_factory=set)
    user_rules: list[HighlightRule] = field(default_factory=list)


class HighlightPage(QWidget):
    """
    The shipped rulesets, plus the user's own rules and what they reach.

    The list is a `QTreeWidget` with no children, which is the usual way to get
    a table whose rows carry an object: `setData` on column 0 holds the rule
    itself, so selection, reordering and editing never have to find a rule by
    searching for its name.
    """

    def __init__(self, group: RulesetGroup, parent: QWidget | None = None):
        super().__init__(parent)
        self._rules: list[HighlightRule] = list(
            group.sets.get(USER_SET, Ruleset()).rules)
        self._active = set(group.active)

        layout = QVBoxLayout(self)

        shipped = QGroupBox("Shipped rule sets")
        shipped_box = QVBoxLayout(shipped)
        self.set_boxes: dict[str, QCheckBox] = {}
        for name in sorted(group.sets):
            if name == USER_SET:
                continue
            box = QCheckBox(f"{name.capitalize()} "
                            f"({len(group.sets[name].rules)} rules)")
            box.setChecked(name in self._active)
            box.setToolTip(_SET_HELP.get(name, ""))
            shipped_box.addWidget(box)
            self.set_boxes[name] = box
        layout.addWidget(shipped)

        own = QGroupBox("Your rules")
        own_box = QVBoxLayout(own)
        self.user_box = QCheckBox("Apply my rules")
        self.user_box.setChecked(USER_SET in self._active)
        own_box.addWidget(self.user_box)

        self.list = QTreeWidget()
        self.list.setColumnCount(len(COLUMNS))
        self.list.setHeaderLabels(list(COLUMNS))
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        header = self.list.header()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self.list.itemChanged.connect(self._item_changed)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        own_box.addWidget(self.list)

        buttons = QHBoxLayout()
        self.add_btn = QPushButton("&Add…")
        self.edit_btn = QPushButton("&Edit…")
        self.remove_btn = QPushButton("&Remove")
        self.up_btn = QPushButton("Move &Up")
        self.down_btn = QPushButton("Move &Down")
        self.add_btn.clicked.connect(self.add)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.up_btn.clicked.connect(lambda: self.move_selected(-1))
        self.down_btn.clicked.connect(lambda: self.move_selected(1))
        for btn in (self.add_btn, self.edit_btn, self.remove_btn,
                    self.up_btn, self.down_btn):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        own_box.addLayout(buttons)

        self.hint = QLabel(
            "A rule set to <b>view + strips</b> is also marked on the minimap "
            "and the timeline. That needs one pass over every open file, so it "
            "runs in the background and is worth saving for the patterns you "
            "are actually hunting for.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.DIM};")
        own_box.addWidget(self.hint)
        layout.addWidget(own, 1)

        self._reload()

    # ------------------------------------------------------------------
    # the list
    # ------------------------------------------------------------------

    @property
    def rules(self) -> list[HighlightRule]:
        """The working copy, in list order. Order is FR-31's priority tiebreak."""
        return list(self._rules)

    def active_sets(self) -> set[str]:
        out = {name for name, box in self.set_boxes.items() if box.isChecked()}
        if self.user_box.isChecked():
            out.add(USER_SET)
        return out

    def _reload(self, select: int = -1) -> None:
        """
        Rebuild every row from the working copy.

        Wholesale rather than incrementally, because every operation here is a
        list edit on a list of at most a few dozen and the alternative is three
        separate paths that can disagree about what row 4 is. `blockSignals`
        guards the checkbox handler, which would otherwise fire once per row
        during construction.
        """
        self.list.blockSignals(True)
        self.list.clear()
        for rule in self._rules:
            item = QTreeWidgetItem([
                rule.name,
                rule.pattern,
                _match_label(rule),
                where_label(rule),
                "",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, rule)
            item.setForeground(0, colour_swatch(rule))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(4, Qt.CheckState.Checked if rule.enabled
                               else Qt.CheckState.Unchecked)
            if rule.on_strips:
                item.setToolTip(3, "Hits are marked on the minimap and the "
                                   "timeline as well as coloured in the view.")
            self.list.addTopLevelItem(item)
        self.list.blockSignals(False)
        for i in (0, 2, 3, 4):
            self.list.resizeColumnToContents(i)
        if 0 <= select < len(self._rules):
            self.list.setCurrentItem(self.list.topLevelItem(select))
        self._selection_changed()

    def _selection_changed(self) -> None:
        row = self.selected_row()
        has = row >= 0
        self.edit_btn.setEnabled(has)
        self.remove_btn.setEnabled(has)
        self.up_btn.setEnabled(has and row > 0)
        self.down_btn.setEnabled(has and row < len(self._rules) - 1)

    def selected_row(self) -> int:
        item = self.list.currentItem()
        return -1 if item is None else self.list.indexOfTopLevelItem(item)

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """The enabled tick, which is the one thing editable in place."""
        if column != 4:
            return
        row = self.list.indexOfTopLevelItem(item)
        if not 0 <= row < len(self._rules):
            return
        on = item.checkState(4) == Qt.CheckState.Checked
        if on != self._rules[row].enabled:
            self._rules[row] = _with(self._rules[row], enabled=on)

    # ------------------------------------------------------------------
    # editing (FR-31)
    # ------------------------------------------------------------------

    def _names(self) -> set[str]:
        return {r.name for r in self._rules}

    def add(self) -> None:
        dialog = RuleDialog(None, taken=self._names(), parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            rule = dialog.rule()
            if rule is not None:
                self.add_rule(rule)

    def add_rule(self, rule: HighlightRule) -> None:
        """Append a rule. Separate from `add` so a test needs no modal dialog."""
        self._rules.append(rule)
        self._reload(len(self._rules) - 1)

    def edit_selected(self) -> None:
        row = self.selected_row()
        if row < 0:
            return
        dialog = RuleDialog(self._rules[row], taken=self._names(), parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            rule = dialog.rule()
            if rule is not None:
                self.replace_rule(row, rule)

    def replace_rule(self, row: int, rule: HighlightRule) -> None:
        if 0 <= row < len(self._rules):
            self._rules[row] = rule
            self._reload(row)

    def remove_selected(self) -> None:
        row = self.selected_row()
        if row < 0:
            return
        del self._rules[row]
        self._reload(min(row, len(self._rules) - 1))

    def move_selected(self, delta: int) -> None:
        row = self.selected_row()
        target = row + delta
        if row < 0 or not 0 <= target < len(self._rules):
            return
        self._rules.insert(target, self._rules.pop(row))
        self._reload(target)


class SettingsDialog(QDialog):
    """
    Two tabs: how the view looks, and what gets coloured.

    `changed` fires on Apply and on OK, with everything settled. The window
    applies it; the dialog never reaches into a session, a view or a settings
    store, which is what lets the whole thing be built in a test over a bare
    `RulesetGroup`.
    """

    changed = Signal(object)
    """A `Settled`."""

    def __init__(self, group: RulesetGroup, *, current: Settled,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("LogWeave Settings")
        self.resize(760, 560)

        self.tabs = QTabWidget()
        self.appearance = _AppearancePage(current)
        self.session = _SessionPage(current)
        self.highlight = HighlightPage(group)
        self.tabs.addTab(self.appearance, "&Appearance")
        self.tabs.addTab(self.session, "&Session")
        self.tabs.addTab(self.highlight, "&Highlighting")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(
            QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.buttons)

    def settled(self) -> Settled:
        got = self.appearance.settled()
        self.session.settle_into(got)
        got.active_sets = self.highlight.active_sets()
        got.user_rules = self.highlight.rules
        return got

    def apply(self) -> None:
        self.changed.emit(self.settled())

    def _ok(self) -> None:
        self.apply()
        self.accept()


class _AppearancePage(QWidget):
    """
    The preferences the View menu already had, gathered in one place.

    Deliberately the same keys and the same ranges as `ui.settings`, read from
    the live window rather than from the store: the store is where they are
    *kept*, and a dialog that read it directly would show a stale value for
    anything changed by a menu since the last write.
    """

    def __init__(self, current: Settled, parent: QWidget | None = None):
        super().__init__(parent)
        form = QFormLayout(self)

        self.font_point = QSpinBox()
        self.font_point.setRange(FONT_MIN, FONT_MAX)
        self.font_point.setValue(current.font_point)
        self.font_point.setSuffix(" pt")
        form.addRow("&Text size", self.font_point)

        self.lane_width = QSpinBox()
        self.lane_width.setRange(theme.LANE_WIDTH_MIN, theme.LANE_WIDTH_MAX)
        self.lane_width.setValue(current.lane_width)
        self.lane_width.setSuffix(" px")
        self.lane_width.setToolTip(
            "Width of the coloured bar identifying each source (FR-28).")
        form.addRow("&Source lane width", self.lane_width)

        columns = QGroupBox("Columns")
        cols = QVBoxLayout(columns)
        self.show_timestamp = QCheckBox("Timestamp")
        self.show_timestamp.setToolTip(
            "Showing the column also trims the duplicate timestamp from the "
            "start of the message.")
        self.show_alias = QCheckBox("Source name")
        self.show_line_no = QCheckBox("Line number in its own file")
        self.show_timestamp.setChecked(current.show_timestamp)
        self.show_alias.setChecked(current.show_alias)
        self.show_line_no.setChecked(current.show_line_no)
        for box in (self.show_timestamp, self.show_alias, self.show_line_no):
            cols.addWidget(box)
        form.addRow(columns)

        self.time = _TimePage(current.time_display)
        form.addRow(self.time)

        strips = QGroupBox("Overview strips")
        box = QVBoxLayout(strips)
        self.show_minimap = QCheckBox("Minimap (severity across the rows)")
        self.show_timeline = QCheckBox("Timeline (volume across the clock)")
        self.show_minimap.setChecked(current.show_minimap)
        self.show_timeline.setChecked(current.show_timeline)
        box.addWidget(self.show_minimap)
        box.addWidget(self.show_timeline)
        form.addRow(strips)

        return_ = QLabel(
            "These are also on the View menu, which is the quicker way to "
            "change one while reading.")
        return_.setWordWrap(True)
        return_.setStyleSheet(f"color: {theme.DIM};")
        form.addRow(return_)

    def settled(self) -> Settled:
        return Settled(
            font_point=self.font_point.value(),
            lane_width=self.lane_width.value(),
            show_timestamp=self.show_timestamp.isChecked(),
            show_alias=self.show_alias.isChecked(),
            show_line_no=self.show_line_no.isChecked(),
            show_minimap=self.show_minimap.isChecked(),
            show_timeline=self.show_timeline.isChecked(),
            time_display=self.time.settled(),
        )


class _SessionPage(QWidget):
    """
    What happens to profiles outside this window -- FR-53, FR-54.

    Two settings that are neither about how the log looks nor about what gets
    coloured, which is why they are a tab and not another group box on
    Appearance: one decides what the application does *before* there is a
    window to look at, and the other reaches outside the application
    altogether.

    Like every other page here, it settles plain data and touches nothing. The
    association is written by `MainWindow`, because the dialog does not get to
    edit the registry on an Apply the reader may still cancel.
    """

    def __init__(self, current: Settled, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        start = QGroupBox("On start")
        box = QVBoxLayout(start)
        self.reopen_last = QCheckBox("Reopen the most recent profile")
        self.reopen_last.setChecked(bool(current.reopen_last))
        self.reopen_last.setToolTip(
            "Off by default: opening yesterday's logs because LogWeave was "
            "once pointed at them is a slow start nobody asked for (FR-54).")
        box.addWidget(self.reopen_last)
        hint = QLabel(
            "Files named on the command line, or a profile opened by "
            "double-click, win over this.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.DIM};")
        box.addWidget(hint)
        layout.addWidget(start)

        files = QGroupBox("Profile files")
        inner = QVBoxLayout(files)
        self.associate = QCheckBox(
            f"Open {pf.SUFFIX} files in LogWeave when double-clicked")
        self.associate.setChecked(current.associate is True)
        self.associate.setEnabled(current.associate is not None)
        self.associate.setToolTip(
            "Writes one key under HKEY_CURRENT_USER: no administrator rights, "
            "no change for other accounts, and unticking removes it (FR-53).")
        inner.addWidget(self.associate)
        self.state = QLabel(association.describe())
        self.state.setWordWrap(True)
        self.state.setStyleSheet(f"color: {theme.DIM};")
        inner.addWidget(self.state)
        layout.addWidget(files)

        layout.addStretch(1)
        self._had = current.associate

    def settle_into(self, got: Settled) -> None:
        """
        Add this page's answers to the dialog's.

        `associate` settles `None` -- "leave it alone" -- unless the reader
        actually moved the box. An Apply that rewrote the association every
        time would hit the registry on every OK, and on a platform with no
        association at all it would have nothing to write.
        """
        got.reopen_last = self.reopen_last.isChecked()
        now = self.associate.isChecked()
        got.associate = (None if self._had is None or now == self._had else now)


class _TimePage(QGroupBox):
    """
    The timestamp column's format -- FR-35.

    A mode, and for the absolute mode a pattern with presets behind a combo
    box. The custom field is always visible rather than revealed by a "custom"
    entry, because a reader who wants to adjust a preset slightly should be
    able to see what the preset *is*: choosing one fills the field, and editing
    the field is what makes it custom.

    The two relative modes grey the pattern out rather than hiding it, so the
    window does not change shape as the radio buttons move, and because the
    pattern is still what comes back if absolute is chosen again.
    """

    def __init__(self, current: TimeDisplay, parent: QWidget | None = None):
        super().__init__("Timestamp column", parent)
        layout = QVBoxLayout(self)

        self.mode = QComboBox()
        for mode, label, tip in (
            (TimeMode.ABSOLUTE, "Clock time",
             "The time each line was written."),
            (TimeMode.ELAPSED, "Elapsed from a zero point",
             "Signed offset from a row you choose with View ▸ Timestamps "
             "▸ Set zero point here. An incident reads as a timeline: the "
             "deploy is +0 and the first timeout is +1:04.882."),
            (TimeMode.DELTA, "Difference from the row above",
             "How long passed since the previous row. A stall between two "
             "lines is invisible in clock time and unmissable here."),
        ):
            self.mode.addItem(label, mode.value)
            self.mode.setItemData(self.mode.count() - 1, tip,
                                  Qt.ItemDataRole.ToolTipRole)
        self.mode.setCurrentIndex(
            max(0, self.mode.findData(current.mode.value)))
        self.mode.currentIndexChanged.connect(self._mode_changed)

        form = QFormLayout()
        form.addRow("&Shows", self.mode)

        self.preset = QComboBox()
        for pattern, label in timefmt.PRESETS:
            self.preset.addItem(f"{label}  —  {pattern}", pattern)
        self.preset.addItem("Custom", "")
        self.preset.currentIndexChanged.connect(self._preset_chosen)
        form.addRow("&Format", self.preset)

        self.pattern = QLineEdit(current.pattern)
        self.pattern.setToolTip(
            "%Y %y %m %d  %H %I %M %S  %f microseconds  %L milliseconds  "
            "%b %a %p %j  %% a literal percent")
        self.pattern.textEdited.connect(self._pattern_edited)
        form.addRow("&Pattern", self.pattern)
        layout.addLayout(form)

        self.advice = QLabel()
        self.advice.setWordWrap(True)
        layout.addWidget(self.advice)

        self._sync_preset()
        self._mode_changed()

    # ------------------------------------------------------------------

    def _preset_chosen(self) -> None:
        pattern = self.preset.currentData()
        if pattern:
            self.pattern.setText(pattern)
        self._revalidate()

    def _pattern_edited(self) -> None:
        self._sync_preset()
        self._revalidate()

    def _sync_preset(self) -> None:
        """Put the combo on the preset this pattern is, or on Custom."""
        index = self.preset.findData(self.pattern.text())
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(index if index >= 0
                                    else self.preset.count() - 1)
        self.preset.blockSignals(False)

    def _mode_changed(self) -> None:
        absolute = self.mode.currentData() == TimeMode.ABSOLUTE.value
        self.preset.setEnabled(absolute)
        self.pattern.setEnabled(absolute)
        self._revalidate()

    def _revalidate(self) -> None:
        """
        Say what is wrong, or what the result will look like.

        A preview rather than a bare "valid": the question a reader actually
        has about `%j` is not whether it parses.
        """
        if self.mode.currentData() != TimeMode.ABSOLUTE.value:
            relative = (
                "Offsets are signed and fixed width; the column is sized "
                "from the span of the open files. Other copy formats keep "
                "clock time, because an offset cites nothing a colleague "
                "can find.")
            self.advice.setStyleSheet(f"color: {theme.DIM};")
            self.advice.setText(relative)
            return
        problem = timefmt.validate_pattern(self.pattern.text())
        if problem:
            self.advice.setStyleSheet(f"color: {theme.STATUS_ERROR};")
            self.advice.setText(problem)
            return
        shown = timefmt.TimeFormatter(
            TimeDisplay(pattern=self.pattern.text())).render(_PREVIEW_US)
        self.advice.setStyleSheet(f"color: {theme.DIM};")
        self.advice.setText(f"Rows will read:  {shown}")

    def settled(self) -> TimeDisplay:
        """
        What the page decided -- falling back to the shipped pattern.

        A pattern that will not render is refused here rather than carried
        outward, because `Settled` is installed wholesale and an invalid one
        would reach the paint sixty times a second.
        """
        pattern = self.pattern.text()
        if timefmt.validate_pattern(pattern):
            pattern = timefmt.FULL
        return TimeDisplay(mode=TimeMode(self.mode.currentData()),
                           pattern=pattern)


_PREVIEW_US = 1_793_124_128_113_456
"""
A fixed sample instant for the format preview: 2026-10-27 at 18:02:08.113456.

Fixed rather than "now", so the preview does not change under the reader while
they are editing a pattern, and so a screenshot of this dialog means the same
thing tomorrow.
"""


_SET_HELP = {
    "severity": "ERROR, WARN, INFO and the rest, pointed at the eight severity "
                "classes. These also drive the minimap's colours and the "
                "severity mark in the gutter. Only WARNING and above colour "
                "the message text — colouring INFO as well would colour "
                "every row — but all eight are still classified.",
    "shapes": "What a failure looks like when no line says ERROR: exception "
              "class names, Java and Python stack frames, 'Caused by:'.",
    "syslog": "Leading <0> to <4> priorities — emergency through warning "
              "— for a raw syslog capture. Off by default because the "
              "pattern is rare elsewhere and every rule costs a branch in "
              "the scan.",
}


def _match_label(rule: HighlightRule) -> str:
    """What kind of pattern this is, in one cell."""
    parts = ["regex" if rule.is_regex else "text"]
    if rule.case_sensitive:
        parts.append("case")
    if rule.word_boundary:
        parts.append("whole word")
    if rule.scope == "match":
        parts.append("matched text only")
    return ", ".join(parts)


def _with(rule: HighlightRule, **changes) -> HighlightRule:
    from dataclasses import replace
    return replace(rule, **changes)
