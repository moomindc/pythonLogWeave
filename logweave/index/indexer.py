"""
The indexing pipeline -- SPEC 5, and the rollover inference of SPEC 4.5.

Reads a source in line-aligned chunks and produces a `FileIndex`. Single
process; the `ProcessPoolExecutor` fan-out and the sidecar cache are M2.

## Rollover inference, and the bug it fixes

Two of the catalogue's formats are incomplete on their own. syslog writes
`Oct  4 14:22:08` with no year. strace writes `14:22:08.113456` with no date at
all. The missing piece has to be inferred, and *which direction you infer from*
decides whether the answer is right.

logmerger anchors from `st_ctime` and works **forwards**. That mis-years every
pre-January line of any file that spans New Year: a file created in December and
still being written in January has its December entries dated with the *new*
year, placing them eleven months into the future and scattering them to the far
end of the merged view.

LogWeave anchors at the **tail**, from `mtime`, and walks **backwards**: the last
entry is the one whose year we can actually pin down, because the file was last
written at `mtime`. Every rollover found walking back decrements the year. This
is SPEC 4.5, and `newyear_syslog.log` and `midnight_strace.log` in the corpus
exist to hold it honest.

A rollover is only counted when the calendar jumps *far* backwards -- six months
for a year boundary, twelve hours for a midnight. Without that guard, two
entries logged a millisecond out of order across a second boundary would read as
a rollover and shift a whole year of entries. That failure is silent, which
makes it risk R4.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..source import encoding
from ..source.base import LogSource
from . import extract as ex
from . import layoutio
from .civil import US_PER_DAY, US_PER_SEC, civil_from_days
from .detect import Detection, detect
from .fileindex import FileIndex, IndexStats, _Growable
from .histogram import Histogram
from .layouts import (
    FLAG_MULTILINE,
    FLAG_SCALAR_FALLBACK,
    FLAG_SEVERITY_MASK,
    FLAG_TS_INFERRED,
)
from .lines import detect_lone_cr, last_complete_line_end, split_lines
from .severity import DEFAULT_RULESET, SeverityRuleset, classify

CHUNK_BYTES = 64 * 1024 * 1024
"""
SPEC 5.1. Large enough that per-chunk overhead vanishes, small enough that the
working set of a chunk's temporaries stays well inside RAM: the kernel holds a
dozen or so `n`-length arrays at once, so a 64 MB chunk of 100-byte lines costs
roughly 60 MB of scratch.
"""

DETECT_BYTES = 1 * 1024 * 1024
"""How much is read for format detection before the main pass begins."""

YEAR_ROLLOVER_MONTHS = 6
"""Months the calendar must jump back before a year rollover is believed."""

DAY_ROLLOVER_US = 12 * 3600 * US_PER_SEC
"""How far the clock must jump back before a midnight rollover is believed."""


@dataclass
class _Carry:
    """State that crosses a chunk boundary."""

    tail: bytes = b""
    tail_off: int = 0
    line_base: int = 0
    open_entry: bool = False
    """True if the last chunk ended inside a multi-line entry."""


# ---------------------------------------------------------------------------
# rollover inference (SPEC 4.5)
# ---------------------------------------------------------------------------

MESSAGE_OFFSET_SLACK = 4
"""
How far into a line a timestamp may start and still count as leading it.

