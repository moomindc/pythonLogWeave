"""
``GzipSource`` -- gzipped logs via a decompressed cache copy. D12, SPEC 3.2, 5.4.

**Why decompress rather than stream.** Every layer above this one assumes random
access by byte offset: the index is an array of offsets, the merge seeks into it,
and the paint path reads a handful of ranges out of the middle of the file. A
DEFLATE stream has none of that -- seeking backwards means decompressing from the
start again, so a scroll to the end and back would re-inflate the whole file
twice. D12 therefore settles it: decompress once into the cache directory, then
hand the result to a plain ``FileSource`` and let one indexing path serve both.

The cache key folds in the original's size and mtime (SPEC 5.4's sidecar naming),
so a regenerated ``.gz`` yields a different cache file rather than a stale one,
and the identity the engine sees changes with it.

**Free space is checked before starting**, because the failure this avoids is
ugly: a 300MB archive inflating to 4GB on a nearly-full disk fails halfway
through, after a visible delay, having filled the volume. The estimate comes
from the gzip trailer's ISIZE field, and because that field is only 32 bits the
check is repeated as the write proceeds (see ``estimate_uncompressed_size``).

LRU eviction of the cache is M2 (SPEC 5.4); nothing here deletes anything.

No Qt (SPEC 1).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from .base import (
    ChangeEvent,
    LogSource,
    OpenReport,
    SourceError,
    SourceId,
    SourceIdentity,
    SourceKind,
    SourceMeta,
    Watcher,
    default_alias,
)
from .encoding import EncodingInfo
from .file import FileSource, make_source_id

GZIP_MAGIC = b"\x1f\x8b"

ISIZE_MODULUS = 1 << 32
"""The gzip trailer records the uncompressed size modulo 2**32 (RFC 1952)."""

COPY_CHUNK = 1 << 20

#: Free space demanded on top of the estimate. Decompressing into a volume with
#: no slack left is its own kind of damage, and the estimate can understate.
DISK_HEADROOM = 64 << 20

#: How often, in bytes written, free space is re-checked during decompression.
#: Covers an ISIZE that wrapped, and a volume another process is filling.
RECHECK_EVERY = 64 << 20


def cache_dir() -> Path:
    """
    Where decompressed copies live. SPEC 5.4, alongside the sidecar cache.

    ``%LOCALAPPDATA%\\LogWeave\\cache`` on Windows,
    ``$XDG_CACHE_HOME/logweave/cache`` elsewhere.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "LogWeave" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "logweave" / "cache"


def estimate_uncompressed_size(path: Path | str) -> int:
    """
    Estimate the decompressed size from the gzip trailer's ISIZE field.

    ISIZE is the uncompressed length **modulo 2**32**, so for anything over 4GiB
    it understates by some multiple of 4GiB. Compressed text never exceeds its
    own uncompressed length, so a value below the compressed size proves at least
    one wrap and the estimate is lifted accordingly -- but a 20GB log that
    compresses to 1GB still looks like 4GB here, which is why the disk check is
    also repeated during the write.

    For a multi-member (concatenated) archive the trailer describes only the last
    member, which is the same understatement by another route and is handled the
    same way.
    """
    path = Path(path)
    compressed = path.stat().st_size
    if compressed < 18:  # smaller than an empty gzip member: nothing to estimate
        return 0
    with open(path, "rb") as handle:
        handle.seek(-4, os.SEEK_END)
        isize = int.from_bytes(handle.read(4), "little")
    estimate = isize
    while estimate < compressed:
        estimate += ISIZE_MODULUS
    return estimate


def _check_free_space(target: Path, needed: int, origin: str) -> None:
    try:
        free = shutil.disk_usage(target).free
    except OSError as exc:
        raise SourceError(
            f"Cannot check free space on {target}: {exc.strerror or exc}", origin=origin
        ) from exc
    if free < needed + DISK_HEADROOM:
        raise SourceError(
            f"Not enough free disk space to decompress {origin}. "
            f"It needs about {needed / 1e9:.1f}GB plus {DISK_HEADROOM / 1e9:.1f}GB headroom, "
            f"but only {free / 1e9:.1f}GB is free on {target}. "
            f"Free some space, or point the cache directory at another volume.",
            origin=origin,
            recoverable=False,
        )


