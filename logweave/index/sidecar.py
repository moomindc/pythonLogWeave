"""
The sidecar index cache -- SPEC 5.4, NFR-4, PRD Q8.

Indexing a 10 GB log takes about two minutes. Doing it again every time the file
is opened is the difference between a tool someone reaches for and one they
avoid, so the index is written beside the cache and reopening becomes a memory
map: NFR-4 allows two seconds for a 10 GB set, and a memmap does not read the
file at all.

## The file

A length-prefixed JSON header, then the arrays, each page-aligned so
`np.memmap` can hand them out with no copy:

    magic   8 bytes   b"LWINDEX\\x01"
    hlen    4 bytes   little-endian uint32, length of the header
    header  hlen      UTF-8 JSON
    pad               to the next page boundary
    arrays            off, ts, flags, line_no, and order when present

Page alignment is the whole point of the layout. An unaligned array would have
to be read and copied; an aligned one is handed to the kernel's page cache and
never touched until a paint asks for a row.

## Deciding whether a sidecar may be used

The hard part is not writing the file, it is being certain it still describes
the log. Three signals, checked in order of cost:

- **`parser_version`** -- bumped whenever a change to detection, extraction or
  the flag layout would produce different arrays for the same bytes. A stale
  sidecar from an older parser is silently wrong rather than loudly broken,
  which is the worst kind of cache bug, so this is checked first and cheaply.
- **`head_sha`** over a prefix -- catches the file being replaced by a
  different one at the same path, which is what log rotation does. The prefix
  is at most 64 KB, and its *length* is stored alongside the digest, because a
  log under 64 KB would otherwise hash its own entire contents: appending a
  line would change the digest, growth would be indistinguishable from
  rotation, and the append path could never fire for a young file. Comparing
  over the recorded length instead means a grown file still matches on its
  head, which is what makes tailing a new log cheap.
- **size and mtime** -- catch ordinary growth.

Growth is the interesting case, and it is the one that makes reopening a live
production log fast: if the head still matches and the byte before `indexed_to`
is still a line terminator, every entry already in the sidecar is still correct,
and only the appended tail needs indexing. That is the same path as follow
(SPEC 7.3), which is deliberate -- one implementation, exercised constantly
rather than only during a rotation.

A sidecar is never *trusted* on size alone. A file that shrank, or whose head
changed, is re-indexed from scratch.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .fileindex import FileIndex, IndexStats

MAGIC = b"LWINDEX\x01"
HEADER_LEN = struct.Struct("<I")
FORMAT_VERSION = 2

PARSER_VERSION = 1
"""
Bumped whenever the same bytes would now index differently.

Anything that changes the catalogue, the extraction kernel, the severity
ruleset, the flag bits or the rollover rules changes this. Getting it wrong
means serving arrays that disagree with the current parser, which no later
check can detect -- so when in doubt, bump it.
"""

PAGE = 4096
HEAD_SAMPLE = 64 * 1024
DEFAULT_CAP_BYTES = 10 * 1024**3

_ARRAYS = (("off", np.uint64), ("ts", np.int64),
           ("flags", np.uint8), ("line_no", np.uint32))


def cache_root() -> Path:
    """
    Where sidecars live (SPEC 5.4).

    Per-user and outside the log's own directory: a log directory is often
    read-only, watched by a shipper, or on a share where writing a stray file
    would be noticed and resented.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "LogWeave" / "index"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "logweave" / "index"
    mac = Path.home() / "Library" / "Caches"
    if mac.is_dir():
        return mac / "LogWeave" / "index"
    return Path.home() / ".cache" / "logweave" / "index"


def hash_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def cache_name(path: Path, size: int, mtime_ns: int) -> str:
    """
    The sidecar's filename.

    The path is hashed rather than mangled, so arbitrarily long and arbitrarily
    punctuated paths produce a valid filename and two different paths never
    collide into one cache entry. Size and mtime are in the name as well as the
    header, so a stale entry for a file that has since changed can be swept
    without opening it.
    """
    key = str(Path(path).resolve()).casefold() if os.name == "nt" \
        else str(Path(path).resolve())
    return f"{hash_bytes(key.encode('utf-8', 'surrogatepass'))}-{size}-{mtime_ns}.lwi"