Covers a level prefix or a stray marker before the stamp. Beyond this the text
in front is content, not punctuation, and trimming it would throw information
away rather than remove a repetition.
"""


def message_offset(detection) -> int:
    """
    Where the message begins, for a format whose timestamp leads the line.

    The view shows the parsed timestamp in its own column, so for a format like
    `iso_space_dot` the same timestamp would otherwise appear twice on the row.
    This says how much of the line the timestamp accounts for, and the view
    trims that much when it is showing the column (SPEC 9.5).

    Returns 0 when there is nothing to trim -- no format, or an anchored format
    like an Apache access log where the timestamp sits inside the line and the
    text in front of it is the client address, which is not a repetition of
    anything.
    """
    layout = getattr(detection, "layout", None)
    if layout is None or layout.is_anchored:
        return 0
    if layout.ts_offset > MESSAGE_OFFSET_SLACK:
        return 0
    return int(layout.ts_offset + layout.ts_width)


def infer_years(
    month: np.ndarray, day: np.ndarray, anchor_wall_us: int
) -> tuple[np.ndarray, int]:
    """
    Assign a year to every entry of a yearless format, anchored at the tail.

    `month` and `day` are in file order. `anchor_wall_us` is the file's mtime, naive local. Returns
    the per-entry year and the number of rollovers found.
    """
    n = month.size
    if n == 0:
        return np.empty(0, dtype=np.int64), 0

    # Going forward through the file, the calendar normally advances. A jump
    # backwards of at least six months is a New Year; anything smaller is clock
    # jitter or out-of-order logging and must not move the year.
    back = np.zeros(n, dtype=bool)
    back[1:] = (month[1:] < month[:-1]) & (month[:-1] - month[1:] >= YEAR_ROLLOVER_MONTHS)
    rel = np.cumsum(back)

    anchor_days = anchor_wall_us // US_PER_DAY
    ay, am, ad = (int(v[0]) for v in civil_from_days(np.array([anchor_days], np.int64)))

    # The last entry cannot postdate the last write. If its calendar position is
    # later in the year than mtime's, the file must have rolled over since.
    last_md = int(month[-1]) * 100 + int(day[-1])
    anchor_year = ay - 1 if last_md > am * 100 + ad else ay

    years = anchor_year - int(rel[-1]) + rel
    return years.astype(np.int64), int(rel[-1])


def infer_dates(sod_us: np.ndarray, anchor_wall_us: int) -> tuple[np.ndarray, int]:
    """
    Assign a date to every entry of a dateless format, anchored at the tail.

    Same shape of argument as `infer_years`, one level down: a backwards jump of
    at least twelve hours is midnight, anything smaller is jitter.
    """
    n = sod_us.size
    if n == 0:
        return np.empty(0, dtype=np.int64), 0

    back = np.zeros(n, dtype=bool)
    back[1:] = (sod_us[:-1] - sod_us[1:]) >= DAY_ROLLOVER_US
    rel = np.cumsum(back)

    anchor_days = int(anchor_wall_us // US_PER_DAY)
    anchor_sod = int(anchor_wall_us % US_PER_DAY)
    if int(sod_us[-1]) > anchor_sod:
        # The last entry's time of day is after mtime's, so it belongs to the
        # day before.
        anchor_days -= 1

    days = anchor_days - int(rel[-1]) + rel
    return days.astype(np.int64), int(rel[-1])


# ---------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------

def build_index(
    source: LogSource,
    *,
    mtime_wall_us: int,
    chunk_bytes: int = CHUNK_BYTES,
    detection: Detection | None = None,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
    start_at: int | None = None,
    limit: int | None = None,
    first_line: int = 1,
) -> FileIndex:
    """
    Index a source from `start_at` to its current extent.

    `mtime_wall_us` anchors the yearless and dateless formats (SPEC 4.5) and is
    taken from the source rather than guessed, because it is the only evidence
    of when the last line was written. It must be **naive local** wall time per
    D1, not a UTC epoch: pass `civil.epoch_us_to_wall_us(st_mtime * 1e6)`. The
    distinction is worth the awkward name -- getting it wrong shifts a file by
    the local UTC offset, which usually looks fine and mis-years a syslog across
    New Year.

    `start_at` defaults to the source's `data_start()`, and is passed explicitly
    only by a follow append, which resumes from `FileIndex.indexed_to`.

    `detection` may be supplied to skip re-detection -- which is what a follow
    append does, since a file does not change format mid-stream.

    `first_line` is the 1-based line number of the first line at `start_at`.
    It only matters when resuming: `line_no` is a position within the whole
    file (D9), so an append that restarted the count at 1 would attribute a
    copied row to the wrong line of the file, which is the one thing
    attribution must never do.
    """
    # A source with a byte-order mark reports content starting past it, so the
    # mark is never indexed as part of the first entry (see LogSource.data_start).
    if start_at is None:
        start_at = source.data_start()
    end = source.size() if limit is None else min(source.size(), start_at + limit)
    idx = FileIndex(
        source_id=str(source.id),
        stats=IndexStats(),
    )

    enc = resolve_encoding(source, start_at)
    idx.encoding = enc.name
    if detection is None:
        detection = _detect_from_head(source, start_at, end, enc, ruleset=ruleset)
    idx.format_name = detection.fmt.name if detection.fmt else ""
    idx.fast_path = detection.fast_path
    idx.confidence = detection.confidence
    idx.stats.notes = list(detection.notes)
    idx.stats.used_fast_path = detection.fast_path
    idx.message_offset = message_offset(detection)
    idx.severity_offset = detection.severity_offset
    if detection.layout is not None:
        idx.layout_json = layoutio.to_json(detection.layout)

    if not detection.ok:
        # FR-19: a file with no recognisable format is still listed, still
        # readable, and still scrollable -- it simply has no timestamps, so it
        # cannot be interleaved. Refusing to open it would be worse.
        idx.off = np.array([start_at], dtype=np.uint64)
        return idx

    offs = _Growable(np.uint64)
    tss = _Growable(np.int64)
    flags = _Growable(np.uint8)
    lnos = _Growable(np.uint32)
    # Components for the formats whose timestamps cannot be completed per chunk;
    # the inference is global to the file, so it runs once after the pass.
    needs_parts = detection.fmt.infer_year or detection.fmt.infer_date
    months = _Growable(np.int64) if detection.fmt.infer_year else None
    days = _Growable(np.int64) if detection.fmt.infer_year else None
    sods = _Growable(np.int64) if needs_parts else None

    carry = _Carry(tail_off=start_at, line_base=max(0, first_line - 1))
    lone_cr: bool | None = None
    pos = start_at
    stride = enc.terminator_width

    while pos < end:
        raw = source.read_range(pos, min(pos + chunk_bytes, end))
        if not raw:
            break
        pos += len(raw)
        file_chunk = carry.tail + raw
        chunk_off = carry.tail_off

        # Everything below this line works in *scan space*: for a single-byte
        # encoding that is the file's own bytes, and for UTF-16 it is the chunk
        # with the NUL half of each code unit dropped, so the fixed-offset
        # kernels of SPEC 4.4 see the single-byte form they were written for.
        # SPEC 3.3's rule is to narrow the buffer being scanned, not to
        # transcode the file -- so every offset recorded in the index is still a
        # true file offset, which `_to_file` is responsible for.
        phase = encoding.scan_phase(enc, byte_start=chunk_off)
        chunk = encoding.narrow_for_scan(file_chunk, enc, byte_start=chunk_off)
        buf = np.frombuffer(chunk, dtype=np.uint8)
        if lone_cr is None:
            lone_cr = detect_lone_cr(buf)

        def _to_file(scan_pos, phase=phase, chunk_off=chunk_off):
            """Scan-space position -> file byte offset."""
            return chunk_off + phase + stride * scan_pos

        # Never split a line across chunks: trim to the last terminator and
        # carry the remainder. At end of file the remainder is a partial line
        # and is deliberately left unindexed (SPEC 7.3). The cut is found in
        # scan space and mapped back, because the carry must be real file bytes.
        if pos < end:
            cut = last_complete_line_end(buf, lone_cr)
            if cut == 0:
                # No terminator in a whole chunk -- a single enormous line. Keep
                # accumulating rather than spinning.
                carry.tail = file_chunk
                continue
            cut_bytes = phase + stride * cut
            carry.tail = file_chunk[cut_bytes:]
            carry.tail_off = chunk_off + cut_bytes
            chunk = chunk[:cut]
            buf = buf[:cut]
        else:
            carry.tail = b""

        starts, lens = split_lines(buf, lone_cr)
        if starts.size == 0:
            continue

        res = (
            ex.extract(buf, starts, lens, detection.layout)
            if detection.layout is not None
            else ex.extract_scalar(chunk, starts, lens, detection.fmt, detection.pattern)
        )

        sev = classify(chunk, buf, starts, lens, detection.severity_offset, ruleset=ruleset)

        opens = np.flatnonzero(res.valid)
        idx.stats.lines += int(starts.size)
        idx.stats.chunks += 1

        if opens.size == 0:
            # Every line is a continuation. They extend whichever entry is open;
            # if none is, they are unparsed preamble.
            if not carry.open_entry:
                idx.stats.unparsed_lines += int(starts.size)
            carry.line_base += int(starts.size)
            continue

        leading = int(opens[0])
        if leading and not carry.open_entry:
            idx.stats.unparsed_lines += leading

        entry_off = _to_file(starts[opens]).astype(np.uint64)
        entry_lno = (opens + carry.line_base + 1).astype(np.uint32)

        # An entry is multi-line if the next entry does not start on the very
        # next line. The last entry of a chunk is resolved by the next chunk, so
        # its span is provisional; `_finish` fixes the final one.
        spans = np.empty(opens.size, dtype=np.int64)
        spans[:-1] = np.diff(opens)
        spans[-1] = int(starts.size) - int(opens[-1])

        f = sev[opens].astype(np.uint8)
        f |= np.where(spans > 1, np.uint8(FLAG_MULTILINE), np.uint8(0)).astype(np.uint8)
        if detection.layout is None:
            f |= np.uint8(FLAG_SCALAR_FALLBACK)
        if detection.fmt.infer_year or detection.fmt.infer_date:
            f |= np.uint8(FLAG_TS_INFERRED)

        offs.extend(entry_off)
        lnos.extend(entry_lno)
        flags.extend(f)
        tss.extend(res.ts[opens])
        if sods is not None:
            # A yearless or dateless format leaves `ts` meaningless, so the
            # components are carried through instead and completed once the
            # whole file has been seen.
            assert res.parts is not None and res.parts.sod_us is not None
            sods.extend(res.parts.sod_us[opens])
            if months is not None:
                assert res.parts.month is not None and res.parts.day is not None
                months.extend(res.parts.month[opens])
                days.extend(res.parts.day[opens])

        carry.open_entry = True
        carry.line_base += int(starts.size)

    _finish(idx, offs, tss, flags, lnos, months, days, sods, detection,
            mtime_wall_us=mtime_wall_us, end=end)
    return idx


def resolve_encoding(source: LogSource, start_at: int) -> encoding.EncodingInfo:
    """
    The encoding the scan should assume.

    A source that resolved its own encoding is believed -- that is where a user
    override arrives (FR-5). Otherwise it is detected from the head bytes rather
    than taken from `meta.encoding`, which is a display label and is left at its
    default by simple sources.
    """
    info = getattr(source, "encoding_info", None)
    if isinstance(info, encoding.EncodingInfo):
        return info
    return encoding.detect(source.read_range(start_at, start_at + encoding.SCAN_BYTES))


def _detect_from_head(
    source: LogSource,
    start_at: int,
    end: int,
    enc: encoding.EncodingInfo,
    *,
    ruleset: SeverityRuleset,
) -> Detection:
    raw = source.read_range(start_at, min(start_at + DETECT_BYTES, end))
    # Detection must see exactly what extraction will see, or a layout
    # synthesised here would describe byte positions the kernel never visits.
    head = encoding.narrow_for_scan(raw, enc, byte_start=start_at)
    buf = np.frombuffer(head, dtype=np.uint8)
    starts, lens = split_lines(buf)
    return detect(head, buf, starts, lens, ruleset=ruleset)


def _finish(
    idx: FileIndex,
    offs: _Growable,
    tss: _Growable,
    flags: _Growable,
    lnos: _Growable,
    months: _Growable | None,
    days: _Growable | None,
    sods: _Growable | None,
    detection: Detection,
    *,
    mtime_wall_us: int,
    end: int,
) -> None:
    """Resolve inference, seal `off`, and check monotonicity."""
    n = len(tss)
    idx.flags = flags.view.copy()
    idx.line_no = lnos.view.copy()
    idx.stats.entries = n
    idx.stats.bytes_indexed = max(0, end - int(offs.view[0])) if n else 0
    idx.stats.multiline_entries = int((idx.flags & FLAG_MULTILINE).astype(bool).sum())

    # `off` has n+1 slots: entry i's text runs to the start of entry i+1, and
    # the last entry runs to the end of the indexed region (SPEC 2.3).
    off = np.empty(n + 1, dtype=np.uint64)
    off[:n] = offs.view
    off[n] = np.uint64(end)
    idx.off = off

    ts = tss.view.copy()

    if detection.fmt is not None and detection.fmt.infer_year:
        assert months is not None and days is not None and sods is not None
        years, rolls = infer_years(months.view, days.view, mtime_wall_us)
        ts = ex.combine_civil(years, months.view, days.view, sods.view)
        idx.stats.year_rollovers = rolls
        idx.stats.notes.append(f"year inferred from mtime, {rolls} rollover(s) walked back")
    elif detection.fmt is not None and detection.fmt.infer_date:
        assert sods is not None
        day_nums, rolls = infer_dates(sods.view, mtime_wall_us)
        ts = day_nums * US_PER_DAY + sods.view
        idx.stats.day_rollovers = rolls
        idx.stats.notes.append(f"date inferred from mtime, {rolls} rollover(s) walked back")

    idx.ts = ts
    idx.stats.inferred = n if (
        detection.fmt is not None
        and (detection.fmt.infer_year or detection.fmt.infer_date)
    ) else 0

    # SPEC 4.6: most logs are monotonic, and when they are the merge engine can
    # binary-search `ts` directly. `order` is built only when it is needed,
    # because it is 8 bytes an entry -- a third of the whole index.
    if n > 1 and bool((np.diff(ts) < 0).any()):
        idx.order = np.argsort(ts, kind="stable").astype(np.uint64)
        idx.stats.monotonic = False
        idx.stats.notes.append("file is not time-ordered; a sort permutation was built")
    else:
        idx.stats.monotonic = True

    # The overview histogram (SPEC 8.1), built here and not per chunk.
    #
    # SPEC 8.1 describes it as folded into the chunk loop, and that is not
    # possible: the bucket width is derived from the file's whole time span and
    # `t0` from its first entry, neither of which is known until the last chunk
    # has been read -- and for a yearless or dateless format (SPEC 4.5) the
    # timestamps themselves do not exist in final form until the inference
    # above has run. Bucketing per chunk against a guessed grid would mean
    # rebucketing whenever the guess was wrong, which is this loop again.
    #
    # The cost of doing it here is one vectorised pass over `ts` and
    # `severity`, both of which are already in cache from the lines above:
    # measured 50.8 M entries/s at M0, so ~2% of the indexing budget for the
    # file it has just read at 128 MB/s.
    if n:
        idx.hist = Histogram.build(ts, idx.flags & FLAG_SEVERITY_MASK)
