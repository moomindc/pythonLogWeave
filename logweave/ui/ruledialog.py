"""
Editing one highlight rule -- FR-31.

Separate from the settings dialog that owns the list, because this is the form
that knows what a rule *is*: which fields contradict each other, which
combinations cannot contribute to the index, and what a pattern does before it
is saved. The list dialog only knows about order and selection.

## Validation happens while you type, not when you click OK

`HighlightRule.validate` already exists and already returns a sentence rather
than raising, because `Ruleset` has to survive a user mid-edit (SPEC 10.1). The
same sentence is shown live under the pattern field and OK is disabled while
there is one. A dialog that accepts a rule and then silently drops it in
`Ruleset.rejected` would be the worst of both: the user sees it in the list and
it never fires.
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..highlight.rules import HighlightRule
from ..index.severity import Severity
from . import theme

NO_COLOUR = "(none)"
"""
What an unset colour reads as.

A rule may legitimately set only a background, or only bold, so "no foreground"
has to be expressible -- and `Style.over` is built on exactly that: `None` means
inherit what was already there.
"""


class ColourButton(QPushButton):
    """
    A swatch that opens the system colour picker.

    `set_colour` is the whole API, so a test never has to open a modal picker
    to put a colour in: the dialog it opens is Qt's, it is modal, and a test
    that clicked this would hang rather than fail.
    """

    def __init__(self, label: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._label = label
        self._colour: str | None = None
        self.setMinimumWidth(130)
        self.clicked.connect(self._pick)
        self.set_colour(None)

    @property
    def colour(self) -> str | None:
        return self._colour

    def set_colour(self, value: str | None) -> None:
        self._colour = value or None
        if self._colour:
            # The swatch is the control: a hex string in a button is unreadable
            # and a colour you cannot see is one you cannot choose.
            fg = "#000000" if QColor(self._colour).lightnessF() > 0.55 else "#ffffff"
            self.setStyleSheet(
                f"background-color: {self._colour}; color: {fg};"
                f"border: 1px solid {theme.STRIP_SEPARATOR}; padding: 4px;")
            self.setText(self._colour)
        else:
            self.setStyleSheet("")
            self.setText(f"{self._label}: {NO_COLOUR}")

    def _pick(self) -> None:
        start = QColor(self._colour) if self._colour else QColor(theme.FOREGROUND)
        got = QColorDialog.getColor(start, self, f"Choose a {self._label} colour")
        if got.isValid():
            self.set_colour(got.name())


class RuleDialog(QDialog):
    """
    One rule's fields. `rule()` is what was edited, or None if it was cancelled.

    Constructed from a `HighlightRule` and never mutating it: the caller gets a
    new one back on accept. A form that edited the live object would leave a
    half-typed pattern in the ruleset when the user pressed Escape.
    """

    def __init__(self, rule: HighlightRule | None = None,
                 *, taken: set[str] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Highlight Rule" if rule else "New Highlight Rule")
        self._original = rule
        self._taken = {n for n in (taken or set())
                       if rule is None or n != rule.name}
        """
        Names already in use, minus this rule's own.

        Names are the handle `Ruleset.remove` and `replace_rule` take, so a
        duplicate makes an edit land on whichever copy comes first and look
        like it did nothing.
        """

        form = QFormLayout()

        self.name = QLineEdit(rule.name if rule else "")
        self.name.setPlaceholderText("what this rule is for, e.g. timeouts")
        form.addRow("&Name", self.name)

        self.pattern = QLineEdit(rule.pattern if rule else "")
        self.pattern.setPlaceholderText("text to look for, or a regular expression")
        form.addRow("&Pattern", self.pattern)

        self.is_regex = QCheckBox("Pattern is a regular expression")
        self.case_sensitive = QCheckBox("Match case")
        self.word_boundary = QCheckBox("Whole words only")
        self.word_boundary.setToolTip(
            "Requires a word boundary either side, so DEBUG does not match "
            "DEBUGGER. Ignored when the pattern does not begin and end with a "
            "word character.")
        for box in (self.is_regex, self.case_sensitive, self.word_boundary):
            form.addRow("", box)

        self.scope = QComboBox()
        self.scope.addItem("Colour the whole line", "line")
        self.scope.addItem("Colour only the matched text", "match")
        form.addRow("&Scope", self.scope)

        colours = QHBoxLayout()
        self.fg = ColourButton("text")
        self.bg = ColourButton("background")
        colours.addWidget(self.fg)
        colours.addWidget(self.bg)
        clear = QPushButton("Clear")
        clear.setToolTip("Unset both colours, leaving only bold or italic")
        clear.clicked.connect(self._clear_colours)
        colours.addWidget(clear)
        colours.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(colours)
        form.addRow("&Colour", wrap)

        weight = QHBoxLayout()
        self.bold = QCheckBox("Bold")
        self.italic = QCheckBox("Italic")
        weight.addWidget(self.bold)
        weight.addWidget(self.italic)
        weight.addStretch(1)
        wrap2 = QWidget()
        wrap2.setLayout(weight)
        form.addRow("", wrap2)

        self.priority = QSpinBox()
        self.priority.setRange(-999, 999)
        self.priority.setToolTip(
            "Higher wins where two rules match the same text. The shipped "
            "severity rules use 10 per class, so 80 beats all of them.")
        form.addRow("P&riority", self.priority)

        self.severity = QComboBox()
        self.severity.addItem("Do not change the severity class", None)
        for level in Severity:
            if level is not Severity.NONE:
                self.severity.addItem(level.name.title(), int(level))
        self.severity.setToolTip(
            "Counts rows matching this rule as that severity, which is what "
            "colours them on the minimap and in the source dock. Only "
            "available for a literal pattern: the index-time classifier "
            "compares fixed byte columns and cannot run a regex.")
        form.addRow("Se&verity", self.severity)

        # --- the question the user actually asked for ---------------------
        self.where_view = QRadioButton("The log view only")
        self.where_strips = QRadioButton(
            "The log view, and mark hits on the minimap and timeline")
        self.where_strips.setToolTip(
            "Marking needs one pass over every byte of every open file, "
            "because a strip covers the whole merged stream. It runs in the "
            "background; two or three marked rules are a second or two.")
        self.where_view.setChecked(True)
        where = QVBoxLayout()
        where.addWidget(self.where_view)
        where.addWidget(self.where_strips)
        wrap3 = QWidget()
        wrap3.setLayout(where)
        form.addRow("Colour &in", wrap3)

        self.enabled = QCheckBox("Rule is active")
        self.enabled.setChecked(True)
        form.addRow("", self.enabled)

        self.complaint = QLabel()
        self.complaint.setWordWrap(True)
        self.complaint.setStyleSheet(f"color: {theme.STALE};")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.complaint)
        layout.addWidget(self.buttons)

        if rule is not None:
            self._load(rule)

        # Every field the advice depends on, not just the two that can make a
        # rule invalid. The severity combo and the "colour in" radio both
        # produce a sentence of their own, and a warning that only appeared
        # after an unrelated keystroke would be a warning nobody saw.
        for widget in (self.name, self.pattern):
            widget.textChanged.connect(self._revalidate)
        for box in (self.is_regex, self.case_sensitive, self.word_boundary,
                    self.where_strips, self.where_view, self.enabled):
            box.toggled.connect(self._revalidate)
        for combo in (self.scope, self.severity):
            combo.currentIndexChanged.connect(self._revalidate)
        self._revalidate()

    # ------------------------------------------------------------------

    def _load(self, rule: HighlightRule) -> None:
        self.is_regex.setChecked(rule.is_regex)
        self.case_sensitive.setChecked(rule.case_sensitive)
        self.word_boundary.setChecked(rule.word_boundary)
        self.scope.setCurrentIndex(0 if rule.scope == "line" else 1)
        self.fg.set_colour(rule.fg)
        self.bg.set_colour(rule.bg)
        self.bold.setChecked(rule.bold)
        self.italic.setChecked(rule.italic)
        self.priority.setValue(rule.priority)
        self.enabled.setChecked(rule.enabled)
        self.where_strips.setChecked(rule.on_strips)
        self.where_view.setChecked(not rule.on_strips)
        want = None if rule.severity is None else int(rule.severity)
        self.severity.setCurrentIndex(max(0, self.severity.findData(want)))

    def _clear_colours(self) -> None:
        self.fg.set_colour(None)
        self.bg.set_colour(None)

    def _draft(self) -> HighlightRule:
        """The rule as the form currently stands, valid or not."""
        level = self.severity.currentData()
        return HighlightRule(
            name=self.name.text().strip(),
            pattern=self.pattern.text(),
            is_regex=self.is_regex.isChecked(),
            case_sensitive=self.case_sensitive.isChecked(),
            word_boundary=self.word_boundary.isChecked(),
            scope="line" if self.scope.currentIndex() == 0 else "match",
            fg=self.fg.colour,
            bg=self.bg.colour,
            bold=self.bold.isChecked(),
            italic=self.italic.isChecked(),
            priority=self.priority.value(),
            enabled=self.enabled.isChecked(),
            severity=None if level is None else Severity(level),
            on_strips=self.where_strips.isChecked(),
        )

    def _revalidate(self) -> None:
        """
        Why this rule cannot be saved yet, under the fields it is about.

        A regex severity rule is called out here rather than forbidden. It is a
        perfectly good painting rule; what it cannot do is contribute to the
        index-time classifier (SPEC 10.3), and a user who picked a class and
        got nothing on the minimap is owed that sentence.
        """
        draft = self._draft()
        notes: list[str] = []
        if not draft.name:
            notes.append("A rule needs a name.")
        elif draft.name in self._taken:
            notes.append(f"There is already a rule called {draft.name!r}.")
        why = draft.validate()
        if why:
            notes.append(why[0].upper() + why[1:] + ".")
        blocked = bool(notes)

        if draft.severity is not None and draft.is_regex:
            notes.append(
                "A regular expression cannot set the severity class, so this "
                "rule will colour text but will not reach the minimap or the "
                "source dock. Use a literal pattern for that.")
        if draft.severity is not None and not draft.is_regex:
            notes.append(
                "Changing which words map to a severity class re-indexes the "
                "open files.")
        if draft.on_strips:
            notes.append(
                "Marking on the strips scans every open file once, in the "
                "background.")

        self.complaint.setText(" ".join(notes))
        self.buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(not blocked)

    def rule(self) -> HighlightRule | None:
        """What was edited, or None if the form was never valid."""
        draft = self._draft()
        if not draft.name or draft.name in self._taken or draft.validate():
            return None
        return draft

    def accept(self) -> None:  # noqa: D102  (Qt override)
        if self.rule() is None:
            return
        super().accept()


def severity_label(rule: HighlightRule) -> str:
    return "" if rule.severity is None else Severity(rule.severity).name.title()


def scope_label(rule: HighlightRule) -> str:
    return "line" if rule.scope == "line" else "match"


def where_label(rule: HighlightRule) -> str:
    """How the rule's reach reads in the list. Kept next to the radio group."""
    return "view + strips" if rule.on_strips else "view"


def colour_swatch(rule: HighlightRule) -> QColor:
    """
    The colour shown against a rule in the list.

    Foreground first, then background, then the theme's text colour -- the same
    order `marks._colour_of` uses, so the swatch, the painted text and the strip
    mark cannot disagree about which colour a rule "is".
    """
    return QColor(rule.fg or rule.bg or theme.FOREGROUND)


