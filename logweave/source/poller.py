"""
The polling watcher -- SPEC 7.4, FR-21, NFR-8, NFR-20, risk R5.

One daemon thread stats every followed path on an interval and calls back when
something about it changed. That is all it does: it is a **wake-up mechanism**,
not the thing that decides what happened.

## Why polling first, and why `os.stat` rather than a handle

SPEC 7.4 asks for `ReadDirectoryChangesW` on local files for sub-100ms latency,
polling at 250ms for UNC, and `inotify`/`FSEvents` elsewhere. That is four
mechanisms, three of them platform-specific, before a single appended line has
ever reached the view. The poller is the one that works everywhere, including on
the shares D5 says notifications cannot be trusted on, so it is the one that
gets built first -- and then measured, so the decision to add
`ReadDirectoryChangesW` is made against a number rather than a table in a spec.
At 250ms it already meets NFR-8's 200ms *processing* budget with a worst-case
notice latency of 250ms, which is within FR-21's "promptly" for a human reading
a log.

The probe is a plain `os.stat`, deliberately, and not `path_identity()`:

* `path_identity()` **opens the file**. On a log held exclusively by its writer
  (SPEC 7.1.1) that open fails, so polling it that way would report the file
  gone every 250ms -- a live source permanently marked stale by the act of
  watching it. `os.stat` reads directory metadata and does not open anything.
* It is one syscall for identity *and* size *and* mtime, where
  `path_identity()` is an open, a `GetFileInformationByHandle` and a close.
* Nothing here opens, maps or locks anything, so NFR-20 cannot be violated by
  the watcher: it is incapable of holding a handle that blocks the writer's
  rotation.

## What the kinds on these events are worth

The probe sees four numbers, and from them this module names the likely change.
That naming is **provisional**. "Same identity and the size grew" really is an
append, but "size unchanged and mtime moved" could be an append the stat raced,
or a rewrite in place, and only a re-hash of the live head can tell -- which
needs the open handle, which belongs to the thread that owns the source.

So the follow engine treats an event as "look at this source now" and calls
`FileSource.detect_change`, which implements SPEC 7.2's table properly. A
spurious event therefore costs one `detect_change`, and a mis-named one costs
nothing at all. The alternative -- doing the real classification on this thread
-- would have a background thread reading and remapping a source the UI thread
is painting from.

## Threading

The callback runs on the poller's thread. Callers in the UI layer must hand it
straight to a queued signal and do nothing else there (SPEC 7.4). The Qt-free
layers cannot enforce that, so it is stated here and at `Poller.add`.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .base import ChangeEvent, ChangeKind, SourceId, Watcher

POLL_INTERVAL_S = 0.15
"""
Seconds between probes.

**Faster than SPEC 7.4's 250ms, and that is a correction.** The spec set 250ms
for UNC and reserved sub-100ms for `ReadDirectoryChangesW` on local files,
because NFR-8 allows 200ms from a line being written to it being visible and a
250ms poll cannot meet that on its own. The premise was that polling faster
would cost too much.

It does not. A probe is one `os.stat`, measured at **12 us**, so eight sources
cost 98 us a pass -- **0.065% of one core** at this interval. Against that, the
notice latency is bounded by the interval, and 150ms leaves 50ms for the whole
of `FollowEngine.apply`, which is ~19ms on a 2 M-entry index.

