r"""
``FileSource`` -- local and UNC log files. SPEC 3.1, 3.2; FR-4, FR-4a, NFR-20.

Two read strategies behind one interface:

* **local**: one ``mmap`` over the open handle. ``read_range`` is then a slice of
  mapped memory, which is what SPEC 3.1's "must be cheap and repeatable"
  requires -- the paint path calls it for every entry on screen, several times a
  second, and must not pay a syscall each time.
* **UNC** (``\\server\share\...``): positional reads instead. mmap over SMB is
  unreliable: a mapping can fail outright, and a disconnect surfaces as a
  SIGBUS-equivalent fault inside a memory access rather than as a catchable
  error, which would take the whole process with it (SPEC 3.2, D5).

The handle always comes from ``winfile``, never from ``open()``, so watching a
log never blocks its writer's rotation (NFR-20 -- see that module for the
measurement). The ladder in ``winfile.open_escalating`` supplies the
``OpenReport`` that FR-4a requires be shown to the user.

**One measured caveat on the mmap strategy, against NFR-20.** Share flags do not
govern mapped sections. While a mapping is held, Windows still lets the writer
append, rename and delete -- the rotation cases NFR-20 names -- but it refuses
an open with truncate-on-open semantics (CREATE_ALWAYS, which ``open(path, "w")``
and ``logging.FileHandler(mode="w")`` use) with ERROR_USER_MAPPED_FILE, and it
accepts ``SetEndOfFile`` while silently not shrinking the file. So a
copytruncate-style rotation would believe it had emptied a log that keeps
growing. ``use_mmap=False`` has no such limit and is the escape hatch for a
source whose writer truncates in place; M6 should consider defaulting followed
Windows sources to it. Pinned by
``test_an_active_mapping_blocks_truncation_but_nothing_else``.

Three cases the implementation is shaped around, all of them routine in
production and all of them NFR-21 ("degrade, never crash"):

* **the file vanishes mid-session** -- reads continue from the open handle to
  EOF, exactly as SPEC 7.2 prescribes, because size and identity are taken from
  the descriptor and never from the path.
* **a zero-length file** -- ``mmap`` of 0 bytes is an error on every platform, so
  the mapping is created lazily and the source simply reports ``size() == 0``.
* **the file grows between ``size()`` calls** -- a mapping has a fixed length, so
  the extent is re-stat-ed and the mapping rebuilt when a read reaches past it.

No Qt (SPEC 1).
"""

from __future__ import annotations

import hashlib
import mmap
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from . import encoding as enc
from . import poller, winfile
from .base import (
    HEAD_SAMPLE,
    ChangeEvent,
    ChangeKind,
    LogSource,
    OpenMethod,
    OpenReport,
    SourceError,
    SourceId,
    SourceIdentity,
    SourceKind,
    SourceMeta,
    Watcher,
    default_alias,
)


def make_source_id(origin: str) -> SourceId:
    """
    A session-stable id that is not a path (SPEC 3.1, ``SourceId``).

    Derived from the origin so the same file reopened in one session keeps its
    id, with a short random suffix so the same path opened twice -- which FR-23
    treats as two sources after a rotation -- stays distinguishable.
    """
    digest = hashlib.blake2b(origin.encode("utf-8", "surrogateescape"), digest_size=6).hexdigest()
    return SourceId(f"{digest}-{uuid.uuid4().hex[:4]}")


