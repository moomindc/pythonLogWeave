"""
The session -- layer 4, SPEC 1. Qt-free.

Everything the UI needs, and nothing Qt. The view asks for a span of rows and
gets back decoded text, timestamps, source identity and severity; it never
touches a `FileIndex`, a `MergeIndex` or a byte offset.

That boundary is not bureaucratic. It means the whole application short of
painting can be driven from a test or a script, and it is what the architecture
test enforces (`tests/test_architecture.py`).

## Where the per-frame cost goes

`rows()` is called from `paintEvent`, so it is on the 16 ms budget (NFR-5). Two
caches keep it there:

* `MergeIndex`'s block cache, which makes finding *which* entries are on screen
  essentially free while scrolling (measured 98.5% hit rate, SPEC 14.1).
* a decoded-text LRU here, keyed `(source, entry)`, so re-painting the same rows
  during a selection drag, a resize or a repaint costs nothing.

Neither cache holds anything proportional to file size.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from ..index.cached import CacheOutcome, open_index
from ..index.detect import Detection
from ..index.fileindex import FileIndex
from ..source import encoding
from ..source.base import LogSource, SourceError, SourceMeta
from . import palette
from .overview import Overview

TEXT_CACHE_ROWS = 4096
"""
Decoded entries held at once.

Sized for "a few screens either side of the viewport", not for the file: a
screenful is ~60 rows, so this absorbs fast scrolling and every repaint while
staying a fixed, small cost.
"""


@dataclass
class Row:
    """One merged row, ready to paint. No Qt types."""

    row: int
    source: int
    entry: int
    ts: int
    """`wall_us`, including the source's skew."""
    line_no: int
    """1-based line in its own file (D9) -- what copy attribution cites."""
    severity: int
    text: str
    multiline: bool = False
    inferred: bool = False
    """The timestamp was completed from mtime rather than read (SPEC 4.5)."""
    msg_from: int = 0
    """
    Where the message starts within `text`, past a line-leading timestamp.

    `text` stays verbatim -- what is in the file, byte for byte -- and this is
    an offset into it rather than a trim already applied, so the view and the
    clipboard can each decide. Zero when the timestamp is not at the front of
    the line, as in an access log (SPEC 9.5).
    """

    @property
    def message(self) -> str:
        """The row's text with a repeated leading timestamp trimmed off."""
        return self.text[self.msg_from:] if self.msg_from else self.text


@dataclass
class SessionSource:
    """A source, its index, and how it is presented."""

    source: LogSource
    index: FileIndex
    meta: SourceMeta
    detection: Detection | None = None
    error: str = ""
    cache: CacheOutcome | None = None
    """What the sidecar cache did for this source (SPEC 5.4). None if unused."""

    @property
    def alias(self) -> str:
        return self.meta.alias or self.meta.display_name

    @property
    def colour(self) -> str:
        return self.meta.colour

    @property
    def message_offset(self) -> int:
        """
        Characters to skip past a line-leading timestamp, plus its separator.

        The index records how much of the line the timestamp itself occupies;
        the separator after it is a display matter, so it is resolved here. One
        space is the overwhelmingly common case, and anything longer is left
        alone because it is probably alignment the author meant to keep.
        """
        off = self.index.message_offset
        return off + 1 if off else 0


@dataclass
class OpenResult:
    """What happened when files were added -- the UI reports this (FR-19)."""

    opened: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    no_format: list[str] = field(default_factory=list)
    low_confidence: list[tuple[str, float]] = field(default_factory=list)
    cached: int = 0
    """How many of the opened files came wholly or partly from the sidecar cache."""

    @property
    def ok(self) -> bool:
        return bool(self.opened)

    def summary(self) -> str:
        bits = []
        if self.opened:
            cached = f", {self.cached} cached" if self.cached else ""
            bits.append(f"{len(self.opened)} opened{cached}")
        if self.no_format:
            bits.append(f"{len(self.no_format)} with no recognised timestamp")
        if self.low_confidence:
            bits.append(f"{len(self.low_confidence)} low confidence")
        if self.failed:
            bits.append(f"{len(self.failed)} failed")
        return ", ".join(bits) or "nothing opened"


LOW_CONFIDENCE = 0.6
"""Below this, the UI says so rather than quietly showing a guess (FR-19)."""