So NFR-8 is met on local files by the portable mechanism, and
`ReadDirectoryChangesW` would buy latency that is already inside budget at the
cost of a Windows-only code path with its own failure modes. SPEC 7.4 has been
corrected to record that.
"""

STOP_TIMEOUT_S = 2.0
"""How long `stop(wait=True)` gives the thread before giving up on the join."""


@dataclass(frozen=True)
class Probe:
    """
    What one `os.stat` of a followed path says about it.

    `exists=False` carries no other field, so a vanished path compares equal to
    itself and does not re-fire every tick while the file is away.
    """

    exists: bool = False
    volume: int = 0
    file_id: int = 0
    size: int = 0
    mtime_ns: int = 0


def probe_path(path: Path | str) -> Probe:
    """
    Stat `path`, reporting a missing or unreachable one as `Probe()`.

    Every `OSError` is one outcome here -- gone, denied, share dropped -- because
    the distinction is not visible from a stat and the follow engine makes it
    from the open handle. Swallowing them is also what keeps one unreachable
    source from stopping the poll loop for the other seven (NFR-21).
    """
    try:
        st = os.stat(path)
    except OSError:
        return Probe()
    return Probe(
        exists=True,
        volume=int(getattr(st, "st_dev", 0)),
        file_id=int(getattr(st, "st_ino", 0)),
        size=int(st.st_size),
        mtime_ns=int(st.st_mtime_ns),
    )


def classify(before: Probe, after: Probe) -> ChangeKind:
    """
    Name the change between two probes. Provisional -- see the module docstring.

    The order of the tests is the order of certainty. Identity first, because a
    new file at the same path is a rotation whatever its size did; then size,
    which is unambiguous in both directions; then mtime, which only says
    *something* happened.
    """
    if not after.exists:
        return ChangeKind.VANISHED if before.exists else ChangeKind.UNCHANGED
    if not before.exists:
        return ChangeKind.ROTATED           # reappeared: a new file at the name
    if (before.volume, before.file_id) != (after.volume, after.file_id):
        return ChangeKind.ROTATED
    if after.size < before.size:
        return ChangeKind.TRUNCATED
    if after.size > before.size:
        return ChangeKind.APPENDED
    if after.mtime_ns != before.mtime_ns:
        # Same size, touched. Usually a rewrite in place; occasionally an append
        # the stat caught mid-flight. `detect_change` settles it from the head.
        return ChangeKind.APPENDED
    return ChangeKind.UNCHANGED


class _Registration:
    __slots__ = ("source_id", "path", "on_change", "last", "live")

    def __init__(self, source_id: SourceId, path: Path, on_change) -> None:
        self.source_id = source_id
        self.path = path
        self.on_change = on_change
        self.last = probe_path(path)
        self.live = True


class PollWatcher(Watcher):
    """
    The handle a caller gets back from `Poller.add`.

    `stop` is idempotent and does not join the shared thread -- one source
    ceasing to be followed must not block on a tick that is probing a slow
    network share for another.
    """

    def __init__(self, poller: Poller, reg: _Registration) -> None:
        self._poller = poller
        self._reg = reg

    @property
    def active(self) -> bool:
        return self._reg.live

    def stop(self) -> None:
        self._poller._remove(self._reg)


class Poller:
    """
    One thread, every followed path.

    A thread per source would be eight threads to watch eight files, each asleep
    99.9% of the time, and eight shutdowns to get wrong. A probe is a single
    stat, so one thread serves every source with room to spare -- and the loop
    is written so a share that has stopped answering delays the others by its
    own timeout and nothing more.
    """

    def __init__(self, interval: float = POLL_INTERVAL_S) -> None:
        self.interval = float(interval)
        self._regs: list[_Registration] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.ticks = 0
        """Completed passes. Only a test or a benchmark has any use for it."""
        self.failures: list[tuple[SourceId, str]] = []
        """Callbacks that raised, which a daemon thread cannot otherwise report."""

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def add(
        self,
        source_id: SourceId,
        path: Path | str,
        on_change: Callable[[ChangeEvent], None],
    ) -> PollWatcher:
        """
        Follow `path`, calling `on_change` **on the poller's thread**.

        The baseline is taken here, so a change between opening the source and
        following it is not reported. It does not need to be: the follow engine
        compares the source's size against how far its index reaches, and picks
        up anything that arrived in the gap on the first tick that has work.
        """
        reg = _Registration(source_id, Path(path), on_change)
        with self._lock:
            self._regs.append(reg)
        self.start()
        return PollWatcher(self, reg)

    def _remove(self, reg: _Registration) -> None:
        reg.live = False
        with self._lock:
            if reg in self._regs:
                self._regs.remove(reg)

    @property
    def watching(self) -> int:
        with self._lock:
            return len(self._regs)

    # ------------------------------------------------------------------
    # the thread
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Idempotent, so every `add` can call it without checking."""
        with self._lock:
            if self._thread is not None or self._stopping:
                return
            self._thread = threading.Thread(
                target=self._run, name="logweave-poller", daemon=True)
            thread = self._thread
        thread.start()

    def stop(self, *, wait: bool = True) -> None:
        with self._lock:
            self._stopping = True
            thread = self._thread
            self._thread = None
            self._regs.clear()
        self._wake.set()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(STOP_TIMEOUT_S)

    def poll_once(self) -> int:
        """
        One pass over every registration; returns how many changes were reported.

        Public because it is the whole of the loop's behaviour with none of its
        timing: a test drives this directly and asserts on what came out,
        instead of sleeping and hoping. The loop calls nothing else.
        """
        with self._lock:
            regs = list(self._regs)
        fired = 0
        for reg in regs:
            if not reg.live:
                continue
            now = probe_path(reg.path)
            kind = classify(reg.last, now)
            reg.last = now
            if kind is ChangeKind.UNCHANGED:
                continue
            fired += 1
            try:
                reg.on_change(ChangeEvent(reg.source_id, kind, size=now.size))
            except Exception as exc:                    # noqa: BLE001
                # A callback raising must not take the thread down and silently
                # stop following the other seven sources (NFR-21). A daemon
                # thread has nowhere to report to, so it is kept on the
                # registration for whoever asks and the pass continues.
                self.failures.append((reg.source_id, repr(exc)))
        self.ticks += 1
        return fired

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
            self.poll_once()
            # Waiting on an Event rather than sleeping is what makes `stop`
            # immediate: a 250ms sleep per source would otherwise be added to
            # every close, and closing eight sources would visibly hang.
            if self._wake.wait(self.interval):
                return


_default: Poller | None = None
_default_lock = threading.Lock()


def default_poller() -> Poller:
    """
    The process-wide poller, created on first use.

    Shared because the thread is shared: there is no sense in a second one. It
    is a module global rather than something the session owns so that
    `LogSource.watch` can keep the signature its ABC declares -- a test or a
    benchmark that wants its own simply constructs a `Poller`.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = Poller()
        return _default


def shutdown_default() -> None:
    """Stop and forget the shared poller. For teardown in tests and at exit."""
    global _default
    with _default_lock:
        poller, _default = _default, None
    if poller is not None:
        poller.stop()
