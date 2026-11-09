"""
Where in the whole file a rule's hits are -- FR-31a, FR-44, SPEC 8.4, 10.1.

Painting a highlight rule in the view is a question about the forty-eight rows
on screen. Marking it on an overview strip is a different question -- *where in
the merged stream* are the hits -- and a strip covers every row of every source,
so the only honest answer is to scan all of them. This module is that scan, and
it is Qt-free like the rest of the engine: it produces sorted timestamps, and
the widget turns them into pixels.

## The measurement that shaped it

`re` has no DFA. An alternation is tried branch by branch at every position, so
a scan's throughput falls roughly linearly in the number of branches. From the
committed baseline (`python tools/bench.py`, SPEC 14.1f):

| Pattern | Throughput | 64 MB set |
|---|---|---|
| one case-sensitive literal | 439 MB/s | 0.1 s |
| one case-insensitive literal | 40.7 MB/s | 1.6 s |
| three rules, one alternation | 6.9 MB/s | 9.3 s |
| five rules, one alternation | 3.8 MB/s | 16.6 s |
| five rules, five separate passes | 8.6 MB/s | 7.4 s |
| the full default ruleset (30 branches) | 0.39 MB/s | 166 s |

The absolute figures move 20-30% run to run on a laptop under thermal load, as
the harness itself warns; the **ratios** are what is stable, and they are what
the design turns on. Two things follow, both of them in the design rather than
in a comment:

* **Rules are scanned one at a time, not as one alternation.** That is the
  opposite of the paint path, where SPEC 10.1's single alternation is right
  because the per-row cost of entering the regex engine dominates. Here the
  subject is tens of megabytes and the branch count dominates, so five separate
  passes beat one five-branch pass by **2.2x** on the same subject.
* **Marking is opt-in per rule** (`HighlightRule.on_strips`). Marking the whole
  default ruleset would be **most of three minutes on a 64 MB set**. Marking the
  two or three patterns a reader is actually hunting for is a second or two, in
  the background.

## What a mark scan sees that the view does not

Deliberately not identical to the paint path, and the differences are the
useful way round:

* **The whole entry, not the first line of it.** A collapsed row draws one
  physical line (SPEC 9.1), so the view cannot colour an `OutOfMemoryError` on
  the fourth line of a stack trace. A strip exists to tell you that entry is
  there, so the scan reads all of it.
* **`^` anchors at a line start in the file.** In the view it anchors at the
  start of what is drawn, which moves when the timestamp column trims a
  duplicate (SPEC 9.5). A mark whose position depended on a column being shown
  would be a mark that moved when you tidied the display.
* **`SCAN_LIMIT` is honoured.** A match past 2048 bytes into an entry does not
  mark, because it would not colour either (`rules.SCAN_LIMIT`), and a strip
  that pointed at rows the view then showed as plain would be worse than no
  strip. The limit is characters there and bytes here, which is the same number
  for the ASCII that log text overwhelmingly is.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

import numpy as np

from ..highlight.rules import SCAN_LIMIT, HighlightRule
from .scan import (
    Needle,
    ScanProgress,
    byte_oriented,
    scan_source,
    source_codec,
)

__all__ = ["MAX_HITS", "MarkLayer", "MarkScan", "marking_rules", "scan", "scan_key"]

MAX_HITS = 200_000
"""
Most hits kept per rule, per source.

