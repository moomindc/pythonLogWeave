"""
Running a find or a text filter off the UI thread -- FR-44, FR-45, NFR-10.

A find reads every byte of every source. At the measured 1.27 GB/s for a
case-sensitive literal through chunked reads that is eight seconds on a 10 GB
set, and at 81 MB/s for a case-insensitive *regex* it is two minutes. None of
that may happen on the thread that paints.

This is `ui.markscan` applied to two more jobs, and it is safe for the same two
reasons: a scan only ever **reads** the session's arrays and the sources' bytes
and writes nothing back, and a result is dropped rather than applied late if
the session has changed shape under it. The one addition is **partial results**
-- NFR-10 asks for incremental output, so the worker emits snapshots as it
goes and the window shows each one.

## The one rule that is easy to get wrong

A worker must not touch `session.rows()`. It mutates the decoded-text LRU and
the merge's block cache, so calling it off-thread is a data race with the paint
path on every row -- which is exactly why `ui.indexing` keeps the session
single-threaded and why `session.export` is a generator on the UI thread
rather than a thread of its own. `find.search` and `filters.apply` read `off`,
`ts`, `flags` and the source bytes, and nothing else.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from ..session import filters as filtering
from ..session import find as finding

PROGRESS_EVERY = 16
"""
Chunks between progress signals.

