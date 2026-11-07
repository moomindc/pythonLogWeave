"""
The ``LogSource`` contract -- SPEC 3.1.

This is the interface every later source type plugs into: Windows Event Log
(phase 2, D3) and the syslog listener (phase 3, D6) become new implementations
here, not new plumbing elsewhere.

The indexer requires exactly two things of a source: random-access byte reads,
and an extent that may grow. A record-oriented source satisfies that by
serialising its records into a local spill file and exposing *that*, so one
indexing path serves every source type.

**No Qt in this module or anywhere under it** (SPEC 1, enforced by
tests/test_architecture.py).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

HEAD_SAMPLE = 64 * 1024
"""Bytes hashed from the head of a source to detect replacement in place."""


class SourceKind(StrEnum):
    FILE = "file"
    GZIP = "gzip"
    CSV = "csv"
    JSONL = "jsonl"
    SSH = "ssh"
    EVTX = "evtx"          # phase 2
    EVENT_CHANNEL = "evt"  # phase 2
    SYSLOG = "syslog"      # phase 3


class OpenMethod(StrEnum):
    """
    Which rung of the SPEC 7.1.1 ladder actually opened the source.

    Surfaced in the UI because a user needs to know whether they are looking at
    a live file or a frozen snapshot.
    """

    SHARE_ALL = "share_all"
    RETRY = "retry"
    BACKUP_SEMANTICS = "backup_semantics"
    VSS_SNAPSHOT = "vss_snapshot"


@dataclass(frozen=True)
class SourceId:
    """Stable handle for a source within a session. Not a path."""

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass
class SourceMeta:
    """Presentation and merge attributes. All user-adjustable."""

    display_name: str
    kind: SourceKind = SourceKind.FILE
    origin: str = ""                  # path, UNC path, or user@host:/path
    alias: str = ""                   # FR-29; shortest unique part by default
    colour: str = ""                  # source lane colour, FR-28
    skew_us: int = 0                  # D2 / FR-18, added at merge time
    encoding: str = ""                # resolved by the source, FR-5
    follow: bool = True               # FR-24, whether to tail it
    enabled: bool = True              # FR-9, whether it is in the merge


@dataclass(frozen=True)
class SourceIdentity:
    """
    Identity for rotation and truncation detection -- SPEC 7.2.

    Path is deliberately **not** part of it: rotation is precisely the case where
    the same path resolves to a different file.
    """

    volume: int = 0           # Windows volume serial / POSIX st_dev
    file_id: int = 0          # Windows file index / POSIX st_ino
    size: int = 0
    head_sha: str = ""        # blake2b-16 of the first head_len bytes
    head_len: int = 0
    """
    How many bytes ``head_sha`` actually covers -- ``min(size, HEAD_SAMPLE)``.

    Recording the coverage is what makes the hash usable on a small file. Two
    hashes are only comparable when they cover the same prefix: a log that grows
    from 400 bytes to 900 has a different hash purely because the second one
    covers 500 more bytes, and reading that as a rewrite would re-index every
    log on every append for its first few minutes.
    """

    def same_file(self, other: SourceIdentity) -> bool:
        """Identity ignoring size, which changes on every append."""
        return (self.volume, self.file_id) == (other.volume, other.file_id)

    def head_comparable(self, other: SourceIdentity) -> bool:
        """Whether the two head hashes cover the same prefix and can be compared."""
        return self.head_len > 0 and self.head_len == other.head_len

    def replaced_in_place(self, other: SourceIdentity) -> bool:
        """
        Same file, but its head changed -- truncated and rewritten (SPEC 7.2).

        Conservative on purpose. When the two hashes cover different prefix
        lengths they carry no information, and this returns False rather than
        guessing: a false "rewritten" costs a full re-index of a live log every
        time it grows, which is the worse of the two errors by a wide margin.

        For a definitive answer on a file whose head coverage has changed, use
        ``rewritten_given`` with the current bytes, which re-hashes over exactly
        the prefix this identity recorded. ``FileSource.detect_change`` does
        that and implements the whole of SPEC 7.2's table.
        """
        if not self.same_file(other):
            return False
        if not self.head_comparable(other):
            return False
        return self.head_sha != other.head_sha

    def rewritten_given(self, current_head: bytes) -> bool:
        """
        Definitive rewrite test: re-hash the live bytes over this identity's own
        coverage, so the comparison is like for like whatever the file has done
        since.
        """
        if not self.head_sha:
            return False
        return self.head_sha != self.hash_head(current_head, self.head_len)

    @staticmethod
    def hash_head(data: bytes, limit: int = HEAD_SAMPLE) -> str:
        """Hash a prefix. ``limit`` must be the coverage being compared against."""
        return hashlib.blake2b(data[: min(limit, HEAD_SAMPLE)], digest_size=16).hexdigest()


@dataclass
class OpenReport:
    """What happened when the source was opened. Never raises for rung failures."""

    method: OpenMethod = OpenMethod.SHARE_ALL
    can_follow: bool = True
    attempts: list[str] = field(default_factory=list)
    note: str = ""


class ChangeKind(StrEnum):
    UNCHANGED = "unchanged"
    """
    Nothing happened since the last poll.

    A member rather than ``None`` so a poller has one thing to switch on. The
    follow engine sees this result far more often than any other, so making it
    the odd case out would put a special case on the hottest path.
    """

    APPENDED = "appended"
    TRUNCATED = "truncated"
    ROTATED = "rotated"
    VANISHED = "vanished"
    UNREACHABLE = "unreachable"


@dataclass
class ChangeEvent:
    source_id: SourceId
    kind: ChangeKind
    size: int = 0
    note: str = ""


class Watcher(ABC):
    """Returned by ``LogSource.watch``. M6 implements these; M1 returns None."""

    @abstractmethod
    def stop(self) -> None: ...


class SourceError(Exception):
    """A source could not be opened or read. Carries a user-facing message."""

    def __init__(self, message: str, *, origin: str = "", recoverable: bool = True):
        super().__init__(message)
        self.origin = origin
        self.recoverable = recoverable


class LogSource(ABC):
    """
    A growable, randomly-readable stream of bytes with an identity.

    Implementations must never modify, lock, or prevent rotation of what they
    read (NFR-20). On Windows that means opening with FILE_SHARE_DELETE, which
    Python's own ``open()`` does not request -- see ``winfile.py``.
    """

    id: SourceId
    meta: SourceMeta

    report: OpenReport | None = None
    """
    The report from the most recent ``open()``, for callers that used ``with``.

    ``__enter__`` has to return the source itself, so it cannot also return the
    report -- and the report is not incidental: it says which rung of the
    SPEC 7.1.1 ladder opened the file and whether following is possible at all,
    which the UI has to tell the user. Implementations set this in ``open()``.
    """

    @abstractmethod
    def open(self) -> OpenReport:
        """Open the source, and also record the result in ``self.report``."""

    @abstractmethod
    def size(self) -> int:
        """Current extent in bytes. May grow between calls."""

    @abstractmethod
    def read_range(self, start: int, end: int) -> bytes:
        """
        Bytes in ``[start, end)``, clamped to the current extent.

        Must be cheap and repeatable: the paint path calls this for the handful
        of entries on screen, so for a local file it should not cost a syscall.
        """

    @abstractmethod
    def identity(self) -> SourceIdentity: ...

    def data_start(self) -> int:
        """
        First byte of actual content, for a source with a prologue to skip.

        Almost always 0. It is 3 for a UTF-8 file with a byte-order mark, and 2
        for a transcoded UTF-16 one. Without this the mark is indexed as part of
        the first entry's text and renders as a stray glyph on the first row.

        Returning an offset rather than silently re-basing ``read_range`` keeps
        every byte offset in a ``FileIndex`` a true offset into what the source
        serves, which is what copy attribution (D9) and the sidecar cache (M2)
        both depend on.
        """
        return 0

    def watch(self, on_change: Callable[[ChangeEvent], None]) -> Watcher | None:
        """None if the source cannot grow, or if following is not implemented yet."""
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> LogSource:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def default_alias(path: Path | str, others: list[Path | str] | None = None) -> str:
    """
    FR-29: the shortest distinguishing part of a filename.

    Falls back to progressively more of the path while the alias collides with
    another source's.
    """
    p = Path(path)
    candidates = [p.stem, p.name]
    if p.parent.name:
        candidates.append(f"{p.parent.name}/{p.name}")
    taken = {Path(o).stem for o in (others or []) if Path(o) != p}
    for c in candidates:
        if c not in taken:
            return c
    return str(p)