A cap rather than a buffer that grows: a rule matching a million rows marks
every pixel of the strip, which says nothing, and collecting a million match
objects to say it is the slowest way to say nothing. Past the cap the scan of
that source stops and the layer reports itself as truncated, which the dialog
shows -- a rule that hits this is a rule to narrow, and the user is told so
rather than left waiting.
"""


@dataclass(frozen=True)
class MarkLayer:
    """
    One rule's hits across the session, as sorted merged-time timestamps.

    Timestamps and not rows, and that is what makes the layer survive things
    that would otherwise invalidate it. A row number moves when a source is
    enabled or disabled (FR-9) or when a followed file appends in the middle
    (SPEC 6.4); a wall time does not. Both strips already work in merged time
    -- `Strip.times` is their common axis -- so a layer drawn against it needs
    no knowledge of which strip it is on.
    """

    rule: str
    colour: str
    times: np.ndarray
    """`(hits,)` int64 merged `wall_us`, sorted. Skew already applied."""

    truncated: bool = False
    """`MAX_HITS` was reached on at least one source; see the constant."""

    @property
    def hits(self) -> int:
        return int(self.times.size)

    def counts(self, edges: np.ndarray) -> np.ndarray:
        """
        Hits falling in each range of `edges` -- `(len(edges) - 1,)` int64.

        Two `searchsorted`s over a sorted array, so a repaint costs
        O(pixels x log hits) and is independent of how much log is open. This is
        the whole reason the layer is a sorted time array rather than a row set:
        the strips hand their pixel edges straight in.

        The last edge is made inclusive, for the same reason `_aggregate` pushes
        its final edge one bucket on: a strip's edges are
        `linspace(first, last, H + 1)`, so the last one sits exactly *on* the
        last entry's timestamp, and a half-open range would drop a hit landing
        there. One missing mark out of thousands looks precisely like a correct
        strip, which is what made the same mistake in `_aggregate` survive
        review.
        """
        edges = np.asarray(edges, dtype=np.int64)
        if edges.size < 2 or not self.times.size:
            return np.zeros(max(0, edges.size - 1), dtype=np.int64)
        edges = edges.copy()
        edges[-1] += 1
        return np.diff(np.searchsorted(self.times, edges, side="left"))

    def pixels(self, edges: np.ndarray) -> np.ndarray:
        """
        Which pixel rows have any hit at all -- what `overlay_marks` takes.

        Presence, not density. A mark's job is "there is one here"; how many
        there are is what the band underneath already shows.
        """
        return np.flatnonzero(self.counts(edges) > 0)


@dataclass
class MarkScan:
    """Every layer for one (ruleset, session) pair, with what it cost."""

    layers: tuple[MarkLayer, ...] = ()
    notes: tuple[str, ...] = ()
    """
    Why a rule or a source contributed nothing. Reported, never raised.

    Same posture as `Ruleset.rejected`: one unscannable source must not stop
    the other seven, and a user who asked for a mark and got none is owed the
    reason.
    """

    bytes_scanned: int = 0
    elapsed_ms: float = 0.0
    cancelled: bool = False
    key: str = ""

    def by_rule(self) -> dict[str, MarkLayer]:
        return {layer.rule: layer for layer in self.layers}

    @property
    def hits(self) -> int:
        return sum(layer.hits for layer in self.layers)


def marking_rules(rules: list[HighlightRule]) -> list[HighlightRule]:
    """The enabled rules that asked to be on the strips, in rule order."""
    return [r for r in rules if r.enabled and r.on_strips and not r.validate()]


def scan_key(session, rules: list[HighlightRule]) -> str:
    """
    A digest of everything a scan's result depends on.

    What a cached scan must be invalidated by, and nothing else: the patterns
    and their flags, which sources are in and switched on, their skews, and how
    many entries each has -- the last of which is what makes a followed file's
    append produce a new key (SPEC 7.3). Deliberately not a content hash; the
    entry count moves whenever the bytes behind it do.
    """
    parts = [f"{r.name}\x1f{r.regex_source()}" for r in marking_rules(rules)]
    for i, ss in enumerate(getattr(session, "sources", [])):
        parts.append(f"{i}:{int(ss.meta.enabled)}:{int(ss.meta.skew_us)}:"
                     f"{int(ss.index.n)}")
    return hashlib.blake2b("\x1e".join(parts).encode("utf-8"),
                           digest_size=12).hexdigest()


def scan(session, rules: list[HighlightRule], *,
         progress=None, cancelled=None) -> MarkScan:
    """
    Scan every enabled source for every marking rule.

    `progress(done_bytes, total_bytes, rule_name)` and `cancelled() -> bool`
    are both optional and are both called often enough to drive a progress bar
    and a cancel button; neither touches the session, so this whole function
    runs on a worker thread (see `ui.markscan`).

    Ordered rule-outermost so that a cancel leaves whole layers rather than
    partial ones: a layer missing is honest, a layer that silently covers the
    first half of a file is not.
    """
    started = time.perf_counter()
    wanted = marking_rules(rules)
    sources = [ss for ss in getattr(session, "sources", [])
               if ss.meta.enabled and ss.index.n]
    total = sum(int(ss.index.indexed_to) for ss in sources) * max(1, len(wanted))

    layers: list[MarkLayer] = []
    notes: list[str] = []
    done = 0
    stop = False

    for rule in wanted:
        times: list[np.ndarray] = []
        truncated = False
        for ss in sources:
            pattern, why = _bytes_pattern(rule, ss)
            if pattern is None:
                notes.append(f"{rule.name}: {ss.alias}: {why}")
                done += int(ss.index.indexed_to)
                continue
            got, hit_cap, read = _scan_source(
                pattern, ss, progress=progress, done=done, total=total,
                rule=rule.name, cancelled=cancelled)
            done += read
            truncated = truncated or hit_cap
            if hit_cap:
                notes.append(
                    f"{rule.name}: {ss.alias}: more than {MAX_HITS:,} hits, "
                    f"stopped there -- narrow the pattern")
            if got.size:
                times.append(got)
            if cancelled is not None and cancelled():
                stop = True
                break
        if stop:
            break
        merged = (np.sort(np.concatenate(times)) if times
                  else np.empty(0, dtype=np.int64))
        layers.append(MarkLayer(rule=rule.name, colour=_colour_of(rule),
                                times=merged, truncated=truncated))

    return MarkScan(
        layers=tuple(layers),
        notes=tuple(notes),
        bytes_scanned=done,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        cancelled=stop,
        key=scan_key(session, rules),
    )


def _colour_of(rule: HighlightRule) -> str:
    """
    What a mark is drawn in.

    The rule's foreground, or its background if that is all it sets -- a rule
    styled only by a row tint still has a colour, and a mark with no colour at
    all would be invisible. The fallback is the caller's (`ui.strips`), which
    is also the only layer that knows what the theme's default text colour is.
    """
    return rule.fg or rule.bg or ""


def _bytes_pattern(rule: HighlightRule, ss) -> tuple[re.Pattern | None, str]:
    """
    The rule as a pattern over this source's raw bytes, or why it cannot be.

    Bytes rather than text, because the index addresses bytes: a match's offset
    goes straight into `searchsorted(off, ...)` to find its entry. Decoding
    first would mean a character offset with no way back to an entry, and
    decoding per entry instead would mean a Python loop over millions of them.

    A rule is compiled as a regex even when its pattern is a plain word, which
    is the one place this differs from `session.find`: a rule's `regex_source`
    is already a regex -- it carries the word-boundary and case flags the rule
    editor set -- so there is no literal to hand to the faster path. Two
    sources refuse:

    * **A UTF-16 or UTF-32 source.** Its bytes are not a byte-oriented encoding
      of the pattern (`scan.byte_oriented`). Rather than scan it wrongly, the
      source is reported. This is also the only place in the engine that would
      have cared, because everything else treats a file as bytes and decodes on
      demand.
    * **A pattern the source's codec cannot hold**, e.g. a rule containing a
      character that is not in cp1252 against a cp1252 log. There is nothing in
      those bytes that could match it, so the honest answer is to say so.
    """
    if not byte_oriented(ss):
        info = ss.source.encoding_info
        return None, f"{info.name} is not byte-oriented, so it is not scanned"

    codec = source_codec(ss)
    try:
        body = rule.regex_source().encode(codec)
    except (LookupError, UnicodeEncodeError):
        return None, f"the pattern cannot be written in {codec}"
    # MULTILINE, so `^` and `$` mean a line of the file. See the module
    # docstring for why that is a better anchor here than the view's.
    try:
        return re.compile(body, re.MULTILINE), ""
    except re.error as exc:
        return None, f"invalid as bytes: {exc}"


def _scan_source(pattern: re.Pattern, ss, *, progress, done: int, total: int,
                 rule: str, cancelled) -> tuple[np.ndarray, bool, int]:
    """
    One rule over one source. Returns (timestamps, hit the cap, bytes read).

    The scan itself and the match-to-entry arithmetic are `session.scan`'s,
    shared with find and with text filters; what is left here is turning the
    entries into the merged *timestamps* a `MarkLayer` holds, and applying the
    source's skew.
    """
    state = ScanProgress(done=done, total=total)
    rows, state = scan_source(
        _Compiled(pattern), ss, limit=SCAN_LIMIT, max_hits=MAX_HITS,
        progress=progress, cancelled=cancelled, state=state, label=rule)
    read = state.done - done
    if rows.size == 0:
        return np.empty(0, dtype=np.int64), state.capped, read
    ts = np.asarray(ss.index.ts, dtype=np.int64)[rows] + int(ss.meta.skew_us)
    return ts, state.capped, read


class _Compiled(Needle):
    """A rule's compiled bytes pattern as a `scan.Needle`."""

    def __init__(self, pattern: re.Pattern):
        self._pattern = pattern

    def offsets(self, buf: bytes) -> np.ndarray:
        return np.fromiter((m.start() for m in self._pattern.finditer(buf)),
                           dtype=np.int64)
