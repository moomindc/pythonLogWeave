"""
Indexing with the sidecar cache in front of it -- SPEC 5.4, NFR-4.

`build_index` always reads the whole file. This decides whether it has to.

Three outcomes, and the middle one is the one that matters in practice:

* **Hit.** Nothing changed; the arrays are memory-mapped and the file is never
  read. This is NFR-4: under two seconds for a 10 GB set, because almost no
  work happens.
* **Append.** The file grew and its head is unchanged, so every entry already
  indexed is still correct and only the new tail is read. A production log a
  user keeps coming back to is always in this state.
* **Miss.** Full index, then a sidecar is written for next time.

The append path is deliberately the same shape as a follow append (SPEC 7.3).
One implementation, exercised on every reopen rather than only when a file
rotates, which is how it stays correct.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..source.base import LogSource
from . import sidecar
from .detect import Detection
from .fileindex import FileIndex
from .indexer import build_index
from .layoutio import from_json, to_json
from .layouts import TimestampFormat
from .severity import DEFAULT_RULESET, SeverityRuleset


@dataclass
class CacheOutcome:
    """What the cache did. Reported in the UI and asserted in tests."""

    index: FileIndex
    status: str = "miss"
    """One of `hit`, `append`, `miss`, `disabled`."""
    reason: str = ""
    reused_bytes: int = 0
    indexed_bytes: int = 0
    wrote: bool = False

    def describe(self) -> str:
        if self.status == "hit":
            return f"cache hit, {self.index.n:,} entries mapped"
        if self.status == "append":
            return (f"cache append, reused {self.reused_bytes / 1e6:.1f} MB, "
                    f"indexed {self.indexed_bytes / 1e6:.1f} MB")
        return f"indexed {self.indexed_bytes / 1e6:.1f} MB ({self.reason})"


def open_index(
    source: LogSource,
    *,
    mtime_wall_us: int,
    path: Path | None = None,
    use_cache: bool = True,
    root: Path | None = None,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
    write_back: bool = True,
    **build_kwargs,
) -> CacheOutcome:
    """
    Produce a `FileIndex` for `source`, reusing a sidecar where one applies.

    `path` identifies the file for cache purposes. Without it -- an in-memory
    or remote source -- there is nothing stable to key on and the cache is
    skipped rather than guessed at.
    """
    if not use_cache or path is None:
        idx = build_index(source, mtime_wall_us=mtime_wall_us,
                          ruleset=ruleset, **build_kwargs)
        return CacheOutcome(index=idx, status="disabled",
                            indexed_bytes=idx.stats.bytes_indexed)

    path = Path(path)
    try:
        st = os.stat(path)
        size, mtime_ns = st.st_size, st.st_mtime_ns
    except OSError:
        size, mtime_ns = source.size(), 0

    head = source.read_range(0, sidecar.HEAD_SAMPLE)

    def terminator_at(offset: int) -> bool:
        # The byte before the indexed region must have ended a line. If it did
        # not, the final entry in the sidecar covers only part of a line and
        # every offset after it would be wrong -- a failure that shows up as
        # garbled text much later, not as a cache miss here.
        if offset <= 0:
            return False
        return source.read_range(offset - 1, offset) in (b"\n", b"\r")

    found = sidecar.lookup(path, size=size, mtime_ns=mtime_ns, head=head,
                           terminator_at=terminator_at, root=root)

    if found.full:
        return CacheOutcome(index=found.index, status="hit", reason=found.reason,
                            reused_bytes=found.index.indexed_to)

    if found.partial:
        outcome = _append(source, found, mtime_wall_us=mtime_wall_us,
                          ruleset=ruleset, root=root, **build_kwargs)
        if outcome is not None:
            if write_back:
                outcome.wrote = _write(outcome.index, path, size, mtime_ns,
                                       head, root)
            return outcome

    idx = build_index(source, mtime_wall_us=mtime_wall_us, ruleset=ruleset,
                      **build_kwargs)
    out = CacheOutcome(index=idx, status="miss", reason=found.reason,
                       indexed_bytes=idx.stats.bytes_indexed)
    if write_back:
        out.wrote = _write(idx, path, size, mtime_ns, head, root)
    return out


def _write(index: FileIndex, path: Path, size: int, mtime_ns: int,
           head: bytes, root: Path | None) -> bool:
    """
    Write a sidecar, and treat failure as unimportant.

    A cache that cannot be written is a missed speedup, never a reason to fail
    an open: a read-only cache directory, a full disk or a locked file must not
    stop someone reading their logs.
    """
    if not index.n:
        return False
    try:
        sidecar.write(index, path=path, size=size, mtime_ns=mtime_ns,
                      head=head, root=root)
    except (OSError, ValueError):
        return False
    return True


def _append(source: LogSource, found: sidecar.Reuse, *, mtime_wall_us: int,
            ruleset: SeverityRuleset, root: Path | None,
            **build_kwargs) -> CacheOutcome | None:
    """
    Index the appended tail and splice it onto the mapped prefix.

    Returns None when the tail cannot be indexed consistently with the prefix,
    which sends the caller back to a full re-index. Being unable to reuse is
    always an acceptable answer here; producing a spliced index that is subtly
    wrong is not.
    """
    old = found.index
    detection = _detection_for(old)
    if detection is None:
        return None

    # The first line of the tail continues the file's line numbering (D9).
    first_line = _next_line_number(source, old)

    tail = build_index(
        source, mtime_wall_us=mtime_wall_us, ruleset=ruleset,
        start_at=found.resume_at, detection=detection, first_line=first_line,
        **build_kwargs,
    )
    spliced = _splice(old, tail)
    # The prefix's arrays have been copied into `spliced`, so the mapping of
    # the old sidecar is no longer needed -- and must go, because on Windows an
    # open mapping is what stops the superseded sidecar being pruned.
    old.release()
    return CacheOutcome(
        index=spliced,
        status="append",
        reason=found.reason,
        reused_bytes=found.resume_at,
        indexed_bytes=tail.stats.bytes_indexed,
    )


def _next_line_number(source: LogSource, index: FileIndex) -> int:
    """
    The 1-based line number the appended region starts at (D9).

    The last indexed entry may span several physical lines, so its line count
    is read rather than assumed: its stored range runs to the end of the
    indexed region and therefore includes its own terminators, so counting
    them gives the answer exactly. One short read, once per append, against
    getting every copied line number in the tail wrong.
    """
    if not index.n:
        return 1
    lo, hi = index.entry_range(index.n - 1)
    try:
        last = source.read_range(lo, hi)
    except OSError:
        last = b""
    return int(index.line_no[-1]) + max(1, last.count(b"\n"))


def _detection_for(old: FileIndex) -> Detection | None:
    """
    Rebuild the detection the prefix was indexed with (SPEC 5.4).

    Re-detecting on the tail alone is not an option: a few hundred bytes can
    resolve to a different member of the ISO family than the whole file did,
    and the two halves of the index would then disagree about where the
    timestamp sits. Returning None here sends the caller to a full re-index,
    which is always a safe answer.
    """
    if old.layout_json is None:
        return None
    try:
        layout = from_json(old.layout_json)
    except (KeyError, TypeError, ValueError):
        return None
    return Detection(
        fmt=TimestampFormat(name=old.format_name, pattern=""),
        layout=layout,
        confidence=old.confidence,
        severity_offset=old.severity_offset,
        notes=["resumed from sidecar"],
    )


def _splice(old: FileIndex, tail: FileIndex) -> FileIndex:
    """
    Join a mapped prefix and a freshly indexed tail into one index.

    `off` is the subtle one: it has n+1 slots, the last being where the region
    ends (SPEC 2.3). The prefix's final slot and the tail's first slot are the
    same boundary, so one of them is dropped rather than both being kept, which
    would otherwise insert a zero-length entry at the join.
    """
    out = FileIndex(
        source_id=old.source_id or tail.source_id,
        format_name=old.format_name,
        fast_path=old.fast_path and tail.fast_path,
        confidence=min(old.confidence, tail.confidence) if tail.n else old.confidence,
        encoding=old.encoding,
        message_offset=old.message_offset,
        severity_offset=old.severity_offset,
        # Carried deliberately: without it the sidecar written from a spliced
        # index has no layout, so the *next* append cannot rebuild the
        # detection and silently degrades to a full re-index -- which shows up
        # only as every other reopen being slow.
        layout_json=old.layout_json,
        stats=old.stats,
    )
    out.off = np.concatenate([np.asarray(old.off[:-1]), np.asarray(tail.off)])
    out.ts = np.concatenate([np.asarray(old.ts), np.asarray(tail.ts)])
    out.flags = np.concatenate([np.asarray(old.flags), np.asarray(tail.flags)])
    out.line_no = np.concatenate([np.asarray(old.line_no),
                                  np.asarray(tail.line_no)])

    s, t = out.stats, tail.stats
    s.bytes_indexed += t.bytes_indexed
    s.lines += t.lines
    s.entries = out.n
    s.multiline_entries += t.multiline_entries
    s.inferred += t.inferred
    s.unparsed_lines += t.unparsed_lines
    s.chunks += t.chunks
    s.year_rollovers += t.year_rollovers
    s.day_rollovers += t.day_rollovers
    s.notes = list(dict.fromkeys([*s.notes, *t.notes]))

    # Monotonicity has to be re-decided over the join: each half can be sorted
    # on its own while the pair is not, if the tail opens earlier than the
    # prefix closed (SPEC 4.6).
    ts = out.ts
    s.monotonic = bool(ts.size < 2 or np.all(ts[1:] >= ts[:-1]))
    out.order = None if s.monotonic else np.argsort(ts, kind="stable").astype(np.uint64)
    return out


def layout_header(detection: Detection | None) -> dict | None:
    """The layout, ready for a sidecar header. None when there is nothing to save."""
    if detection is None or detection.layout is None:
        return None
    return to_json(detection.layout)