@dataclass(frozen=True)
class Reuse:
    """What may be reused from a sidecar, and why."""

    index: FileIndex | None = None
    resume_at: int = 0
    """Byte offset to resume indexing from. 0 with an index means fully valid."""
    reason: str = ""

    @property
    def full(self) -> bool:
        return self.index is not None and self.resume_at == 0

    @property
    def partial(self) -> bool:
        return self.index is not None and self.resume_at > 0

    @property
    def miss(self) -> bool:
        return self.index is None


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _header(index: FileIndex, *, path: Path, size: int, mtime_ns: int,
            head: bytes) -> dict:
    arrays = []
    offset = PAGE
    for name, dtype in _ARRAYS:
        arr = getattr(index, name)
        arrays.append({"name": name, "dtype": np.dtype(dtype).str,
                       "len": int(arr.size), "offset": offset})
        offset = _align(offset + arr.nbytes)
    if index.order is not None:
        arrays.append({"name": "order", "dtype": np.dtype(np.uint64).str,
                       "len": int(index.order.size), "offset": offset})

    s = index.stats
    return {
        "format_version": FORMAT_VERSION,
        "parser_version": PARSER_VERSION,
        "source": {"path": str(path), "size": size, "mtime_ns": mtime_ns},
        "head_sha": hash_bytes(head),
        "head_len": len(head),
        "source_id": index.source_id,
        "ts_format": index.format_name,
        "confidence": index.confidence,
        "encoding": index.encoding,
        "fast_path": index.fast_path,
        "message_offset": index.message_offset,
        "severity_offset": index.severity_offset,
        "layout": index.layout_json,
        "entries": index.n,
        "indexed_to": index.indexed_to,
        "has_order": index.order is not None,
        "stats": {
            "bytes_indexed": s.bytes_indexed, "lines": s.lines,
            "entries": s.entries, "multiline_entries": s.multiline_entries,
            "inferred": s.inferred, "unparsed_lines": s.unparsed_lines,
            "chunks": s.chunks, "monotonic": s.monotonic,
            "year_rollovers": s.year_rollovers, "day_rollovers": s.day_rollovers,
            "used_fast_path": s.used_fast_path, "notes": list(s.notes),
        },
        "arrays": arrays,
    }


def _align(offset: int) -> int:
    return (offset + PAGE - 1) // PAGE * PAGE


def write(index: FileIndex, *, path: Path, size: int, mtime_ns: int,
          head: bytes, root: Path | None = None) -> Path:
    """
    Write a sidecar for `index`, atomically.

    Written to a temporary file in the same directory and renamed, because a
    half-written sidecar that still passes its header checks would be read back
    as truth. The rename is atomic on both NTFS and POSIX, so a reader sees
    either the old entry or the complete new one.
    """
    root = root or cache_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / cache_name(path, size, mtime_ns)

    header = _header(index, path=Path(path), size=size, mtime_ns=mtime_ns,
                     head=head[:HEAD_SAMPLE])
    blob = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(blob) + len(MAGIC) + HEADER_LEN.size > PAGE:
        # The header is a few hundred bytes of fixed fields plus detection
        # notes; if notes ever made it larger than a page the array offsets
        # computed above would be wrong, so fail rather than write a bad file.
        raise ValueError(f"sidecar header is {len(blob)} bytes, over one page")

    fd, tmp = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(MAGIC)
            fh.write(HEADER_LEN.pack(len(blob)))
            fh.write(blob)
            for spec in header["arrays"]:
                fh.write(b"\0" * (spec["offset"] - fh.tell()))
                arr = getattr(index, spec["name"])
                fh.write(np.ascontiguousarray(arr).tobytes())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    _prune_older(path, keep=target, root=root)
    return target


def _prune_older(path: Path, *, keep: Path, root: Path) -> None:
    """
    Drop superseded sidecars for the same file.

    A growing log gets a new sidecar on every reopen, because size and mtime
    are in the filename. Without this, watching a log that rolls over daily
    would leave a sidecar per visit, each nearly as large as the last, and the
    LRU sweep would spend its whole budget on dead copies of one file.
    """
    prefix = cache_name(path, 0, 0).split("-")[0]
    try:
        stale = [p for p in root.glob(f"{prefix}-*.lwi") if p != keep]
    except OSError:
        return
    for p in stale:
        try:
            p.unlink()
        except OSError:
            continue


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def read_header(sidecar: Path) -> dict | None:
    """The header alone. Returns None for anything that is not a sidecar."""
    try:
        with open(sidecar, "rb") as fh:
            if fh.read(len(MAGIC)) != MAGIC:
                return None
            (hlen,) = HEADER_LEN.unpack(fh.read(HEADER_LEN.size))
            return json.loads(fh.read(hlen).decode("utf-8"))
    except (OSError, ValueError, struct.error, UnicodeDecodeError):
        return None


def _load(sidecar: Path, header: dict) -> FileIndex:
    """Map the arrays. Nothing is read here -- the pages arrive on demand."""
    st = header.get("stats", {})
    index = FileIndex(
        source_id=header.get("source_id", ""),
        format_name=header.get("ts_format", ""),
        fast_path=bool(header.get("fast_path", False)),
        confidence=float(header.get("confidence", 0.0)),
        encoding=header.get("encoding", ""),
        message_offset=int(header.get("message_offset", 0)),
        severity_offset=header.get("severity_offset"),
        layout_json=header.get("layout"),
        stats=IndexStats(**{k: v for k, v in st.items()
                            if k in IndexStats.__dataclass_fields__}),
    )
    for spec in header["arrays"]:
        arr = np.memmap(sidecar, dtype=np.dtype(spec["dtype"]), mode="r",
                        offset=spec["offset"], shape=(spec["len"],))
        setattr(index, spec["name"], arr)
    index.stats.notes = list(index.stats.notes) + ["loaded from sidecar cache"]
    return index


def lookup(path: Path, *, size: int, mtime_ns: int, head: bytes,
           terminator_at: Callable[[int], bool] | None = None,
           root: Path | None = None) -> Reuse:
    """
    Decide what, if anything, may be reused for this file (SPEC 5.4).

    `terminator_at(offset)` is asked whether the byte before the indexed region
    ended a line. It is only consulted for the grown-file case, and only then
    because a sidecar covering a *partial* final line would make every later
    entry's offsets wrong -- a corruption that would show up much later as
    garbled text rather than as a cache miss.
    """
    root = root or cache_root()
    head = head[:HEAD_SAMPLE]

    exact = root / cache_name(path, size, mtime_ns)
    header = read_header(exact)
    if header is not None:
        bad = _incompatible(header, head)
        if bad:
            return Reuse(reason=bad)
        try:
            return Reuse(index=_load(exact, header), reason="exact match")
        except (OSError, ValueError, KeyError) as exc:
            return Reuse(reason=f"unreadable sidecar: {exc}")

    # No entry for this exact size and mtime. The file may still be one we have
    # indexed a prefix of, so look for an older entry for the same path.
    best = _newest_for_path(path, root)
    if best is None:
        return Reuse(reason="no sidecar")

    header = read_header(best)
    if header is None:
        return Reuse(reason="no sidecar")

    # Size first, then the head. A truncated file will usually fail the head
    # check too -- there is less of it than was hashed -- and "file shrank" is
    # the more useful thing to report of the two.
    was = int(header["source"]["size"])
    if size < was:
        return Reuse(reason="file shrank")
    if size == was:
        # Same size, different mtime, same head. A rewrite of identical length
        # is possible, and cheap to get wrong, so this is not reused.
        return Reuse(reason="mtime changed without growth")

    bad = _incompatible(header, head)
    if bad:
        return Reuse(reason=bad)

    indexed_to = int(header.get("indexed_to", 0))
    if indexed_to <= 0 or indexed_to > size:
        return Reuse(reason="sidecar extent does not fit the file")
    if terminator_at is not None and not terminator_at(indexed_to):
        return Reuse(reason="indexed region ends mid-line")

    try:
        index = _load(best, header)
    except (OSError, ValueError, KeyError) as exc:
        return Reuse(reason=f"unreadable sidecar: {exc}")
    return Reuse(index=index, resume_at=indexed_to,
                 reason=f"reusing {indexed_to:,} of {size:,} bytes")


def _incompatible(header: dict, head: bytes) -> str:
    """
    Why this sidecar may not be used, or "" if it may.

    The head is re-hashed over however many bytes the sidecar hashed, not over
    a fixed window: a file that has grown since still matches on the prefix
    that was recorded, which is the whole basis of the append path.
    """
    if header.get("format_version") != FORMAT_VERSION:
        return "sidecar format version differs"
    if header.get("parser_version") != PARSER_VERSION:
        return "parser version differs"
    want = int(header.get("head_len", HEAD_SAMPLE))
    if len(head) < want:
        return "file is shorter than the hashed head"
    if header.get("head_sha") != hash_bytes(head[:want]):
        return "file head changed"
    return ""


def _newest_for_path(path: Path, root: Path) -> Path | None:
    """
    The most recently indexed sidecar for this path, whatever its size/mtime.

    The filename carries a hash of the path, so this is a prefix match on the
    directory listing rather than a scan of every header.
    """
    key = cache_name(path, 0, 0).split("-")[0]
    try:
        candidates = list(root.glob(f"{key}-*.lwi"))
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: _size_in_name(p))


def _size_in_name(p: Path) -> int:
    try:
        return int(p.stem.split("-")[1])
    except (IndexError, ValueError):
        return -1


# ---------------------------------------------------------------------------
# housekeeping (NFR-22)
# ---------------------------------------------------------------------------

def usage(root: Path | None = None) -> tuple[int, int]:
    """(files, bytes) currently in the cache."""
    root = root or cache_root()
    total = count = 0
    for p in root.glob("*.lwi"):
        try:
            total += p.stat().st_size
        except OSError:
            continue
        count += 1
    return count, total


def sweep(cap_bytes: int = DEFAULT_CAP_BYTES, root: Path | None = None) -> int:
    """
    Evict least-recently-used sidecars until the cache fits under `cap_bytes`.

    By access time, not creation: the set of logs someone returns to is a much
    better predictor of what they will open next than the order they first
    opened them in.
    """
    root = root or cache_root()
    entries = []
    total = 0
    for p in root.glob("*.lwi"):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_atime, st.st_size, p))
        total += st.st_size

    freed = 0
    for _, size, p in sorted(entries):
        if total <= cap_bytes:
            break
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        freed += size
    return freed


def clear(root: Path | None = None) -> int:
    """Delete every sidecar. Returns bytes freed (NFR-22, the UI's button)."""
    root = root or cache_root()
    freed = 0
    for p in root.glob("*.lwi"):
        try:
            size = p.stat().st_size
            p.unlink()
        except OSError:
            continue
        freed += size
    return freed