class GzipSource(LogSource):
    """
    A ``.gz`` log, exposed as the plain file it decompresses to.

    ``size``, ``read_range`` and ``identity`` all delegate to the inner
    ``FileSource`` over the cache copy. That is deliberate: those are the bytes
    the index describes and the view paints, so reporting the archive's size or
    head hash here would make ``identity().size`` disagree with ``size()`` and
    quietly break the sidecar validation in SPEC 5.4.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        source_id: SourceId | None = None,
        meta: SourceMeta | None = None,
        cache_directory: Path | str | None = None,
        encoding_override: str = "",
        siblings: list[Path | str] | None = None,
    ) -> None:
        self.path = Path(path)
        origin = str(path)
        self.id = source_id or make_source_id(origin)
        self.meta = meta or SourceMeta(
            display_name=self.path.name,
            kind=SourceKind.GZIP,
            origin=origin,
            alias=default_alias(self.path.with_suffix("") if self.path.suffix else self.path,
                                siblings),
        )
        self.meta.kind = SourceKind.GZIP
        self.meta.origin = self.meta.origin or origin
        self.report = OpenReport()
        self.cache_directory = Path(cache_directory) if cache_directory else cache_dir()
        self._encoding_override = encoding_override
        self._inner: FileSource | None = None
        self._cache_path: Path | None = None

    # -- cache -------------------------------------------------------------

    def cache_path(self) -> Path:
        """
        The decompressed copy's path, keyed by canonical path, size and mtime.

        Same scheme as SPEC 5.4's sidecar names, so a rewritten archive maps to a
        new file instead of silently reusing the previous expansion.
        """
        try:
            stat = self.path.stat()
        except OSError as exc:
            raise SourceError(
                f"Cannot open {self.path}: {exc.strerror or exc}", origin=self.meta.origin
            ) from exc
        canonical = str(self.path.resolve()).casefold() if sys.platform == "win32" \
            else str(self.path.resolve())
        key = hashlib.blake2b(canonical.encode("utf-8", "surrogateescape"),
                              digest_size=16).hexdigest()
        return self.cache_directory / f"{key}-{stat.st_size}-{stat.st_mtime_ns}.log"

    def _verify_magic(self) -> None:
        try:
            with open(self.path, "rb") as handle:
                head = handle.read(2)
        except OSError as exc:
            raise SourceError(
                f"Cannot open {self.path}: {exc.strerror or exc}", origin=self.meta.origin
            ) from exc
        if head != GZIP_MAGIC:
            raise SourceError(
                f"{self.path} is not a gzip file (it does not start with 1f 8b).",
                origin=self.meta.origin,
                recoverable=False,
            )

    def _decompress(self, target: Path) -> int:
        """
        Inflate into ``target`` via a ``.part`` file renamed on completion.

        The rename is the commit: a crash or a full disk leaves only the partial
        file, so the presence of ``target`` means a complete expansion and a
        later session can reuse it. A truncated archive is reported rather than
        indexed, because a partial read must never be presented as complete
        (PRD R9, NFR-21).
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        written = 0
        next_check = RECHECK_EVERY
        try:
            with gzip.open(self.path, "rb") as src, open(partial, "wb") as dst:
                while True:
                    block = src.read(COPY_CHUNK)
                    if not block:
                        break
                    dst.write(block)
                    written += len(block)
                    if written >= next_check:
                        _check_free_space(target.parent, COPY_CHUNK, self.meta.origin)
                        next_check = written + RECHECK_EVERY
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            partial.unlink(missing_ok=True)
            raise SourceError(
                f"Cannot decompress {self.path}: {exc}. "
                f"The archive may be truncated or corrupt.",
                origin=self.meta.origin,
                recoverable=False,
            ) from exc
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, target)
        return written

    # -- LogSource ---------------------------------------------------------

    def open(self) -> OpenReport:
        if self._inner is not None:
            return self.report
        self._verify_magic()
        target = self.cache_path()
        if target.exists():
            note = f"Reusing the cached expansion at {target}."
        else:
            estimate = estimate_uncompressed_size(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            _check_free_space(target.parent, estimate, self.meta.origin)
            written = self._decompress(target)
            note = f"Decompressed {self.path.name} to {written} bytes at {target}."

        inner = FileSource(
            target,
            source_id=self.id,
            meta=self.meta,
            encoding_override=self._encoding_override,
        )
        self.report = inner.open()
        self.report.note = (self.report.note + " " + note).strip()
        self._inner = inner
        self._cache_path = target
        return self.report

    def _require_inner(self) -> FileSource:
        if self._inner is None:
            raise SourceError(f"{self.path} is not open", origin=self.meta.origin)
        return self._inner

    def size(self) -> int:
        return self._require_inner().size()

    def read_range(self, start: int, end: int) -> bytes:
        return self._require_inner().read_range(start, end)

    def decode_range(self, start: int, end: int) -> str:
        return self._require_inner().decode_range(start, end)

    def identity(self) -> SourceIdentity:
        return self._require_inner().identity()

    def data_start(self) -> int:
        """The BOM length of the *decompressed* bytes, which are what is indexed."""
        return self._inner.data_start() if self._inner else 0

    @property
    def encoding_info(self) -> EncodingInfo:
        return self._inner.encoding_info if self._inner else EncodingInfo()

    def set_encoding(self, name: str) -> EncodingInfo:
        """Instant override, same contract as ``FileSource.set_encoding`` (FR-5)."""
        self._encoding_override = name
        return self._require_inner().set_encoding(name)

    def watch(self, on_change: Callable[[ChangeEvent], None]) -> Watcher | None:
        """An archive is finished: the decompressed copy cannot grow (SPEC 3.1)."""
        return None

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
            self._inner = None

    def __repr__(self) -> str:
        return f"GzipSource({str(self.path)!r}, cache={self._cache_path})"
