"""
Export the view to a file -- FR-47, FR-46b, SPEC 11.3.

Two pieces: a dialog that asks what shape the file should be, and a runner that
writes it a batch at a time off a zero-delay timer.

## Why the runner is a timer and not a thread

`session.export.run` explains it from the engine side: `session.rows()` mutates
the decoded-text LRU and the merge's block cache, so reading rows on a worker
thread while the view paints is a data race on every row. A find or a mark scan
is safe off-thread because it reads only the index arrays and the bytes; this
is not.

So the export stays on the UI thread and yields between batches. A zero-delay
`QTimer` runs one batch per turn of the event loop, which keeps NFR-11's
"nothing blocks for 50 ms" without a second thread and without a single lock:
a batch of 2000 rows is a few milliseconds, and the window stays live --
scrollable, cancellable, closable -- throughout.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..session.export import ExportSpec, Progress, run, suffix_preset
from ..session.rowformat import (
    DEFAULT_TEMPLATE,
    PRESET_LABELS,
    Columns,
    Preset,
    RowFormatter,
    SourceNaming,
    validate_template,
)
from . import theme

EXPORT_FILTERS = ("Text (*.txt *.log);;CSV (*.csv);;Markdown (*.md);;"
                  "All files (*)")


class ExportDialog(QDialog):
    """
    Asks where to write and in what form.

    The preset defaults from the filename's extension: a `.csv` that came out
    as aligned text is a file the user has to throw away, and the extension is
    the clearest statement of intent available.
    """

    def __init__(self, parent: QWidget | None = None, *, rows: int = 0,
                 selection: tuple[int, int] | None = None,
                 filtered: bool = False, columns: Columns | None = None,
                 suggested: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("Export view")
        self._rows = int(rows)
        self._selection = selection
        self._columns = columns or Columns()

        self._touched = False

        self._path = QLineEdit(str(suggested) if suggested else "")
        self._path.setMinimumWidth(320)
        self._path.textChanged.connect(self._path_changed)
        pick = QPushButton("Browse…")
        pick.setAutoDefault(False)
        pick.clicked.connect(self._browse)
        where = QHBoxLayout()
        where.setContentsMargins(0, 0, 0, 0)
        where.addWidget(self._path, 1)
        where.addWidget(pick)

        self._preset = QComboBox()
        for preset in Preset:
            # The data is the enum's *value*, not the member: Qt round-trips a
            # `StrEnum` through QVariant as a plain str, so storing the member
            # and comparing it back with `is` would never match.
            self._preset.addItem(PRESET_LABELS[preset], preset.value)
        self._preset.currentIndexChanged.connect(self._preset_chosen)

        self._scope = QComboBox()
        self._scope.addItem(
            f"The whole view{' (filtered)' if filtered else ''} "
            f"— {self._rows:,} rows", "view")
        if selection and selection[1] >= selection[0]:
            count = selection[1] - selection[0] + 1
            self._scope.addItem(f"The selection — {count:,} rows", "selection")
            if count > 1:
                self._scope.setCurrentIndex(1)

        self._naming = QComboBox()
        for naming, label in ((SourceNaming.BASENAME, "File name (app.log)"),
                              (SourceNaming.ALIAS, "Alias (app)"),
                              (SourceNaming.PATH, "Full path")):
            self._naming.addItem(label, naming.value)

        self._template = QLineEdit(DEFAULT_TEMPLATE)
        self._template.textChanged.connect(lambda _text: self._refresh())
        self._crlf = QCheckBox("Windows line endings (CRLF)")

        self._complaint = QLabel("")
        self._complaint.setStyleSheet(f"color: {theme.STATUS_WARN};")
        self._complaint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("File", where)
        form.addRow("Format", self._preset)
        form.addRow("Rows", self._scope)
        form.addRow("Name sources as", self._naming)
        form.addRow("Template", self._template)
        form.addRow("", self._crlf)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._complaint)
        layout.addWidget(self._buttons)
        self._refresh()

    # ------------------------------------------------------------------

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export to", self._path.text(), EXPORT_FILTERS)
        if path:
            self._path.setText(path)

    def _path_changed(self, text: str) -> None:
        """
        Follow the extension, until the user overrides the format themselves.

        Only while the preset is still the one an extension chose: changing the
        dropdown and then editing the filename must not quietly undo the
        choice.
        """
        if text and not self._touched:
            wanted = suffix_preset(text)
            index = self._preset.findData(wanted.value)
            if index >= 0:
                self._preset.blockSignals(True)
                self._preset.setCurrentIndex(index)
                self._preset.blockSignals(False)
        self._refresh()

    def _preset_chosen(self, _index: int) -> None:
        self._touched = True
        self._refresh()

    def _refresh(self) -> None:
        is_template = self.preset() is Preset.TEMPLATE
        self._template.setEnabled(is_template)
        self._naming.setEnabled(self.preset() is not Preset.PLAIN)

        why = ""
        if not self._path.text().strip():
            why = "Choose a file to write to."
        elif is_template and (bad := validate_template(self._template.text())):
            why = bad
        self._complaint.setText(why)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Save).setEnabled(not why)

    # ------------------------------------------------------------------

    def preset(self) -> Preset:
        return Preset(self._preset.currentData())

    def naming(self) -> SourceNaming:
        return SourceNaming(self._naming.currentData())

    def formatter(self, session) -> RowFormatter:
        return RowFormatter(session, preset=self.preset(),
                            naming=self.naming(), columns=self._columns,
                            template=self._template.text())

    def spec(self) -> ExportSpec:
        first, count = 0, None
        if self._scope.currentData() == "selection" and self._selection:
            first = self._selection[0]
            count = self._selection[1] - self._selection[0] + 1
        return ExportSpec(path=Path(self._path.text().strip()),
                          first_row=first, rows=count,
                          newline="\r\n" if self._crlf.isChecked() else "\n")


class ExportRun(QObject):
    """
    Drives one export, a batch per turn of the event loop.

    Owned by the window for the life of the export and dropped afterwards.
    `cancel()` abandons the generator, which closes the part-written file --
    the file itself is left on disk, because deleting what the user asked for
    is not this object's call.
    """

    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, session, formatter: RowFormatter, spec: ExportSpec,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._steps = run(session, formatter, spec)
        self._last = Progress(total=0)
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)

    def start(self) -> None:
        self._timer.start()

    def _step(self) -> None:
        try:
            self._last = next(self._steps)
        except StopIteration:
            self._timer.stop()
            self.finished.emit(self._last)
            return
        self.progress.emit(self._last)
        if self._last.done:
            self._timer.stop()
            self._close()
            self.finished.emit(self._last)

    def cancel(self) -> None:
        self._timer.stop()
        self._close()
        self._last = Progress(written=self._last.written,
                              total=self._last.total, cancelled=True, done=True)
        self.finished.emit(self._last)

    def _close(self) -> None:
        self._steps.close()
