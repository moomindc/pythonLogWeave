"""
Indexing without freezing the window -- NFR-5, SPEC 5.3.

A first index runs at tens of MB per second, so a gigabyte-scale drop takes
long enough that a frozen window looks like a crash. The fix is not to move the
session onto a thread: the view paints straight out of the session's arrays, and
a thread mutating them mid-paint is a data race on every row.

Instead the expensive half is moved and the cheap half stays put. The worker
runs `session.prepare`, which only guarantees a sidecar exists for each path --
it shares no object with the window and returns a few hundred bytes of summary.
When it finishes, the window opens the same paths on the GUI thread, where
every one is now a cache hit at roughly 7 ms a file. The session stays
single-threaded, and nothing about the view's painting has to change.

`IndexWorker` therefore touches no Qt widget and no session. It calls one
Qt-free function and emits what came back.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..session.prepare import Prepared, PrepareResult, prepare


class IndexWorker(QObject):
    """
    Runs `prepare` on a `QThread` and reports each file as it lands.

    Cancellation is a flag read between files, not a kill: a worker process
    mid-chunk cannot be interrupted safely, and abandoning its sidecar write
    halfway is exactly the corruption the atomic rename exists to prevent.
    """

    progress = Signal(int, int, str, str)
    """done, total, file name, status"""

    finished = Signal(object)
    """The `PrepareResult`. Emitted on cancellation too, with `cancelled` set."""

    def __init__(self, paths: list[Path], *, root: Path | None = None,
                 use_cache: bool = True, workers: int | None = None):
        super().__init__()
        self._paths = list(paths)
        self._root = root
        self._use_cache = use_cache
        self._workers = workers
        self._cancel = False

    def cancel(self) -> None:
        """Safe to call from the GUI thread: one bool, written once, read often."""
        self._cancel = True

    def run(self) -> None:
        try:
            result = prepare(
                self._paths,
                root=self._root,
                use_cache=self._use_cache,
                workers=self._workers,
                progress=self._on_file,
                cancelled=lambda: self._cancel,
            )
        except Exception as exc:                        # noqa: BLE001
            # Warming is an optimisation. Whatever went wrong here, the window
            # must still go on to open the files -- where a genuine problem
            # will be reported against the file it belongs to.
            result = PrepareResult()
            result.files = [Prepared(path=str(p), status="failed",
                                     reason=str(exc)) for p in self._paths]
        self.finished.emit(result)

    def _on_file(self, done: int, total: int, got: Prepared) -> None:
        self.progress.emit(done, total, Path(got.path).name, got.status)


def start(worker: IndexWorker, owner: QObject) -> QThread:
    """
    Put `worker` on a thread and start it.

    The thread is parented to `owner` so it cannot be collected while running,
    and it deletes itself once finished -- a `QThread` garbage-collected mid-run
    takes the process down with it rather than raising.
    """
    thread = QThread(owner)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread
