"""
Scanning a source's bytes for a pattern -- SPEC 11.1, FR-31a, FR-44, FR-45.

Three features ask the same question of a log: a highlight rule wants to know
which rows to mark on the strips (`session.marks`), a find wants the rows a
needle appears on (`session.find`), and a text filter wants the rows to keep
(`session.filters`). They differ in what they do with the answer, not in how
they get it, so the chunking, the pattern compilation and the
match-offset-to-entry arithmetic live here once.

Qt-free, and more than conventionally so: nothing here touches a session's
caches or writes to anything it was handed, which is what lets the whole module
run on a worker thread while the view paints out of the same arrays (see
`ui.markscan`).

## The three scan kernels, and why there are three

Measured on a 64 MB subject (SPEC 14.1g), scanning for a needle that is not
there -- which is the shape of a search someone is actually hunting with:

| How | Throughput | Projected 10 GB |
|---|---|---|
| `mmap.find`, in place | **2257 MB/s** | 4.5s |
| `bytes.find` over 4 MB chunks | 1318 MB/s | 7.8s |
| `bytes.lower()` then `bytes.find` | 600 MB/s | 17s |
| `re.finditer`, `IGNORECASE` literal | **88 MB/s** | 1.9 **minutes** |
| `re.finditer`, a real regex | 1406 MB/s (*) | 7.3s |

(*) unchunked, and the figure belongs to the pattern rather than to regex: this
one starts with a literal run, which `re` uses to skip. An alternation has no
such prefix and falls to 0.39 MB/s for thirty branches (SPEC 14.1f) -- which is
why FR-31a's strip marks are per rule and opt-in.

Two of those rows decide the design:

* **Case-insensitivity is not a flag, it is a different algorithm.**
  `re.IGNORECASE` costs **15x** a case-sensitive literal through the same
  chunked reads, because it gives up the fast substring path and compares
  character by character. Folding the chunk with `bytes.lower()` and searching
  for a pre-folded needle costs one extra pass over the bytes and is **6.8x
  faster** than the flag. The price is stated in `fold_ascii`: it folds ASCII
  only.
* **A literal never needs the bytes copied out.** `LogSource.find` answers out
  of the mapping, and that is worth 1.7x -- the difference between NFR-10's 5s
  and 7.8s on a 10 GB set. Threads were measured and rejected: `mmap.find`
  holds the GIL, so four of them scan a 64 MB subject at 1937 MB/s against one
  thread's 2257 (SPEC 14.1g). Only processes could help, and they are not
  needed.

## What a chunk is

Always **entry-aligned**, from `FileIndex.off`: cuts are placed at the entry
boundary at or before a byte target, so no match can straddle one and there is
no overlap window to get an off-by-one in. An entry bigger than a chunk becomes
a chunk of its own -- the corpus has a 1 MB single line, and a pathological file
could have a bigger one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

CHUNK_BYTES = 4 * 1024 * 1024
"""
How much of a source is read at once.

Chunk size turns out barely to matter for throughput -- 1215 MB/s at 1 MB
against 1295 MB/s at 64 MB, because the per-chunk copy dominates and is linear
either way (SPEC 14.1g). So it is chosen for the other two things it decides:
how often progress can be reported, and how long a cancel waits. 4 MB is about
3 ms of scanning.
"""


def fold_ascii(data: bytes) -> bytes:
    """
    Lowercase the ASCII letters and leave every other byte alone.

    The case-insensitive scan's whole trick (see the module docstring), and its
    limitation, which is stated rather than hidden: **only ASCII folds.** A
    cp1252 log containing `ÉCHEC` will not be found by typing `échec`, because
    `bytes.lower` does not know which codec those bytes are in -- and it must
    not guess, since the same byte is a different letter in two code pages. Log
    *levels* and identifiers, which is what case-insensitive search is for, are
    ASCII in every format in the catalogue.
    """
    return data.lower()


class Needle:
    """One compiled pattern, ready to scan a source's bytes."""

    literal: bytes | None = None
    """The bytes to look for when this is a plain substring search, else None.
    A literal can be answered by `LogSource.find` without copying."""

    folds: bool = False
    """Whether the subject has to be ASCII-folded before searching it."""

    def offsets(self, buf: bytes) -> np.ndarray:
        """Match start offsets within `buf`, ascending."""
        raise NotImplementedError


class _Literal(Needle):
    def __init__(self, needle: bytes, *, folds: bool = False):
        self.literal = needle
        self.folds = folds

    def offsets(self, buf: bytes) -> np.ndarray:
        if self.folds:
            buf = fold_ascii(buf)
        needle = self.literal
        out: list[int] = []
        at = buf.find(needle)
        while at >= 0:
            out.append(at)
            # Overlapping matches count: searching `aa` in `aaa` is two hits on
            # one row, and the row is what everything downstream wants.
            at = buf.find(needle, at + 1)
        return np.array(out, dtype=np.int64) if out else _NONE


class _Regex(Needle):
    def __init__(self, pattern: re.Pattern):
        self._pattern = pattern

    def offsets(self, buf: bytes) -> np.ndarray:
        return np.fromiter((m.start() for m in self._pattern.finditer(buf)),
                           dtype=np.int64)


_NONE = np.zeros(0, dtype=np.int64)


def source_codec(ss) -> str:
    info = getattr(ss.source, "encoding_info", None)
    return info.name if info is not None else "utf-8"


def byte_oriented(ss) -> bool:
    """
    Whether this source's bytes can be searched for text at all.

    A UTF-16 or UTF-32 source's bytes are not a byte-oriented encoding of
    anything a pattern can be written in, and `terminator_width` is exactly the
    test -- `source.encoding` derives it from the codec, so a width above one
    means the text is wider than a byte. Scanning it as bytes would find
    nothing and report it as an absence, which is worse than saying so.
    """
    info = getattr(ss.source, "encoding_info", None)
    return info is None or info.terminator_width == 1


def compile_needle(
    text: str,
    ss,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    multiline: bool = True,
) -> tuple[Needle | None, str]:
    """
    `text` as something that can scan this source's bytes, or why it cannot.

    Bytes rather than characters, because the index addresses bytes: a match's
    offset goes straight into `searchsorted(off, ...)` to find its entry.
    Decoding first would give a character offset with no way back to an entry,
    and decoding per entry would mean a Python loop over millions of them.
    """
    if not text:
        return None, "nothing to look for"
    if not byte_oriented(ss):
        info = ss.source.encoding_info
        return None, f"{info.name} is not byte-oriented, so it is not scanned"

    codec = source_codec(ss)
    try:
        body = text.encode(codec)
    except (LookupError, UnicodeEncodeError):
        return None, f"the pattern cannot be written in {codec}"

    if not regex:
        if case_sensitive:
            return _Literal(body), ""
        # A folded needle has to be searched in a folded subject. The needle is
        # folded here, once per scan rather than once per chunk.
        return _Literal(fold_ascii(body), folds=True), ""

    # A case-insensitive *regex* keeps the flag and its 16x cost. Folding the
    # subject is not available to it: the pattern would have to be folded too,
    # and that changes what `[A-Z]` or `\\bWARN\\b` mean rather than just where
    # they match.
    flags = re.MULTILINE if multiline else 0
    if not case_sensitive:
        flags |= re.IGNORECASE
    try:
        return _Regex(re.compile(body, flags)), ""
    except re.error as exc:
        return None, f"invalid as bytes: {exc}"


def validate_regex(text: str) -> str:
    """Empty if `text` compiles as a regex, else why it does not."""
    try:
        re.compile(text)
    except re.error as exc:
        return str(exc)
    return ""


def chunks(off: np.ndarray, chunk_bytes: int | None = None):
    """
    Entry-aligned byte ranges covering the whole indexed extent.

    `chunk_bytes` defaults to the module's `CHUNK_BYTES` **at call time**, not
    at definition time, so a test can shrink it and get many seams out of a
    small file. A default argument would have frozen the value at import and
    made that silently ineffective.

    The cuts are chosen by byte target and then snapped to the entry boundary
    at or before it, so a match can never straddle a chunk -- which is what
    removes the overlap window a fixed-size read would need, and with it the
    off-by-one that duplicates or loses a hit on the seam.
    """
    for _a, _b, lo, hi in entry_chunks(off, chunk_bytes):
        yield lo, hi


def entry_chunks(off: np.ndarray, chunk_bytes: int | None = None):
    """
    The same cuts as `chunks`, as `(first entry, last entry, lo byte, hi byte)`.

    The entry bounds are what lets a scan **skip** a chunk whose entries a
    cheaper filter has already rejected (SPEC 11.2: "tier 1 narrows the
    candidate set first, so a tier-2 pass over 'ERROR lines in the last hour'
    reads only those bytes"). The byte range alone cannot answer that question,
    and recovering it with a second `searchsorted` per chunk would be doing
    twice what the cut already knows.
    """
    chunk_bytes = int(chunk_bytes or CHUNK_BYTES)
    end = int(off[-1]) if off.size else 0
    if end <= 0 or off.size < 2:
        return
    targets = np.arange(chunk_bytes, end, chunk_bytes, dtype=np.int64)
    cuts = np.searchsorted(off, targets, side="left")
    # An entry bigger than a chunk collapses its neighbouring cuts onto the
    # same boundary, so `unique` leaves it as a chunk of its own rather than
    # producing an empty range.
    bounds = np.unique(np.concatenate(
        [np.array([0], dtype=np.int64), cuts,
         np.array([off.size - 1], dtype=np.int64)]))
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        lo, hi = int(off[a]), int(off[b])
        if hi > lo:
            yield int(a), int(b), lo, hi


def entries_at(index, starts: np.ndarray, *, limit: int | None = None) -> np.ndarray:
    """
    Absolute match offsets -> the entry indices they fall in, deduplicated.

    Three vectorised steps and no loop over entries:

    1. `searchsorted(off, starts, "right") - 1` -- which entry each match is in.
    2. `starts - off[entry] < limit` -- drop a match too deep into an entry, for
       the one caller that has a reason to (`marks` honours `SCAN_LIMIT`,
       because a mark pointing at a row the view then draws plain would be worse
       than no mark). A search does not pass a limit: a hit on the fortieth line
       of a stack trace is exactly the hit someone is looking for.
    3. `unique` -- an entry matching twice is still one row.
    """
    if starts.size == 0:
        return _NONE
    off = np.asarray(index.off, dtype=np.int64)
    where = np.searchsorted(off, starts, side="right") - 1
    np.clip(where, 0, max(0, index.n - 1), out=where)
    if limit is not None:
        keep = (starts - off[where]) < limit
        if not keep.any():
            return _NONE
        where = where[keep]
    return np.unique(where)


@dataclass
class ScanProgress:
    """Shared bookkeeping for a scan that may be cancelled or capped."""

    done: int = 0
    total: int = 0
    hits: int = 0
    skipped: int = 0
    """Bytes not read because `only` had already rejected every entry in them."""
    capped: bool = False
    cancelled: bool = False


def scan_source(
    needle: Needle,
    ss,
    *,
    limit: int | None = None,
    max_hits: int | None = None,
    only: np.ndarray | None = None,
    progress=None,
    cancelled=None,
    state: ScanProgress | None = None,
    label: str = "",
    on_batch=None,
) -> tuple[np.ndarray, ScanProgress]:
    """
    Every entry of one source that `needle` matches, as entry indices.

    `progress(done, total, label)` and `cancelled() -> bool` are both optional
    and both called per chunk, which is often enough to drive a progress bar and
    a cancel button and seldom enough not to matter.

    Takes the literal fast path -- `LogSource.find` straight over the mapping,
    no bytes copied -- whenever the pattern is a case-sensitive literal. That is
    not a micro-optimisation: it is 1.6x, and it is what puts NFR-10's 10 GB
    inside five seconds.

    `only` is an entry mask from a cheaper predicate: a chunk none of whose
    entries are still candidates is skipped without being read, which is what
    makes a text filter inside a time window read only that window's bytes.

    `on_batch(entries)` is called with each chunk's entries as they are found,
    for the caller that has to show results **before** the scan finishes
    (NFR-10). It is per chunk and not per source on purpose: a 10 GB single
    file is one source, and a find that showed nothing until it finished would
    meet the letter of "incremental" and none of the point.
    """
    st = state or ScanProgress()
    index = ss.index
    off = np.asarray(index.off, dtype=np.int64)
    found: list[np.ndarray] = []

    use_find = needle.literal is not None and not needle.folds
    for a, b, lo, hi in entry_chunks(off):
        if cancelled is not None and cancelled():
            st.cancelled = True
            break
        if only is not None and not only[a:b].any():
            # Every entry in this chunk was already rejected by a cheaper
            # predicate, so there is nothing a match here could change.
            st.done += hi - lo
            st.skipped += hi - lo
            if progress is not None:
                progress(st.done, st.total, label)
            continue
        if use_find:
            starts = _find_in_place(ss.source, needle.literal, lo, hi)
        else:
            raw = ss.source.read_range(lo, hi)
            starts = needle.offsets(raw)
            if starts.size:
                starts = starts + lo
        st.done += hi - lo
        if starts.size:
            st.hits += int(starts.size)
            hit = entries_at(index, starts, limit=limit)
            if hit.size:
                found.append(hit)
                if on_batch is not None:
                    on_batch(hit)
        if progress is not None:
            progress(st.done, st.total, label)
        if max_hits is not None and st.hits >= max_hits:
            st.capped = True
            break

    if not found:
        return _NONE, st
    return np.unique(np.concatenate(found)), st


def _find_in_place(source, needle: bytes, lo: int, hi: int) -> np.ndarray:
    """Every offset of `needle` in `[lo, hi)`, without copying the range out."""
    out: list[int] = []
    at = source.find(needle, lo, hi)
    while at >= 0:
        out.append(at)
        at = source.find(needle, at + 1, hi)
    return np.array(out, dtype=np.int64) if out else _NONE