class Session:
    """
    An open set of log files and the merged view over them.

    Deliberately synchronous. Indexing moves off the UI thread in M2b with the
    process pool; wiring a thread in now would mean building the threading model
    twice, and at M1's measured throughput a 64 MB log indexes in under a second.
    """

    def __init__(self, *, use_cache: bool = True, cache_root: Path | None = None):
        self.use_cache = use_cache
        self.cache_root = cache_root
        self.sources: list[SessionSource] = []
        self._merge = None
        self._overview: Overview | None = None
        self._text: OrderedDict[tuple[int, int], str] = OrderedDict()

    # ------------------------------------------------------------------
    # opening
    # ------------------------------------------------------------------

    def add_files(self, paths: list[Path | str]) -> OpenResult:
        """
        Open and index files, then rebuild the merge once for the whole batch.

        Batched on purpose: dropping twelve files should cost one checkpoint
        build, not twelve.
        """
        from ..source.file import FileSource

        result = OpenResult()
        for raw in paths:
            path = Path(raw)
            try:
                src = FileSource(path)
                src.open()
                self._add_source(src, path, result)
            except (SourceError, OSError) as exc:
                result.failed.append((str(path), str(exc)))

        self._assign_aliases()
        self._rebuild()
        return result

    def _add_source(self, src: LogSource, path: Path, result: OpenResult) -> None:
        from ..index.civil import epoch_us_to_wall_us

        order = len(self.sources)
        try:
            mtime = epoch_us_to_wall_us(int(path.stat().st_mtime * 1_000_000))
        except OSError:
            mtime = 0

        outcome = open_index(src, mtime_wall_us=mtime, path=path,
                             use_cache=self.use_cache, root=self.cache_root)
        index = outcome.index
        meta = src.meta
        meta.alias = meta.alias or path.stem
        meta.origin = meta.origin or str(path)
        meta.colour = meta.colour or palette.lane_colour(order)

        self.sources.append(SessionSource(source=src, index=index, meta=meta,
                                          cache=outcome))
        result.opened.append(str(path))
        result.cached += outcome.status in ("hit", "append")

        if not index.format_name:
            result.no_format.append(str(path))
        elif index.confidence < LOW_CONFIDENCE:
            result.low_confidence.append((str(path), index.confidence))

    def _assign_aliases(self) -> None:
        """
        Give every source the shortest name that tells it from the others
        (FR-29).

        Recomputed for the whole set on every open, because adding a file can
        change what distinguishes the files already open: `service` has to
        become `prod/service` the moment `staging/service.log` arrives. A name
        the user typed is never recomputed -- they have said what they want it
        called, and silently widening it would undo that.
        """
        from .aliasing import distinguishing_aliases

        if not self.sources:
            return
        # Computed over *every* path, including the renamed ones, then applied
        # only to the rest. A name the user typed is not up for recomputation,
        # but the automatic ones still have to stay distinguishable from it --
        # and from the file behind it, which is what decides how much of the
        # path the others need to show.
        paths = [Path(s.meta.origin or s.alias) for s in self.sources]
        computed = distinguishing_aliases(paths)
        taken = {s.meta.alias for s in self.sources if s.meta.alias_custom}

        for source, alias in zip(self.sources, computed, strict=True):
            if source.meta.alias_custom:
                continue
            if alias in taken:
                # A user has already claimed this exact name for another file.
                alias = f"{alias}~{len(taken)}"
            taken.add(alias)
            source.meta.alias = alias

    def set_alias(self, source: int, alias: str) -> str:
        """
        Rename a source (FR-29). An empty name hands it back to the default.

        Returns the alias in force afterwards, which is not always what was
        asked for: clearing one recomputes it from the paths.
        """
        meta = self.sources[source].meta
        alias = alias.strip()
        if alias:
            meta.alias = alias
            meta.alias_custom = True
        else:
            meta.alias_custom = False
            self._assign_aliases()
        if self._merge is not None:
            # The merge carries source names for diagnostics, so it has to be
            # told; nothing about the order or the index changes.
            self._rebuild()
        return self.sources[source].alias

    def _rebuild(self) -> None:
        from ..merge import MergeIndex, MergeSource

        self._merge = MergeIndex([
            MergeSource(
                index=s.index,
                skew_us=s.meta.skew_us,
                enabled=s.meta.enabled,
                name=s.alias,
            )
            for s in self.sources
        ])
        self._text.clear()
        # The strips' memo is keyed on a generation that covers skews, enabled
        # flags and the row count, but not on the merge object itself -- so a
        # rebuild that leaves all three unchanged (a rename) would otherwise be
        # served a strip aggregated against the previous MergeIndex.
        if self._overview is not None:
            self._overview.invalidate()

    def close(self) -> None:
        for s in self.sources:
            s.source.close()
        self.sources.clear()
        self._merge = None
        self._overview = None
        self._text.clear()

    # ------------------------------------------------------------------
    # what the view asks for
    # ------------------------------------------------------------------

    @property
    def merge(self) -> object | None:
        """
        The `MergeIndex`, for the one consumer that legitimately needs it.

        The overview strips convert row boundaries to times through the
        checkpoint table (SPEC 8.3), which is a merge-layer operation with no
        per-row equivalent up here -- `row_to_time` exists, and eight hundred
        calls to it is eight hundred block replays. Read-only, and read by
        `session.overview` alone; the view still never sees it.
        """
        return self._merge

    @property
    def overview(self) -> Overview:
        """
        Both strips over this session (SPEC 8, FR-32, FR-33).

        One instance per session, holding the aggregation memo. Handed out
        rather than rebuilt because the memo is the thing that keeps a scroll
        from re-aggregating: what moves when you scroll is the viewport box,
        and the histogram underneath it has not changed.
        """
        if self._overview is None:
            self._overview = Overview(self)
        return self._overview

    @property
    def total_rows(self) -> int:
        return self._merge.total if self._merge else 0

    @property
    def is_empty(self) -> bool:
        return self.total_rows == 0

    def time_span(self) -> tuple[int, int]:
        return self._merge.time_span() if self._merge else (0, 0)

    def time_to_row(self, wall_us: int) -> int:
        return self._merge.time_to_row(wall_us) if self._merge else 0

    def row_to_time(self, row: int) -> int:
        return self._merge.row_to_time(row) if self._merge else 0

    def set_skew(self, source: int, skew_us: int) -> None:
        """D2 / FR-18. Rebuilds checkpoints; does not re-index."""
        self.sources[source].meta.skew_us = skew_us
        if self._merge:
            self._merge.set_skew(source, skew_us)
            self._text.clear()

    def set_enabled(self, source: int, enabled: bool) -> None:
        """
        Show or hide a source without closing it (FR-9).

        Recorded on the meta as well as the merge, so it survives a rebuild when
        another file is dropped in -- and so a profile can save it (FR-32).
        Distinct from `follow`, which is about tailing, not visibility.
        """
        self.sources[source].meta.enabled = enabled
        if self._merge:
            self._merge.set_enabled(source, enabled)
            self._text.clear()

    def rows(self, start: int, count: int) -> list[Row]:
        """
        Decoded rows `[start, start + count)`. Called from `paintEvent`.

        Short by design when the range runs past the end, rather than padding or
        raising -- a view scrolled to the bottom asks for more rows than exist
        on every frame, and that is not an error.
        """
        if not self._merge or count <= 0:
            return []
        got = self._merge.rows(start, count)
        out: list[Row] = []
        for i in range(len(got)):
            si = int(got.source[i])
            ei = int(got.entry[i])
            ss = self.sources[si]
            out.append(Row(
                row=start + i,
                source=si,
                entry=ei,
                ts=int(got.ts[i]),
                line_no=int(ss.index.line_no[ei]),
                severity=int(ss.index.severity[ei]),
                text=self._entry_text(si, ei),
                multiline=bool(ss.index.is_multiline[ei]),
                inferred=bool(ss.index.ts_inferred[ei]),
                msg_from=ss.message_offset,
            ))
        return out

    def _entry_text(self, source: int, entry: int) -> str:
        key = (source, entry)
        hit = self._text.get(key)
        if hit is not None:
            self._text.move_to_end(key)
            return hit

        ss = self.sources[source]
        lo, hi = ss.index.entry_range(entry)
        raw = ss.source.read_range(lo, hi)
        info = getattr(ss.source, "encoding_info", None)
        text = encoding.decode(raw, info, byte_start=lo) if info else raw.decode(
            "utf-8", errors="replace"
        )
        # The stored range runs to the start of the next entry, so it carries its
        # own terminator and any blank line after it.
        text = text.rstrip("\r\n")

        self._text[key] = text
        if len(self._text) > TEXT_CACHE_ROWS:
            self._text.popitem(last=False)
        return text

    # ------------------------------------------------------------------
    # reporting (FR-19)
    # ------------------------------------------------------------------

    def describe(self) -> list[str]:
        lines = []
        for i, s in enumerate(self.sources):
            lines.append(
                f"[{i}] {s.alias}: {s.index.n} entries, "
                f"{s.index.format_name or 'no format'}, "
                f"confidence {s.index.confidence:.0%}, {s.index.encoding}"
            )
        if self._merge:
            lines.append(
                f"merged: {self._merge.total} rows, "
                f"{self._merge.stats.checkpoints} checkpoints, "
                f"{self._merge.checkpoint_bytes / 1024:.1f} KiB"
            )
        return lines
