"""
Running a mark scan off the UI thread -- FR-31a, NFR-5.

A scan reads every byte of every source (`session.marks`), so at 40.7 MB/s for
a case-insensitive literal a 64 MB set is a second and a half and a gigabyte set
is half a minute. None of that may happen on the thread that paints.

## Why this is safe where a background re-index would not be

`ui.indexing` goes to some trouble to keep the session single-threaded, because
the view paints straight out of the session's arrays and a thread *mutating*
them mid-paint is a data race on every row. A mark scan only ever **reads**
them -- `off`, `ts` and the source's bytes -- and writes nothing back. What it
returns is a new object made of its own arrays, which the window then hands to
the strips on the GUI thread.

Two rules keep that true, and they are the only two:

* **The worker calls one Qt-free function and emits what came back.** It holds
  a session reference, so it must not touch anything that could rebuild it.
* **A scan is abandoned, never applied late.** A file opened or closed while a
  scan runs changes what the answer should be, so the result carries the
  `scan_key` it was computed for and the window drops a result whose key no
  longer matches. Without that, closing a file mid-scan paints marks from the
  file that is no longer open.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from ..highlight.rules import HighlightRule
from ..session.marks import MarkScan, marking_rules, scan, scan_key

PROGRESS_EVERY = 64
"""
Chunks between progress signals.

A queued signal per 4 MB chunk is already modest, but a session of many small
files produces one chunk each, and a thousand queued emissions is a thousand
event-loop wakeups to report work that took no time. The cheap guard is to
count them.
"""


class MarkScanWorker(QObject):
    """Runs `marks.scan` on a `QThread` and reports progress as it goes."""

    progress = Signal(int, int, str)
    """done bytes, total bytes, the rule being scanned."""

    finished = Signal(object)
    """The `MarkScan`. Emitted on cancellation too, with `cancelled` set."""

    def __init__(self, session, rules: list[HighlightRule]):
        super().__init__()
        self._session = session
        self._rules = list(rules)
        self._cancel = False
        self._ticks = 0

    def cancel(self) -> None:
        """Safe from the GUI thread: one bool, written once, read per chunk."""
        self._cancel = True

    def run(self) -> None:
        try:
            result = scan(self._session, self._rules,
                          progress=self._on_progress,
                          cancelled=lambda: self._cancel)
        except Exception as exc:                        # noqa: BLE001
            # Marks are an overlay on an overview. Whatever went wrong, the
            # window must keep working and the strips must keep drawing what
            # they drew before -- so the failure is reported as a note on an
            # empty scan rather than as an exception on a worker thread, where
            # it would be printed to stderr and lost.
            result = MarkScan(notes=(f"mark scan failed: {exc}",),
                              key=self._key())
        self.finished.emit(result)

    def _key(self) -> str:
        try:
            return scan_key(self._session, self._rules)
        except Exception:                               # noqa: BLE001
            return ""

    def _on_progress(self, done: int, total: int, rule: str) -> None:
        self._ticks += 1
        if self._ticks % PROGRESS_EVERY == 1:
            self.progress.emit(int(done), int(total), rule)


class MarkScanner(QObject):
    """
    Owns at most one scan at a time, and the thread it runs on.

    One at a time, because a second scan over the same bytes answers the same
    question more slowly: when the rules change while a scan is running, the
    running one is cancelled and the new one starts. Queueing them instead
    would make a user editing a colour in a dialog line up five full scans of
    their logs.
    """

    updated = Signal(object)
    """A `MarkScan` whose key still matches the session. Connect the strips."""

    progress = Signal(int, int, str)
    started = Signal()
    """Emitted when a scan actually begins, for the status bar."""

    def __init__(self, session, parent: QObject | None = None):
        super().__init__(parent)
        self.session = session
        self.result = MarkScan()
        self._thread: QThread | None = None
        self._worker: MarkScanWorker | None = None
        self._pending: list[HighlightRule] | None = None
        self._scanning: list[HighlightRule] = []
        """
        The rules the running scan was launched with.

        Kept here rather than on the result, so that re-deriving the key to
        check it against the session costs nothing and cannot drift from what
        was actually scanned.
        """

    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None

    def request(self, rules: list[HighlightRule]) -> bool:
        """
        Scan for these rules, if anything about the answer has changed.

        Returns whether a scan was started. The key check is what makes this
        callable from anywhere -- a dialog closing, a file opening, a follow
        tick -- without the caller having to work out whether it matters.
        """
        wanted = marking_rules(rules)
        key = scan_key(self.session, rules)
        if not wanted:
            # Nothing marked: clear rather than leave the last scan's layers
            # on the strips, which would be marks from rules that no longer
            # exist.
            self._pending = None
            self.cancel()
            # Keyed, not just "had layers": the strips can be holding marks
            # this scanner did not put there (a scan driven directly, which is
            # what a test does), and leaving them up would show marks for a
            # rule that no longer asks for any.
            if (self.result.layers or self.result.notes
                    or self.result.key != key):
                self.result = MarkScan(key=key)
                self.updated.emit(self.result)
            return False
        if key == self.result.key and not self.result.cancelled:
            return False
        if self.running:
            # Supersede: remember what is wanted and let the cancelled scan's
            # `finished` start it, so two threads never run at once.
            self._pending = list(rules)
            self.cancel()
            return False
        self._start(list(rules))
        return True

    def invalidate(self) -> None:
        """
        Forget the cached result without starting anything.

        Called when the session changes shape under a scan that has already
        finished -- a file closed, a source switched off. The marks on screen
        are now wrong, and the next `request` must not short-circuit on a key
        that no longer describes anything.
        """
        self.result = MarkScan()

    def _start(self, rules: list[HighlightRule]) -> None:
        self._scanning = list(rules)
        self._thread = QThread(self)
        self._worker = MarkScanWorker(self.session, rules)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self._done)
        self._thread.start()
        self.started.emit()

    def _done(self, result: MarkScan) -> None:
        scanned = self._scanning
        self._join()
        follow_up = self._pending
        self._pending = None
        # The key is checked against the session as it is *now*, not as it was
        # when the scan started: a file opened or closed meanwhile makes the
        # result describe a session that no longer exists, and painting its
        # marks would point at rows that have moved or gone.
        if not result.cancelled and result.key == scan_key(self.session, scanned):
            self.result = result
            self.updated.emit(result)
        if follow_up is not None:
            self._start(follow_up)

    def _join(self) -> None:
        thread, self._thread = self._thread, None
        self._worker = None
        self._scanning = []
        if thread is None:
            return
        thread.quit()
        # Bounded, like `MainWindow._join_thread`: a worker blocked in a read
        # off a dead network share must not take the window down with it, and
        # leaking one thread at shutdown is the lesser failure.
        if not thread.wait(5000):
            return
        thread.deleteLater()

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def stop(self) -> None:
        """Called from `closeEvent`: cancel and wait, once."""
        self._pending = None
        self.cancel()
        self._join()
