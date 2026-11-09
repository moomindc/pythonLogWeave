"""
The follow engine -- SPEC 7, FR-21 to FR-26, NFR-8, NFR-20, risk R5.

Qt-free, like the rest of this layer. It owns no timer and emits no signals: the
UI drives it by calling `apply` when `has_pending` says there is something to
do, and renders whatever the returned `FollowTick` describes.

## The split that makes this safe

Watching happens on a background thread (`source.poller`). Everything else
happens on the thread that owns the session.

That line is not a style preference. A followed source is read by the paint path
and re-indexed by the append path, and `FileSource` keeps mutable state for both
-- the known extent, the mapping, the encoding. A watcher that re-indexed from
its own thread would be mutating that under a paint. So the watcher's callback
does the least it possibly can: record which source moved, and set a flag. All
of the work is in `apply`, which the owner thread calls.

The consequence is that `apply` is also the only place that needs to be correct
about ordering, and it is one function.

## What a tick costs, and where

Measured on a 2 M-entry index, per source with new lines:

    poll (one stat)                    ~15 us, on the poller thread
    detect_change (identity + head)    ~40 us
    index the appended bytes           ~1 ms per 10 k lines
    splice the index                   ~13 ms          <-- O(entries)
    Histogram.extend                   ~50 us
    MergeIndex.extend_checkpoints      ~0.8 ms         (321x a rebuild)
    Overview re-aggregate + repaint     ~4 ms

So ~19 ms end to end against NFR-8's 200 ms, and the term that scales with how
much log is open is the splice -- see `index.appending` for why that is left
alone for now. A source with nothing new costs the poll and nothing else,
because the watcher does not fire.

## Rotation, truncation, and one honest limit

SPEC 7.2's table is implemented in `FileSource.detect_change`; this module acts
on its verdict.

* **Rotated** -- the old file still exists under its new name, and the open
  handle still serves it, so the source is frozen exactly as it is (stale, no
  longer followed, fully readable) and the path's new occupant is opened as a
  separate source. PRD Q7 keeps them distinct in v1. No history is lost.
* **Vanished** -- the same, minus a successor: read the open handle to EOF,
  then mark it stale (FR-25).
* **Unreachable** -- mark stale, keep following; a share that comes back
  resumes without the user doing anything.
* **Truncated in place** -- the old bytes are *gone from the disk*. The source
  is re-indexed from zero into a new generation and the discontinuity is
  recorded, which is the most that can be done: nothing was lost by us, it was
  deleted by the writer.

  FR-23 asks for rotation and truncation to be handled "without losing already-read
  history", and for truncation that is only achievable by keeping the old
  content somewhere of our own. There is one accidental way to get it -- a live
  `mmap` keeps the truncated pages readable -- and taking it would mean holding
  the mapping that NFR-20 forbids, because Windows then refuses the writer's own
  truncating reopen (see `FileSource.unmap`, which measures this). Breaking the
  writer to keep a scrollback is the wrong trade, so it is not made. Spooling
  followed logs to our own store would be a different product.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..index.appending import append_region, trim_to_line
from ..index.indexer import build_index
from ..source.base import ChangeEvent, ChangeKind, SourceId, SourceIdentity, Watcher
from ..source.file import FileSource
from ..source.poller import Poller, default_poller


@dataclass
class FollowState:
    """Per-source follow state. One of these per `SessionSource`, by `SourceId`."""

    source_id: SourceId
    following: bool = False
    watcher: Watcher | None = None
    identity: SourceIdentity | None = None
    """Baseline for `detect_change`, refreshed every time it is consulted."""
    stale: bool = False
    stale_reason: str = ""
    generation: int = 0
    """Bumped on a truncation. A row cached against an older one is not reusable."""
    added: int = 0
    """Entries this source has gained since following started."""
    pending_bytes: int = 0
    """Bytes of a part-written line held back (SPEC 7.3)."""
    truncations: int = 0
    note: str = ""

    @property
    def live(self) -> bool:
        return self.following and not self.stale


@dataclass
class FollowTick:
    """What one `apply` did. Everything the UI needs to redraw, and nothing else."""

    changed: list[int] = field(default_factory=list)
    """Source positions that gained entries."""
    added_rows: int = 0
    first_changed_row: int = 0
    """Lowest merged row whose content may have moved. `total` when none did."""
    truncated: list[int] = field(default_factory=list)
    rotated: list[int] = field(default_factory=list)
    """Sources frozen by a rotation. Their successors are at the end of the list."""
    successors: list[str] = field(default_factory=list)
    went_stale: list[int] = field(default_factory=list)
    rebuilt: bool = False
    """True when the merge was rebuilt rather than extended."""
    elapsed_ms: float = 0.0

    @property
    def anything(self) -> bool:
        return bool(self.changed or self.truncated or self.rotated or self.went_stale)


class FollowEngine:
    """
    Keeps a session's indexes, merge and strips current while files are written to.

    Construct with a session, `set_global_follow(True)`, then call `apply` from
    the owner thread whenever `has_pending`.
    """

    def __init__(self, session, *, poller: Poller | None = None,
                 notify: Callable[[], None] | None = None) -> None:
        self.session = session
        self.poller = poller or default_poller()
        self.notify = notify
        """
        Called, **on the poller's thread**, when there is something to apply.

        The UI passes a Qt signal's `emit` here, which hops to the UI thread
        and calls `apply` from a slot. A callback rather than the engine owning
        a timer: polling `has_pending` on a 50ms timer would add up to 50ms to
        every append's latency and burn a wake-up ten times a second on a log
        nobody is writing to, and this layer must not import Qt in any case.
        """
        self.states: dict[str, FollowState] = {}
        self.global_follow = False
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self.ticks = 0

    # ------------------------------------------------------------------
    # FR-24: what is being followed
    # ------------------------------------------------------------------

    def set_global_follow(self, on: bool) -> None:
        """
        The master switch. Per-source flags are remembered, not overwritten.

        Turning follow off and on again must leave the per-file choices the user
        made intact -- otherwise the global toggle is a destructive operation
        disguised as a convenience.
        """
        self.global_follow = bool(on)
        self.sync()

    def set_follow(self, source: int, on: bool) -> None:
        """FR-24: follow this one file or not. Stored on the source's own meta."""
        self.session.sources[source].meta.follow = bool(on)
        self.sync()

    def sync(self) -> None:
        """
        Reconcile watchers with the current sources and flags.

        Idempotent, and the only place a watcher is created or stopped. Called
        after anything that could change the answer -- a toggle, a file being
        added, a rotation -- so there is no path where a source is followed
        twice or silently not at all.
        """
        wanted: set[str] = set()
        for ss in self.session.sources:
            sid = str(ss.source.id)
            state = self.states.setdefault(sid, FollowState(source_id=ss.source.id))
            if state.stale or not self.global_follow or not ss.meta.follow:
                self._unwatch(state)
                continue
            wanted.add(sid)
            if state.watcher is not None:
                continue
            self._watch(ss, state)

        for sid, state in self.states.items():
            if sid not in wanted:
                self._unwatch(state)

    def _watch(self, ss, state: FollowState) -> None:
        src = ss.source
        if isinstance(src, FileSource):
            # One poller for the whole session, and the one a test can step.
            src.use_poller(self.poller)
        watcher = src.watch(self._on_change)
        if watcher is None:
            state.following = False
            state.note = "this source cannot be followed"
            return
        # The index was built against whatever the file contained at that
        # instant, which for a live log is usually a half-written last line.
        # Giving those bytes back now is what stops the next append splitting
        # that line into two entries (SPEC 7.3).
        state.pending_bytes = trim_to_line(src, ss.index)
        state.identity = src.identity() if isinstance(src, FileSource) else None
        state.watcher = watcher
        state.following = True
        state.note = ""

    def _unwatch(self, state: FollowState) -> None:
        if state.watcher is not None:
            state.watcher.stop()
            state.watcher = None
        state.following = False

    def stop(self) -> None:
        """Stop watching everything. Leaves the indexes and the merge as they are."""
        self.global_follow = False
        for state in self.states.values():
            self._unwatch(state)
        with self._lock:
            self._dirty.clear()

    # ------------------------------------------------------------------
    # the watcher's thread -- nothing happens here
    # ------------------------------------------------------------------

    def _on_change(self, event: ChangeEvent) -> None:
        """
        Called on the poller's thread. Records and returns.

        A set, so a source that changes five times before the next `apply` is
        one unit of work. The event's `kind` is deliberately discarded: it is
        the poller's guess from four numbers, and `apply` asks
        `FileSource.detect_change` on the owner thread, which can look at the
        file properly.
        """
        with self._lock:
            self._dirty.add(str(event.source_id))
        if self.notify is not None:
            self.notify()

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    # ------------------------------------------------------------------
    # the owner thread -- everything happens here
    # ------------------------------------------------------------------

    def apply(self) -> FollowTick:
        """
        Fold every pending change into the session. Owner thread only.

        The order matters and is: per-source index work first, then one merge
        update for the batch, then the strips. Updating the merge inside the
        loop would rebuild checkpoints once per source for no benefit, and
        eight files being written to at once is the normal case, not the
        exceptional one.
        """
        started = time.perf_counter()
        tick = FollowTick()
        with self._lock:
            dirty, self._dirty = self._dirty, set()
        if not dirty:
            tick.first_changed_row = self.session.total_rows
            return tick

        for sid in sorted(dirty):
            pos = self._position(sid)
            if pos is None:
                continue
            self._apply_one(pos, self.states[sid], tick)

        self._settle(tick)
        self.ticks += 1
        tick.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return tick

    def _position(self, sid: str) -> int | None:
        for i, ss in enumerate(self.session.sources):
            if str(ss.source.id) == sid:
                return i
        return None

    def _apply_one(self, pos: int, state: FollowState, tick: FollowTick) -> None:
        ss = self.session.sources[pos]
        src = ss.source
        kind = (src.detect_change(state.identity)
                if isinstance(src, FileSource) and state.identity is not None
                else ChangeKind.APPENDED)

        if kind in (ChangeKind.APPENDED, ChangeKind.UNCHANGED, ChangeKind.VANISHED):
            # VANISHED included on purpose: SPEC 7.2 says to read the open
            # handle to EOF before marking it stale, so the last lines written
            # before the file was deleted are not thrown away.
            self._append(pos, state, tick)
        elif kind is ChangeKind.TRUNCATED:
            self._truncate(pos, state, tick)
        elif kind is ChangeKind.ROTATED:
            self._rotate(pos, state, tick)

        if kind is ChangeKind.VANISHED:
            self._mark_stale(pos, state, tick, "the file is gone")
        elif kind is ChangeKind.UNREACHABLE:
            self._mark_stale(pos, state, tick, "the source is unreachable")
        elif isinstance(src, FileSource):
            state.identity = src.identity()

    def _append(self, pos: int, state: FollowState, tick: FollowTick) -> None:
        ss = self.session.sources[pos]
        result = append_region(ss.source, ss.index,
                               mtime_wall_us=_mtime_wall_us(ss))
        state.pending_bytes = result.pending_bytes
        if result.note:
            state.note = result.note
        if result.needs_rebuild:
            # The tail cannot be indexed on its own -- see
            # `AppendResult.needs_rebuild`. Swallowing this would drop the
            # appended lines permanently and look exactly like a file that had
            # stopped being written to, so it re-reads instead.
            self._reindex(pos, state, tick)
            return
        if not result.changed:
            return
        self.session.replace_index(pos, result.index)
        state.added += result.added
        tick.changed.append(pos)
        tick.added_rows += result.added

    def _reindex(self, pos: int, state: FollowState, tick: FollowTick) -> int:
        """Re-index a source from zero. Returns the entries it now has."""
        ss = self.session.sources[pos]
        before = ss.index.n
        fresh = build_index(ss.source, mtime_wall_us=_mtime_wall_us(ss))
        self.session.replace_index(pos, fresh)
        state.pending_bytes = trim_to_line(ss.source, fresh)
        if fresh.n != before:
            tick.changed.append(pos)
            tick.added_rows += max(0, fresh.n - before)
            state.added += max(0, fresh.n - before)
        tick.rebuilt = True
        return fresh.n

    def _truncate(self, pos: int, state: FollowState, tick: FollowTick) -> None:
        """
        Re-index from zero into a new generation -- SPEC 7.2.

        A full `build_index`, not an append: the file's content is new, so
        resuming would index bytes of the new content as a continuation of the
        old. The bytes that were there are not recoverable -- see the module
        docstring on FR-23.
        """
        state.added = 0
        self._reindex(pos, state, tick)
        state.generation += 1
        state.truncations += 1
        state.note = "truncated by its writer; re-read from the start"
        tick.truncated.append(pos)

    def _rotate(self, pos: int, state: FollowState, tick: FollowTick) -> None:
        """
        Freeze this source and open the path's new occupant beside it -- PRD Q7.

        The old handle still serves the renamed file, so the frozen source stays
        entirely readable; it simply stops growing. Opening the successor
        through the session means it gets an alias, a lane colour and a sidecar
        like any other file, which is the point of them being distinct sources.
        """
        ss = self.session.sources[pos]
        # Last chance at anything written to the old file before the rename.
        self._append(pos, state, tick)
        self._mark_stale(pos, state, tick, "rotated; this is the previous file")
        tick.rotated.append(pos)

        path = Path(ss.meta.origin or "")
        if not path.exists():
            return
        # Both sources now have the same origin path, so FR-29's distinguishing
        # aliases cannot tell them apart: left alone they come out as `b` and
        # `b~2`, and which of those is yesterday's log is anybody's guess. The
        # frozen one is named for what it is, and the live one is given back the
        # name the user opened -- which makes both names chosen rather than
        # computed, hence the explicit assignment of each.
        original = ss.alias
        self.session.set_alias(pos, f"{original} (rotated)")
        before = len(self.session.sources)
        self.session.add_files([path])
        if len(self.session.sources) > before:
            self.session.set_alias(len(self.session.sources) - 1, original)
            tick.successors.append(str(path))
            tick.rebuilt = True

    def _mark_stale(self, pos: int, state: FollowState, tick: FollowTick,
                    reason: str) -> None:
        """FR-25. Already-read content stays viewable; it just stops updating."""
        if state.stale:
            return
        state.stale = True
        state.stale_reason = reason
        # Copy attribution has to know (PRD risk R10): the rows are still
        # readable, but their `path:line` citations now point at whatever took
        # the path over.
        self.session.sources[pos].citation_note = reason
        self._unwatch(state)
        tick.went_stale.append(pos)

    def _settle(self, tick: FollowTick) -> None:
        """
        One merge update and one strip invalidation for the whole batch.

        `extend_checkpoints` returns the first merged row that may have moved,
        which is what FR-22's auto-scroll and FR-26's "N new lines below" are
        both measured from -- and it is also the row from which the view's
        painted rows are no longer trustworthy.
        """
        session = self.session
        merge = session.merge
        if merge is None:
            tick.first_changed_row = 0
            return
        if tick.rebuilt:
            session._rebuild()
            tick.first_changed_row = 0
        else:
            tick.first_changed_row = merge.extend_checkpoints()
        if tick.anything:
            session.overview.invalidate()
        if tick.rebuilt:
            # Row text is cached by (source, entry), and after a re-index or a
            # new source those pairs point at different bytes.
            session._text.clear()
        # A rotation added a source and a stale one lost its watcher, so the
        # set being watched is no longer the set that should be. Reconciling
        # here rather than at the call sites is what makes "the successor of a
        # rotation is followed too" true without anyone having to remember it.
        self.sync()


def _mtime_wall_us(ss) -> int:
    """
    The anchor a yearless or dateless format needs (SPEC 4.5), from the live file.

    Re-read per append rather than kept from the open, because the whole point
    of following is that the file is being written to *now*: a log whose lines
    carry no year is anchored by when they were written, and using the mtime
    from an hour ago mis-years an append made either side of midnight on New
    Year's Eve.
    """
    from ..index.civil import epoch_us_to_wall_us

    try:
        st = Path(ss.meta.origin).stat()
    except OSError:
        return 0
    return epoch_us_to_wall_us(int(st.st_mtime * 1_000_000))