Lower than `markscan.PROGRESS_EVERY` because this bar has a number on it that
someone is watching change, where a mark scan has a spinner. A queued signal
per 4 MB chunk is cheap; one per chunk of a session of a thousand small files
is a thousand event-loop wakeups to report work that took no time, which is
what the counter is for.
"""


class _Worker(QObject):
    """Runs one Qt-free job and reports what it found as it goes."""

    progress = Signal(int, int)
    partial = Signal(object)
    finished = Signal(object)

    def __init__(self, job):
        super().__init__()
        self._job = job
        self._cancel = False
        self._ticks = 0

    def cancel(self) -> None:
        """Safe from the GUI thread: one bool, written once, read per chunk."""
        self._cancel = True

    def run(self) -> None:
        try:
            result = self._job(progress=self._on_progress,
                              cancelled=lambda: self._cancel,
                              partial=self.partial.emit)
        except Exception as exc:                        # noqa: BLE001
            # A find is a convenience over a view that already works. Whatever
            # went wrong, the window must keep working -- so the exception is
            # *emitted* and turned into an empty result carrying the reason on
            # the GUI thread, rather than escaping a worker thread where it
            # would be printed to stderr and lost.
            result = exc
        self.finished.emit(result)

    def _on_progress(self, done: int, total: int, _label: str = "") -> None:
        self._ticks += 1
        if self._ticks % PROGRESS_EVERY == 1:
            self.progress.emit(int(done), int(total))


class _Runner(QObject):
    """
    Owns at most one background job at a time, and the thread it runs on.

    One at a time, because a second scan over the same bytes answers a
    superseded question more slowly: when the query changes while a scan is
    running, the running one is cancelled and the new one starts from its
    `finished`. Queueing them instead would make someone typing `timeout`
    line up seven full scans of their logs.
    """

    progress = Signal(int, int)
    partial = Signal(object)
    updated = Signal(object)
    started = Signal()

    def __init__(self, session, parent: QObject | None = None):
        super().__init__(parent)
        self.session = session
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._pending = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, job) -> None:
        """Run `job`, superseding anything already running."""
        if self.running:
            self._pending = job
            self.cancel()
            return
        self._launch(job)

    def _launch(self, job) -> None:
        self._thread = QThread(self)
        self._worker = _Worker(job)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.partial.connect(self.partial)
        self._worker.finished.connect(self._done)
        self._thread.start()
        self.started.emit()

    def _done(self, result) -> None:
        self._join()
        follow_up, self._pending = self._pending, None
        if isinstance(result, Exception):
            result = self._as_failure(result)
        if self._still_wanted(result):
            self.updated.emit(result)
        if follow_up is not None:
            self._launch(follow_up)

    def _as_failure(self, exc: Exception):
        raise NotImplementedError

    def _still_wanted(self, result) -> bool:
        raise NotImplementedError

    def _join(self) -> None:
        thread, self._thread = self._thread, None
        self._worker = None
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


class FindScanner(_Runner):
    """
    The background find (FR-44).

    Holds the last complete result, so asking the same question twice -- which
    is what re-opening the find bar does -- costs nothing.
    """

    def __init__(self, session, parent: QObject | None = None):
        super().__init__(session, parent)
        self.result = finding.Hits()
        self._query = finding.Query()

    def request(self, query: finding.Query) -> bool:
        """
        Search for `query`, unless the answer is already in hand.

        Returns whether a scan was started. The key check makes this callable
        from anywhere -- a keystroke, a file opening, a follow tick -- without
        the caller having to work out whether it matters.
        """
        key = finding.search_key(self.session, query)
        if query.empty:
            self._query = query
            self.cancel()
            if self.result.count or self.result.key:
                self.result = finding.Hits(query=query, complete=True)
                self.updated.emit(self.result)
            return False
        if (key == self.result.key and self.result.complete
                and self.result.query == query):
            # Same question, same files: re-derive the rows in case the merge
            # moved under us (a filter, a source hidden) and re-emit.
            self.result.locate(self.session.merge)
            self.updated.emit(self.result)
            return False
        self._query = query
        self.start(lambda **kw: finding.search(self.session, query, **kw))
        return True

    def relocate(self) -> None:
        """
        Re-derive the hit rows against the merge as it now is.

        Called when the view's row numbering changes without the hits changing:
        a filter applied or cleared, a source shown or hidden, a followed file
        appending in the middle.
        """
        if self.result.found:
            self.result.locate(self.session.merge)
            self.updated.emit(self.result)

    def invalidate(self) -> None:
        """Forget the cached result without starting anything."""
        self.result = finding.Hits()

    def _as_failure(self, exc: Exception) -> finding.Hits:
        return finding.Hits(query=self._query, complete=True,
                            notes=(f"the search failed: {exc}",),
                            key=finding.search_key(self.session, self._query))

    def _still_wanted(self, result: finding.Hits) -> bool:
        if result.cancelled:
            return False
        # Checked against the session as it is *now*, not as it was when the
        # scan started: a file opened or closed meanwhile makes the result
        # describe a session that no longer exists, and its rows would point
        # at lines that have moved or gone.
        if result.key != finding.search_key(self.session, result.query):
            return False
        self.result = result
        return True


class FilterScanner(_Runner):
    """
    The background text-filter pass -- tier 2 of FR-45.

    Tier 1 is not here: severity, source and time-window masks are a few
    vectorised passes and belong on the UI thread, where the result can be
    applied in the same turn as the click that asked for it (NFR-9). Only a
    filter that has to read bytes comes through this.
    """

    def __init__(self, session, parent: QObject | None = None):
        super().__init__(session, parent)
        self._filter = filtering.Filter()

    def request(self, filt: filtering.Filter) -> None:
        self._filter = filt
        self.start(lambda **kw: filtering.apply(self.session, filt, **kw))

    def _as_failure(self, exc: Exception) -> filtering.Filtered:
        sources = list(self.session.sources)
        return filtering.Filtered(
            filt=filtering.Filter(), masks=[None] * len(sources),
            hists=[ss.index.hist for ss in sources],
            kept=sum(int(ss.index.n) for ss in sources),
            total=sum(int(ss.index.n) for ss in sources),
            notes=(f"the filter failed: {exc}",),
            key=filtering.filter_key(self.session, filtering.Filter()))

    def _still_wanted(self, result: filtering.Filtered) -> bool:
        if result.cancelled:
            return False
        return result.key == filtering.filter_key(self.session, result.filt)
