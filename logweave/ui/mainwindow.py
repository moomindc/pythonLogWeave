"""
The main window -- FR-1, FR-19, FR-28, FR-29.

Thin by design. The window owns a `Session`, a `MergedLogView` and a source
list; it holds no index, no offset and no merge state. Dropping files is one
call into the session and one refresh of the view, which is the whole point of
the layer-4 boundary (SPEC 1).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStatusBar,
)

from ..index import sidecar
from ..index.civil import format_wall_us
from ..session import Session
from . import theme
from .indexing import IndexWorker, start
from .mergedview import MergedLogView

SWATCH = 10

LOG_SUFFIXES = frozenset({".log", ".txt", ".out", ".err", ".gz", ".jsonl", ".csv", ""})
"""
What a *directory* drop picks up, and what the open dialog offers first.

Includes the empty suffix, because `syslog`, `messages` and `dmesg` have no
extension at all and are exactly the files someone drops a /var/log at.
"""


def expand(paths: list[Path]) -> list[Path]:
    """
    Turn whatever was handed to us into a list of files to open.

    A directory becomes the log-looking files inside it; a named file is always
    taken as named. Dragging or typing one file is a decision about that file,
    and second-guessing it would make an unusual extension impossible to open.
    A directory is a bulk action, where pulling in every README and .json
    sitting beside the logs is noise.

    One level deep only: recursing a log directory tree can mean thousands of
    files, with no way to take it back once they are indexing.
    """
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(
                c for c in p.iterdir()
                if c.is_file() and c.suffix.lower() in LOG_SUFFIXES
            ))
        else:
            out.append(p)
    return out


class MainWindow(QMainWindow):
    """One window, one session. Multiple windows are independent (FR-33)."""

    def __init__(self, session: Session | None = None, *,
                 background: bool = True):
        super().__init__()
        self.session = session or Session()
        self.background_indexing = background
        """
        Whether `open_paths` warms the cache on a worker thread (NFR-5).

        Off makes opening synchronous, which is what a test wants -- and what a
        future headless export mode would want -- without having to drive an
        event loop to find out whether a file opened.
        """

        self.setWindowTitle("LogWeave")
        self.resize(1280, 800)
        self.setStyleSheet(theme.QSS)
        self.setAcceptDrops(True)

        self.view = MergedLogView(self.session)
        self.setCentralWidget(self.view)
        self.view.currentRowChanged.connect(self._update_position)
        self.view.copied.connect(self._report_copied)
        self.view.copyRefused.connect(self._report_copy_refused)

        self._worker: IndexWorker | None = None
        self._thread = None
        self._pending: list[Path] = []
        self._queued: list[Path] = []

        self._build_sources_dock()
        self._build_menus()
        self._build_status()
        self.refresh()

    # ------------------------------------------------------------------
    # chrome
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_act = QAction("&Open Logs...", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self.open_dialog)
        file_menu.addAction(open_act)

        close_act = QAction("Close &All", self)
        close_act.triggered.connect(self.close_all)
        file_menu.addAction(close_act)

        file_menu.addSeparator()
        self.cache_act = QAction("Clear &Index Cache", self)
        self.cache_act.setStatusTip(
            "Delete the saved indexes. Files already open keep working; the "
            "next open of a large log will be slow again"
        )
        self.cache_act.triggered.connect(self.clear_cache)
        file_menu.addAction(self.cache_act)

        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        copy_act = QAction("&Copy With Attribution", self)
        copy_act.setShortcut(QKeySequence.StandardKey.Copy)
        copy_act.triggered.connect(self.view.copy_selection)
        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(copy_act)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.sources_dock.toggleViewAction())

        self.timestamp_act = QAction("&Timestamp Column", self)
        self.timestamp_act.setCheckable(True)
        self.timestamp_act.setChecked(self.view.show_timestamp)
        self.timestamp_act.setShortcut("Ctrl+T")
        self.timestamp_act.setStatusTip(
            "Hide the parsed timestamp column when every line already carries "
            "its own; the message then keeps its leading timestamp"
        )
        self.timestamp_act.toggled.connect(self.view.set_show_timestamp)
        view_menu.addAction(self.timestamp_act)

    def _build_status(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self._status_msg = QLabel("No logs open")
        self._status_pos = QLabel("")
        self._status_span = QLabel("")

        # The progress bar lives in the status bar rather than a modal: the
        # window stays usable while a drop indexes, which is the whole point of
        # moving the work off this thread.
        self._progress = QProgressBar()
        self._progress.setMaximumWidth(180)
        self._progress.setTextVisible(True)
        self._progress.hide()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setFlat(True)
        self._cancel_btn.clicked.connect(self.cancel_indexing)
        self._cancel_btn.hide()

        bar.addWidget(self._status_msg, 1)
        bar.addWidget(self._progress)
        bar.addWidget(self._cancel_btn)
        bar.addPermanentWidget(self._status_span)
        bar.addPermanentWidget(self._status_pos)

    def _build_sources_dock(self) -> None:
        self.sources_dock = QDockWidget("Sources", self)
        self.sources_dock.setObjectName("sources")
        self.sources_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.source_list = QListWidget()
        self.source_list.itemChanged.connect(self._source_toggled)
        self.sources_dock.setWidget(self.source_list)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.sources_dock)
        self.sources_dock.setMinimumWidth(220)

    # ------------------------------------------------------------------
    # opening
    # ------------------------------------------------------------------

    def open_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open log files", "",
            "Logs (" + " ".join(f"*{x}" for x in sorted(LOG_SUFFIXES) if x)
            + ");;All files (*)",
        )
        if paths:
            self.open_paths([Path(p) for p in paths])

    def open_paths(self, paths: list[Path], *,
                   background: bool | None = None) -> None:
        """
        Open files and report honestly what happened (FR-19).

        Directories are expanded here rather than at the drop, so a drop, the
        open dialog and the command line all behave identically -- `logweave
        C:/logs` should do what dragging `C:/logs` does.

        A file with no recognised timestamp format still opens -- it is shown in
        file order with its entries carrying no time -- because "we could not
        parse it" is information the user should see next to the data, not a
        reason to refuse the file.

        The indexing itself happens on a worker thread (NFR-5): what returns
        here is only the knowledge that a sidecar now exists, after which the
        files are opened on this thread as cache hits. `background=False` does
        the lot inline, which is what the tests and the headless paths want.
        """
        if background is None:
            background = self.background_indexing
        files = expand(paths)
        if not files:
            self._status_msg.setText("nothing to open")
            return
        if not background or not self.session.use_cache:
            self._finish_open(files)
            return

        if self._worker is not None:
            # Already indexing. Queue the new paths rather than starting a
            # second pool: two pools on one machine fight for the same disk and
            # the same cores, and finish later than either would alone.
            self._queued.extend(files)
            self._status_msg.setText(
                f"indexing... {len(self._queued)} more file(s) queued")
            return

        self._begin_indexing(files)

    def _begin_indexing(self, files: list[Path]) -> None:
        self._pending = files
        self._progress.setRange(0, len(files))
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m")
        self._progress.show()
        self._cancel_btn.show()
        self._status_msg.setText(f"indexing {len(files)} file(s)...")

        self._worker = IndexWorker(files, root=self.session.cache_root,
                                   use_cache=self.session.use_cache)
        self._worker.progress.connect(self._indexing_progress)
        self._worker.finished.connect(self._indexing_finished)
        self._thread = start(self._worker, self)

    def _indexing_progress(self, done: int, total: int, name: str,
                           status: str) -> None:
        self._progress.setValue(done)
        self._status_msg.setText(f"indexing {name} ({status})")

    def _indexing_finished(self, result) -> None:
        self._worker = None
        self._thread = None
        self._progress.hide()
        self._cancel_btn.hide()

        files, self._pending = self._pending, []
        if result.cancelled:
            # Whatever finished before the cancel is in the cache and costs
            # nothing to open, so the files that did get indexed are still
            # opened. Nothing is discarded for having been interrupted.
            done = {f.path for f in result.files}
            files = [p for p in files if str(p) in done]
            self._status_msg.setText("indexing cancelled")
            if not files:
                return

        self._finish_open(files, prepared=result)

        queued, self._queued = self._queued, []
        if queued:
            self._begin_indexing(queued)

    def cancel_indexing(self) -> None:
        self._queued.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._cancel_btn.setEnabled(False)

    def _finish_open(self, files: list[Path], prepared=None) -> None:
        result = self.session.add_files(files)
        self.refresh()
        self._cancel_btn.setEnabled(True)

        parts = [result.summary()]
        if result.no_format:
            parts.append("no timestamp format: " + ", ".join(
                Path(p).name for p in result.no_format))
        if result.low_confidence:
            parts.append("low confidence: " + ", ".join(
                f"{Path(p).name} {c:.0%}" for p, c in result.low_confidence))
        if result.failed:
            parts.append("failed: " + ", ".join(
                Path(p).name for p, _ in result.failed))
        if prepared is not None and prepared.indexed_bytes:
            parts.append(prepared.summary())

        # Reported in the status bar, not a modal dialog. Dropping a folder of
        # fifty logs where three are locked by another process is an ordinary
        # thing to do, and a modal that has to be dismissed before the other
        # forty-seven can be read is worse than useless. The detail goes in the
        # tooltip for whoever wants it.
        self._status_msg.setText("  |  ".join(parts))
        self._status_msg.setToolTip(
            "\n".join(f"{p}: {why}" for p, why in result.failed)
            if result.failed else ""
        )

    def clear_cache(self) -> None:
        """
        NFR-22: the saved indexes are the user's disk, so they get a way to
        reclaim it.

        Files already open are unaffected -- their arrays are mapped, and on
        Windows a mapped sidecar simply refuses to be deleted, so the count
        reported is what actually went.
        """
        root = self.session.cache_root or sidecar.cache_root()
        before, bytes_before = sidecar.usage(root)
        freed = sidecar.clear(root)
        after, _ = sidecar.usage(root)
        gone = before - after
        if not before:
            self._status_msg.setText("index cache is already empty")
            return
        kept = (f", {after} still in use" if after else "")
        self._status_msg.setText(
            f"cleared {gone} of {before} cached index(es), "
            f"{freed / 1e6:.1f} MB freed{kept}")
        self._status_msg.setToolTip(
            f"{root}\n{bytes_before / 1e6:.1f} MB before")

    def close_all(self) -> None:
        self.session.close()
        self.session = Session()
        self.view.session = self.session
        self.refresh()
        self._status_msg.setText("No logs open")

    # ------------------------------------------------------------------
    # drag and drop (FR-1)
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if u.isLocalFile()
        ]
        if paths:
            self.open_paths(paths)
            event.acceptProposedAction()

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self.view.refresh()
        self._reload_source_list()
        self._update_span()
        self._update_position(self.view.current_row())

    def _reload_source_list(self) -> None:
        self.source_list.blockSignals(True)
        self.source_list.clear()
        for i, s in enumerate(self.session.sources):
            item = QListWidgetItem(f"{s.alias}   {s.index.n:,}")
            pix = QPixmap(SWATCH, SWATCH)
            pix.fill(QColor(s.colour))
            item.setIcon(pix)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if s.meta.enabled else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, i)
            fmt = s.index.format_name or "no format"
            item.setToolTip(
                f"{s.meta.origin or s.alias}\n"
                f"{s.index.n:,} entries, {fmt}, {s.index.encoding}\n"
                f"confidence {s.index.confidence:.0%}"
            )
            self.source_list.addItem(item)
        self.source_list.blockSignals(False)

    def _source_toggled(self, item: QListWidgetItem) -> None:
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None:
            return
        self.session.set_enabled(int(idx), item.checkState() == Qt.CheckState.Checked)
        self.view.refresh()
        self._update_span()

    def _update_span(self) -> None:
        if self.session.is_empty:
            self._status_span.setText("")
            return
        lo, hi = self.session.time_span()
        self._status_span.setText(
            f"{format_wall_us(lo, micros=False)}  to  {format_wall_us(hi, micros=False)}   "
        )

    def _update_position(self, row: int) -> None:
        total = self.session.total_rows
        if not total:
            self._status_pos.setText("")
            return
        rows = self.session.rows(row, 1)
        where = ""
        if rows:
            r = rows[0]
            where = f"   {self.session.sources[r.source].alias}:{r.line_no}"
        self._status_pos.setText(f"row {row + 1:,} of {total:,}{where}   ")

    def _report_copied(self, count: int) -> None:
        self.statusBar().showMessage(
            f"Copied {count:,} row{'s' if count != 1 else ''} with attribution", 4000
        )

    def _report_copy_refused(self, count: int) -> None:
        from .mergedview import COPY_ROW_LIMIT

        self.statusBar().showMessage(
            f"{count:,} rows is too many for the clipboard "
            f"(limit {COPY_ROW_LIMIT:,}) -- narrow the selection",
            6000,
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        self.session.close()
        super().closeEvent(event)