class FileSource(LogSource):
    """A growable, randomly-readable local or UNC file."""

    def __init__(
        self,
        path: Path | str,
        *,
        source_id: SourceId | None = None,
        meta: SourceMeta | None = None,
        use_mmap: bool | None = None,
        allow_vss: bool = False,
        encoding_override: str = "",
        siblings: list[Path | str] | None = None,
    ) -> None:
        self.path = Path(path)
        origin = str(path)
        self.id = source_id or make_source_id(origin)
        self.meta = meta or SourceMeta(
            display_name=self.path.name,
            kind=SourceKind.FILE,
            origin=origin,
            alias=default_alias(self.path, siblings),
        )
        self.meta.origin = self.meta.origin or origin
        self.report = OpenReport()
        self.encoding_info = enc.EncodingInfo()

        self._use_mmap = (not winfile.is_unc(path)) if use_mmap is None else use_mmap
        self._allow_vss = allow_vss
        self._encoding_override = encoding_override
        self._fd: int | None = None
        self._map: mmap.mmap | None = None
        self._extent = 0
        self._poller: poller.Poller | None = None
        """Set by ``use_poller``; otherwise ``watch`` takes the shared default."""

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> OpenReport:
        if self._fd is not None:
            return self.report
        if self.path.is_dir():
            raise SourceError(
                f"{self.path} is a directory, not a log file", origin=self.meta.origin
            )
        self._fd, self.report = winfile.open_escalating(self.path, allow_vss=self._allow_vss)
        try:
            self._refresh()
            self._resolve_encoding()
        except Exception:
            self.close()
            raise
        return self.report

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
            self._map = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass  # already reaped; closing twice must not fail a session
            self._fd = None

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def _require_fd(self) -> int:
        if self._fd is None:
            raise SourceError(f"{self.path} is not open", origin=self.meta.origin)
        return self._fd

    # -- extent and mapping ------------------------------------------------

    def _refresh(self) -> int:
        """
        Re-stat the descriptor and remap if the file grew. One syscall.

        ``os.fstat`` on the descriptor rather than the path, so this still
        answers after the file has been rotated away or deleted (SPEC 7.2).
        """
        fd = self._require_fd()
        try:
            self._extent = os.fstat(fd).st_size
        except OSError as exc:
            raise SourceError(
                f"Cannot read the size of {self.path}: {exc.strerror or exc}",
                origin=self.meta.origin,
            ) from exc
        if self._use_mmap:
            self._remap()
        return self._extent

    def _remap(self) -> None:
        """
        Rebuild the mapping to cover the current extent.

        A read-only mapping has a fixed length and ``mmap.resize`` is not
        available for ``ACCESS_READ``, so growth means a new mapping. Shrinking
        is left alone: the old mapping is longer than the file, reads past the
        new extent are already clamped by ``read_range``, and dropping it would
        invalidate the mapping a truncation handler may still be reading from.
        """
        if self._extent == 0:
            if self._map is not None:
                self._map.close()
                self._map = None
            return
        if self._map is not None and len(self._map) >= self._extent:
            return
        new_map = mmap.mmap(self._require_fd(), self._extent, access=mmap.ACCESS_READ)
        if self._map is not None:
            self._map.close()
        self._map = new_map

    def unmap(self) -> None:
        """
        Give up the mapping and read positionally from here on -- NFR-20.

        **Measured, not theoretical.** The module docstring records that Windows
        refuses a truncate-on-open (``CREATE_ALWAYS``) against a file with a
        live mapping, with ERROR_USER_MAPPED_FILE. That is what
        ``logging.FileHandler(mode="w")`` does when an application restarts, and
        what ``open(path, "w")`` does from a script -- so a source held mapped
        while being followed makes its writer's own log rotation fail. NFR-20
        says watching must never do that, and it is not a trade: breaking the
        writer is worse than any amount of our own slowness.

        The slowness is small. A 60-row paint over a 200 k-line file measured
        **43 us mapped against 660 us positional** -- 15x worse per read, and
        still 4% of a 16ms frame (NFR-5). Everything bulk reads in chunks, not
        rows, so one syscall covers a megabyte there.

        Called when following starts, which is after the first index has been
        built, so the expensive pass over the whole file still gets the mapping.
        Idempotent, and harmless on a UNC source that never had one.
        """
        self._use_mmap = False
        if self._map is not None:
            self._map.close()
            self._map = None

    def size(self) -> int:
        """Current extent in bytes. Costs one ``fstat`` and may grow between calls."""
        return self._refresh()

    def mapped_extent(self) -> int:
        """
        The extent ``read_range`` can serve without a syscall.

        Exposed for the indexer, which wants to know whether a range it is about
        to read is already resident.
        """
        return self._extent

    # -- reading -----------------------------------------------------------

    def read_range(self, start: int, end: int) -> bytes:
        """
        Bytes in ``[start, end)``, clamped to the current extent -- SPEC 3.1.

        No syscall when the range lies inside the known extent, which is the
        paint path's case. A range reaching past it triggers one ``fstat``, since
        that is either a file that has grown or a caller that over-reached, and
        both are answered by re-checking the real extent rather than by failing.
        Out of range reads return ``b""``; an inverted range returns ``b""``.
        """
        self._require_fd()
        start = max(0, start)
        if end > self._extent:
            self._refresh()
        end = min(end, self._extent)
        if end <= start:
            return b""
        if self._map is not None:
            return self._map[start:end]
        return winfile.pread(self._require_fd(), start, end - start)

    def decode_range(self, start: int, end: int) -> str:
        """
        Read and decode one range for display -- SPEC 3.3.

        The only place bytes become characters. ``errors="replace"``, so this
        cannot raise whatever the file contains (the ``invalid_utf8.log`` case).
        """
        raw = self.read_range(start, end)
        return enc.decode(raw, self.encoding_info, byte_start=max(0, start))

    # -- encoding ----------------------------------------------------------

    def _resolve_encoding(self) -> None:
        if self._encoding_override:
            self.encoding_info = enc.from_name(self._encoding_override)
        else:
            self.encoding_info = enc.detect(self.read_range(0, enc.SCAN_BYTES))
        self.meta.encoding = self.encoding_info.name

    def set_encoding(self, name: str) -> enc.EncodingInfo:
        """
        Override the encoding, instantly and with no re-index -- FR-5, SPEC 3.3.

        Nothing in the index depends on the encoding, because the index is in
        bytes, so this is a field assignment. Passing ``""`` re-detects.

        The detected BOM length is carried over: the bytes at the start of the
        file do not change just because the user disagrees about the codec, and
        forgetting it would show the BOM as a stray character on row 1.
        """
        self._encoding_override = name
        if name:
            self.encoding_info = enc.from_name(name, bom_len=self.encoding_info.bom_len)
            self.meta.encoding = self.encoding_info.name
        elif self.is_open:
            self._resolve_encoding()
        return self.encoding_info

    # -- identity ----------------------------------------------------------

    def identity(self) -> SourceIdentity:
        """
        Identity for rotation and truncation detection -- SPEC 7.2.

        The head hash is re-read every call rather than cached, because
        "truncated and rewritten in place" is detectable *only* by the head
        having changed under an unchanged identity. For the mmapped path that is
        a memory read; for UNC it is a 64KB network read, which is why the follow
        engine calls this on its poll interval and not per frame.
        """
        fd = self._require_fd()
        return winfile.identity_from_fd(fd, head=self.read_range(0, HEAD_SAMPLE))

    def path_identity(self) -> SourceIdentity | None:
        """
        Identity of whatever the path resolves to now; ``None`` if it is gone.

        SPEC 7.2's rotation test: this differing from ``identity()`` means the
        name has been handed to a new file. ``None`` means "rotated away or
        deleted", for which the prescribed action is to keep reading the open
        handle to EOF and then mark the source stale (FR-25).
        """
        try:
            return winfile.identity_of_path(self.path)
        except OSError:
            return None

    def data_start(self) -> int:
        """
        First byte of content: the detected BOM length -- SPEC 3.3, FR-5.

        3 for a UTF-8 file with a byte-order mark, 2 for a UTF-16 one with a BOM,
        0 otherwise (including BOM-less UTF-16, which has no prologue at all).
        Matches the corpus ground truth exactly: entry 0 of ``edge/utf8_bom.log``
        is at byte 3 and entry 0 of ``edge/utf16le.log`` is at byte 2.

        **Nothing is re-based and nothing is transcoded.** Offsets are offsets
        into the bytes on disk, which is the only coordinate space the corpus
        ground truth, the index, copy attribution (D9) and the M2 sidecar key all
        already agree on. A UTF-16 source is handled the way SPEC 3.3 prescribes
        -- by telling the line splitter its terminators are two bytes wide, via
        ``encoding_info.terminator_width`` or ``encoding.terminator_bytes`` --
        not by rewriting the file into UTF-8.
        """
        return self.encoding_info.bom_len

    def detect_change(self, previous: SourceIdentity) -> ChangeKind:
        """
        Classify what happened since ``previous``, per SPEC 7.2's table.

        This exists rather than the caller comparing two ``SourceIdentity``
        values directly because an identity-only comparison cannot decide the
        small-file case. Two head hashes only mean something when they cover the
        same prefix, and for any log under 64KB -- which every log is for its
        first few minutes -- an append extends the covered prefix. Deciding a
        rewrite needs a re-hash over exactly the span the previous sample
        covered, which needs the live bytes, which only a source has.

        ``SourceIdentity.rewritten_given`` is that comparison; this method
        supplies it the bytes. An append then cannot move the hash, and a
        rewrite still does.
        """
        if not self.is_open:
            return ChangeKind.UNREACHABLE
        now = self.identity()
        if not now.same_file(previous):
            return ChangeKind.ROTATED

        # **The path is checked before the size, and that ordering is the fix
        # for a real bug.** SPEC 7.2's table lists "size grew" and "the path
        # resolves to a different file" as separate rows, and both can be true
        # of the same poll: a writer that appends its last lines and then
        # rotates does exactly that. Returning APPENDED on the size and never
        # looking at the path meant a rotation went unnoticed for as long as the
        # old file was still growing -- which, for copytruncate, is forever.
        #
        # Nothing is lost by preferring the rotation, because the prescribed
        # action for one is to read the open handle to EOF first (SPEC 7.2), and
        # `FollowEngine._rotate` does that before freezing the source.
        at_path = self.path_identity()
        if at_path is None:
            return ChangeKind.VANISHED
        if not at_path.same_file(now):
            return ChangeKind.ROTATED

        if now.size < previous.size:
            return ChangeKind.TRUNCATED
        if previous.head_len and previous.rewritten_given(
            self.read_range(0, previous.head_len)
        ):
            return ChangeKind.TRUNCATED          # same inode, rewritten in place
        if now.size > previous.size:
            return ChangeKind.APPENDED
        return ChangeKind.UNCHANGED

    def use_poller(self, p: poller.Poller) -> None:
        """
        Watch through this poller rather than the shared default.

        How a test drives following deterministically: a poller it owns can be
        stepped with ``poll_once`` instead of slept on, so a follow test asserts
        on what came out rather than on whether 250ms was long enough. The
        follow engine also sets it, so one session's sources share one thread
        even when several sessions are open.
        """
        self._poller = p

    def watch(self, on_change: Callable[[ChangeEvent], None]) -> Watcher | None:
        """
        Follow the file, calling ``on_change`` **on the poller's thread** (M6).

        Watching is registered against the **path**, not this source's handle,
        which is what lets a rotation be noticed at all: the handle keeps
        serving the old file quite happily after the name has been given to a
        new one (SPEC 7.2), so a watcher that only looked at the descriptor
        would report "unchanged" forever.

        ``None`` is returned for a source that cannot grow -- a VSS snapshot
        (SPEC 7.1.1 rung 4) is a frozen point in time, and following it would
        poll a path whose content is fixed and tell the user it was live.
        """
        if self._poller is None:
            self._poller = poller.default_poller()
        if self.report is not None and self.report.method is OpenMethod.VSS_SNAPSHOT:
            return None
        # Before the first event, not after: a mapping held even briefly while
        # the writer restarts is enough to fail its truncating reopen. See
        # ``unmap`` for the measurement.
        self.unmap()
        return self._poller.add(self.id, self.path, on_change)

    def __repr__(self) -> str:
        return f"FileSource({str(self.path)!r}, open={self.is_open}, size={self._extent})"
