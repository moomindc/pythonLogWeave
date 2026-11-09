"""
Show only the rows matching a predicate -- FR-45, FR-48, NFR-9, SPEC 11.2.

A filter produces one **entry mask per source**, and that is the whole of its
output. Everything downstream is arithmetic on masks: the merge builds a
filtered view over them (`MergeIndex.filtered`), the strips derive their
histograms from them, and clearing the filter is dropping them -- the
unfiltered merge was never thrown away, which is what FR-45's "reversible
without re-indexing" asks for.

## Two tiers, because they cost two different things

**Tier 1 -- metadata.** Severity set, source set, time window. Evaluated
directly on the `flags` and `ts` arrays as boolean masks, with no text access
at all: a handful of vectorised passes, independent of how wide the lines are.
This is what meets NFR-9, together with the derived checkpoint table that
`MergeIndex.derive_checkpoints` explains -- the masks were never the expensive
half.

**Tier 2 -- text.** Include and exclude, literal or regex. Requires reading
bytes, so it is the same scan a find is (`session.scan`) and is governed by
NFR-10 rather than NFR-9. It runs in the background and the view becomes
usable at once, its row count growing as the pass proceeds.

Tier 1 runs first and is handed to tier 2 as `only`, so a text filter inside a
time window reads only that window's bytes -- the chunks outside it are skipped
without being read at all.

## What the overview strips do under a filter

The strips are drawn from the per-source density histogram (SPEC 8.1), which
counts **every** entry. Left alone, a filtered view would show a minimap whose
bands described rows that are not in it -- and for a severity filter, bands in
colours the filter has just excluded.

It turns out almost every filter can be applied to the histogram in the
histogram's own terms, for nothing:

* a **severity** set is a column selection -- `counts` is
  `[buckets, 8]` by severity class, so the excluded classes are zeroed;
* a **source** set drops whole histograms, which is what disabling a source
  already does;
* a **time window** needs nothing: the strips' edges span the filtered merge's
  own first and last row, so the buckets outside the window are never read.

Only a **text** filter needs the histogram rebuilt from the kept timestamps,
and that pass is a fraction of the byte scan it accompanies, so it is the one
case where paying O(entries) is free by comparison.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from ..index.histogram import Histogram
from ..index.severity import Severity
from . import scan as scanning

ALL_SEVERITIES = frozenset(int(s) for s in Severity)

PUBLISH_MS = 120.0
"""How often a running tier-2 pass publishes what it has. The same cadence as
`find.PARTIAL_EVERY_MS`, so the two features feel like one."""


@dataclass(frozen=True)
class Filter:
    """
    A composable predicate over merged rows (FR-45). Hashable and comparable,
    so "has this changed?" is `==` and needs no revision counter.

    An empty collection means "no restriction" rather than "nothing", which is
    the reading that makes a half-built filter show everything instead of an
    empty view while someone is still ticking boxes.
    """

    severities: frozenset[int] = frozenset()
    sources: frozenset[int] = frozenset()
    """Source *positions* in the session. A filter is UI state over the set of
    files that is open; opening or closing one re-evaluates it."""
    since: int | None = None
    """Inclusive lower bound, merged `wall_us` (FR-48)."""
    until: int | None = None
    """Inclusive upper bound, merged `wall_us`."""
    include: str = ""
    exclude: str = ""
    regex: bool = False
    case_sensitive: bool = False

    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self.severities or self.sources or self.include
                    or self.exclude
                    or self.since is not None or self.until is not None)

    @property
    def needs_text(self) -> bool:
        """Whether this filter has to read bytes -- tier 2."""
        return bool(self.include or self.exclude)

    @property
    def tier(self) -> int:
        if not self.active:
            return 0
        return 2 if self.needs_text else 1

    def validate(self) -> str:
        if not self.regex:
            return ""
        for text in (self.include, self.exclude):
            if text and (why := scanning.validate_regex(text)):
                return why
        return ""

    def describe(self) -> str:
        """One short line for the status bar. Empty when nothing is set."""
        bits: list[str] = []
        if self.severities and self.severities != ALL_SEVERITIES:
            names = [Severity(s).name for s in sorted(self.severities)]
            bits.append("/".join(names) if len(names) <= 3
                        else f"{len(names)} severities")
        if self.sources:
            bits.append(f"{len(self.sources)} source"
                        f"{'' if len(self.sources) == 1 else 's'}")
        if self.since is not None or self.until is not None:
            bits.append("time window")
        if self.include:
            bits.append(f"contains {self.include!r}")
        if self.exclude:
            bits.append(f"not {self.exclude!r}")
        return ", ".join(bits)

    def with_window(self, since: int | None, until: int | None) -> Filter:
        """FR-48: the timeline hands a dragged range straight to this."""
        from dataclasses import replace
        return replace(self, since=since, until=until)


@dataclass
class Filtered:
    """
    An evaluated filter: the masks, the histograms, and what it cost.

    `None` in `masks` means "all of this source", which is both the cheap
    representation and the one that lets `MergeSource` skip building a
    `Selection` at all.
    """

    filt: Filter = Filter()
    masks: list[np.ndarray | None] = field(default_factory=list)
    hists: list[Histogram | None] = field(default_factory=list)
    kept: int = 0
    total: int = 0
    notes: tuple[str, ...] = ()
    cancelled: bool = False
    complete: bool = True
    bytes_scanned: int = 0
    bytes_total: int = 0
    bytes_skipped: int = 0
    elapsed_ms: float = 0.0
    key: str = ""

    @property
    def empty(self) -> bool:
        """True when the predicate passed nothing -- the view is blank."""
        return self.kept == 0

    def describe(self) -> str:
        shown = f"{self.kept:,} of {self.total:,} rows"
        if not self.complete:
            shown += " so far"
        return shown


def filter_key(session, filt: Filter) -> str:
    """What a cached evaluation is invalidated by: the filter and the shape of
    the session it was evaluated against."""
    shape = ";".join(f"{i}:{ss.meta.origin}:{int(ss.index.n)}"
                     for i, ss in enumerate(getattr(session, "sources", [])))
    return f"{hash(filt)}|{shape}"


def tier1_masks(session, filt: Filter) -> list[np.ndarray | None]:
    """
    The metadata predicate as one mask per source. **NFR-9's share of the work.**

    A couple of vectorised comparisons per source and nothing read from disk.
    `None` where a source is unrestricted, so an unfiltered source costs not
    even a mask.
    """
    out: list[np.ndarray | None] = []
    wanted = (filt.severities if filt.severities
              and filt.severities != ALL_SEVERITIES else None)
    for i, ss in enumerate(getattr(session, "sources", [])):
        index = ss.index
        n = int(index.n)
        if filt.sources and i not in filt.sources:
            out.append(np.zeros(n, dtype=bool))
            continue
        mask: np.ndarray | None = None
        if wanted is not None:
            mask = np.isin(np.asarray(index.severity),
                           np.fromiter(sorted(wanted), dtype=np.uint8))
        if filt.since is not None or filt.until is not None:
            skew = int(ss.meta.skew_us)
            ts = np.asarray(index.ts, dtype=np.int64)
            window = np.ones(n, dtype=bool)
            if filt.since is not None:
                window &= ts >= (int(filt.since) - skew)
            if filt.until is not None:
                window &= ts <= (int(filt.until) - skew)
            mask = window if mask is None else (mask & window)
        out.append(mask)
    return out


def histograms(session, filt: Filter,
               masks: list[np.ndarray | None]) -> list[Histogram | None]:
    """
    The per-source histogram the overview strips should use (SPEC 8.1).

    See the module docstring for why a severity or source filter costs nothing
    here and only a text filter has to rebuild. A rebuild is skipped for a
    source nothing was kept from: there is no histogram of no entries, and the
    strips already treat `None` as "no overview from this source".
    """
    out: list[Histogram | None] = []
    wanted = (filt.severities if filt.severities
              and filt.severities != ALL_SEVERITIES else None)
    for i, ss in enumerate(getattr(session, "sources", [])):
        hist = ss.index.hist
        mask = masks[i] if i < len(masks) else None
        if hist is None or (filt.sources and i not in filt.sources):
            out.append(None)
            continue
        if not filt.needs_text:
            out.append(_columns(hist, wanted) if wanted else hist)
            continue
        if mask is None:
            out.append(hist)
            continue
        if not mask.any():
            out.append(None)
            continue
        out.append(Histogram.build(np.asarray(ss.index.ts)[mask],
                                   np.asarray(ss.index.severity)[mask]))
    return out


def _columns(hist: Histogram, wanted: frozenset[int]) -> Histogram:
    """
    The same histogram with the excluded severity classes zeroed.

    A copy rather than a view, because `counts` may be the sidecar's memory map
    and writing through it would corrupt the cache on disk -- the same hazard
    `Histogram._make_writeable` exists for.
    """
    counts = np.array(hist.counts, dtype=np.uint32, copy=True)
    drop = [c for c in range(counts.shape[1]) if c not in wanted]
    if drop:
        counts[:, drop] = 0
    return Histogram(t0=hist.t0, bucket_us=hist.bucket_us, counts=counts)


def apply(session, filt: Filter, *, progress=None, cancelled=None,
          partial=None) -> Filtered:
    """
    Evaluate `filt` over `session`: tier 1, then tier 2 if there is text.

    `progress(done, total)`, `cancelled() -> bool` and `partial(Filtered)` are
    the same contract as `find.search`, and for the same reason -- this runs on
    a worker thread, reads only the session's arrays and the sources' bytes, and
    writes nothing back (see `ui.findscan`).

    A tier-2 pass that is cancelled part way returns what it had, marked
    `complete=False`. That is deliberately *usable*: a filter whose text pass
    has covered the first half of a file is a filter over the first half of the
    file, and showing those rows while saying the count is still growing is
    more use than showing nothing. The two directions are incomplete in
    opposite ways, and the UI says which: an unfinished **include** shows too
    few rows and gains them as it goes, an unfinished **exclude** shows too many
    and loses them.
    """
    started = time.perf_counter()
    sources = list(getattr(session, "sources", []))
    total_rows = sum(int(ss.index.n) for ss in sources)
    masks = tier1_masks(session, filt)
    notes: list[str] = []

    if why := filt.validate():
        return Filtered(filt=filt, masks=[None] * len(sources),
                        hists=[ss.index.hist for ss in sources],
                        kept=total_rows, total=total_rows, notes=(why,),
                        key=filter_key(session, filt),
                        elapsed_ms=(time.perf_counter() - started) * 1000.0)

    state = scanning.ScanProgress(
        total=sum(int(ss.index.indexed_to) for ss in sources)
        * (bool(filt.include) + bool(filt.exclude)))
    last_publish = started

    passes = bool(filt.include) + bool(filt.exclude)
    for i, ss in enumerate(sources) if filt.needs_text else ():
        base = masks[i]
        if base is not None and not base.any():
            state.done += int(ss.index.indexed_to) * passes
            continue
        for text, keep_matching in ((filt.include, True),
                                    (filt.exclude, False)):
            if not text:
                continue
            needle, why = scanning.compile_needle(
                text, ss, regex=filt.regex,
                case_sensitive=filt.case_sensitive)
            if needle is None:
                notes.append(f"{ss.alias}: {why}")
                state.done += int(ss.index.indexed_to)
                # A source whose bytes cannot be searched is not quietly kept:
                # an include it could not be tested against has not been met.
                # The note says why, so this is a stated exclusion and not a
                # silent one.
                if keep_matching:
                    masks[i] = base = np.zeros(int(ss.index.n), dtype=bool)
                continue

            found = np.zeros(int(ss.index.n), dtype=bool)

            # `st` is bound here deliberately: `scan_source` fills in the
            # `ScanProgress` it was handed, so the binding reads live progress
            # rather than a copy.
            def publish(entries, found=found, i=i, base=base,
                        keep=keep_matching, st=state) -> None:
                nonlocal last_publish
                found[entries] = True
                now = time.perf_counter()
                if partial is None or (now - last_publish) * 1000.0 < PUBLISH_MS:
                    return
                last_publish = now
                snapshot = list(masks)
                snapshot[i] = _combine(base, found, keep)
                # No histograms on a partial: rebuilding them is O(entries)
                # per source and this runs every tenth of a second. The strips
                # keep what they have until the pass finishes, which is the
                # same bargain the row count makes.
                partial(_result(session, filt, sources, snapshot, notes,
                                st, started, complete=False, hists=False))

            hit, state = scanning.scan_source(
                needle, ss, only=base, progress=progress,
                cancelled=cancelled, state=state, label=text,
                on_batch=publish)
            found[hit] = True
            masks[i] = base = _combine(base, found, keep_matching)
            if state.cancelled:
                break
        if state.cancelled:
            break

    return _result(session, filt, sources, masks, notes, state, started,
                   complete=not state.cancelled)


def _combine(base: np.ndarray | None, found: np.ndarray,
             keep_matching: bool) -> np.ndarray:
    """Fold a text pass's hits into the mask the cheaper tier left."""
    hit = found if keep_matching else ~found
    return hit if base is None else (base & hit)


def _result(session, filt, sources, masks, notes, state, started, *,
            complete: bool, hists: bool = True) -> Filtered:
    kept = sum(int(m.sum()) if m is not None else int(ss.index.n)
               for ss, m in zip(sources, masks, strict=True))
    return Filtered(
        filt=filt,
        masks=masks,
        hists=(histograms(session, filt, masks) if hists
               else [ss.index.hist for ss in sources]),
        kept=kept,
        total=sum(int(ss.index.n) for ss in sources),
        notes=tuple(notes),
        cancelled=state.cancelled,
        complete=complete,
        bytes_scanned=state.done,
        bytes_total=state.total,
        bytes_skipped=state.skipped,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        key=filter_key(session, filt),
    )
