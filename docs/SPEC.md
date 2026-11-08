# LogWeave — Technical Specification

| | |
|---|---|
| **Status** | Draft 1 — for review |
| **Date** | 2026-11-07 |
| **Companion** | `docs/PRD.md` (requirement IDs FR-*, NFR-* refer to it) |
| **Target** | Python 3.12+ (dev on 3.14.5), PySide6 6.12+, NumPy 2.x |

---

## 1. Architecture

Five layers, each depending only on those below it. Only the top layer imports Qt;
everything below is headless and unit-testable without a display.

```
┌───────────────────────────────────────────────────────────────────────┐
│ 5. UI (PySide6)                                                       │
│    MainWindow · MergedLogView · MinimapStrip · TimelineStrip           │
│    SourcePanel · FindBar · FilterBar · HighlightEditor · ProfileDialog │
├───────────────────────────────────────────────────────────────────────┤
│ 4. Session                                                            │
│    Session (the open set of sources) · Profile load/save              │
│    FilterSet · SearchJob · Bookmarks · Clipboard/Export               │
├───────────────────────────────────────────────────────────────────────┤
│ 3. Merge                                                              │
│    MergeIndex (checkpointed k-way merge) · BlockCache                 │
│    Overview (severity/density histogram aggregation)                  │
├───────────────────────────────────────────────────────────────────────┤
│ 2. Index                                                              │
│    Indexer (chunked, parallel) · FileIndex (memmapped arrays)         │
│    SidecarCache · FormatDetector · TimestampExtractor · SeverityScan  │
├───────────────────────────────────────────────────────────────────────┤
│ 1. Source                                                             │
│    LogSource (ABC) · FileSource · GzipSource · CsvSource · JsonlSource │
│    SshFileSource · Watcher (local / polling) · encoding detection     │
│    — phase 2: EvtxSource, EventChannelSource; phase 3: SyslogSource   │
└───────────────────────────────────────────────────────────────────────┘
```

**Hard rule.** Layers 1–4 must have no Qt import. The UI observes them through
plain callbacks and a small signal adapter. This keeps the whole engine testable
headlessly on Linux CI (NFR-18) and keeps Qt out of worker processes.

### 1.1 Repository layout

The plan, with **what exists after M4 marked**. Unmarked entries are still
designed and unbuilt; nothing has been built that is not in this list.

```
logweave/
  __main__.py            CLI launcher: logweave [files...] [--profile P]        M3
  source/                layer 1
    base.py              LogSource ABC, SourceId, SourceMeta, SourceIdentity   M1
    file.py                                                                    M1
    gzipped.py           .gz decompressed once into the cache (D12)            M1
    csv.py  jsonl.py  ssh.py
    watcher.py           LocalWatcher (ReadDirectoryChangesW) / PollWatcher
    winfile.py           CreateFileW wrapper with FILE_SHARE_DELETE (§7.1)      M1
    encoding.py                                                                M1
  index/                 layer 2
    layouts.py           the 15-format catalogue and the layout model          M1
    detect.py            FormatDetector, TimestampFormat registry              M1
    extract.py           vectorised + scalar timestamp extractors              M1
    civil.py             calendar arithmetic, local-zone conversion (§2.2)      M1
    severity.py          vectorised severity scan                              M1
    lines.py             line splitting and entry grouping                     M1
    indexer.py           chunked parallel index build                          M1
    fileindex.py         FileIndex: the memmapped arrays                       M1
    sidecar.py           on-disk cache format, validation, LRU                 M2b
    cached.py            hit / append / miss (§5.4)                            M2b
    layoutio.py          the proven layout, serialised so an append reuses it  M2b
  merge/                 layer 3
    mergeindex.py        checkpoints, row_at, time_to_row, row_to_time         M2a
    blockcache.py        -- built inside mergeindex.py, not split out          M2a
    overview.py          strip aggregation
  session/               layer 4
    session.py                                                                 M3
    prepare.py           cache warming in worker processes (§5.3)              M2b
    rowformat.py         copy and export formatting, attribution (§11.3)       M3
    palette.py           severity ramp and source-lane colours                 M3
    aliasing.py          shortest distinguishing source names (FR-29)          M4
    profile.py  filters.py  search.py  bookmarks.py  export.py
  highlight/             layer 5 by number, Qt-free by rule (§1)
    rules.py             HighlightRule, Ruleset, one-pass compilation          M4
    builtins.py          the three shipped rulesets (§10.1)                    M4
  ui/                    layer 5
    app.py               QApplication entry point                              M3
    mainwindow.py        drag-and-drop, source dock, menus, status             M3
    mergedview.py        the merged view -- `logview.py` in the original plan   M3
    indexing.py          the worker thread that keeps the window answering     M2b
    settings.py          view preferences, remembered between runs (FR-38)     M4
    theme.py             fonts, chrome colours, stylesheet                     M3
    minimap.py  timeline.py  sourcepanel.py  findbar.py  filterbar.py
    dialogs.py  styles.py
tests/
  conftest.py            corpus fixture and an in-memory LogSource stub        M0
  test_architecture.py   §1: the engine layers stay Qt-free                    M0
  test_corpus.py  test_prototypes.py                                           M0
  unit/                  the shipped engine modules, against ground truth
  ui/                    the Qt shell; skipped entirely when Qt is absent      M3
  integration/  perf/
tools/
  gen_corpus.py          corpus and ground-truth generator                     M0
  bench.py               engine benchmarks and the regression gate             M0
  bench_ui.py            the paint budget; the one tool that needs Qt          M3
  make_test_logs.py      realistic logs to try the GUI on                      M3
  _prototypes.py         M0 kernels, kept as reference rows in the gate        M0
  rotate_sim.py
docs/
  PRD.md  SPEC.md  ADR/
```

Two deviations from the plan above, both deliberate:

- **`logview.py` is `mergedview.py`.** The name says what it shows rather than
  what it is, and there is no other log view to distinguish it from.
- **`blockcache.py` was not split out.** The cache is forty lines and exists
  only to serve `MergeIndex`; a module boundary there would be ceremony.

---

## 2. Core data model

### 2.1 Entry

The unit of the merged view is an **entry**: one timestamped line plus any
immediately following lines that carry no timestamp (FR-15). A stack trace is one
entry, so it never gets torn apart by the interleave.

An entry is never materialised as an object in bulk. It is addressed by
`(source_id, entry_index)` and its text is read on demand from the source's bytes.

### 2.2 Time representation

Per D1 (naive local time), time is a single **`int64` count of microseconds since
1970-01-01T00:00:00, interpreted as a wall clock** — no zone, no DST. Called
`wall_us` throughout.

- A zone-aware timestamp in a log is converted to machine-local and the zone is
  then discarded, matching logmerger's behaviour.
- A naive timestamp is used exactly as written.
- `wall_us` is computed by integer arithmetic, never by constructing a `datetime`:
  `days_from_civil(y, m, d)` (Hinnant's algorithm, branch-free, vectorisable) then
  `wall_us = (days*86400 + hh*3600 + mm*60 + ss) * 1_000_000 + us`.
- Microsecond resolution. Formats with nanosecond digits truncate (documented).
- Sentinel `TS_NONE = INT64_MIN` for "no timestamp established".

> **Consequence to keep documented in the UI.** Two sources in different
> timezones interleave wrongly, and ordering inside a DST fall-back hour is
> undefined. The per-file skew offset (D2, §6.3) is the remedy.

### 2.3 FileIndex — the per-source arrays

One `FileIndex` per source. All arrays are NumPy, all memory-mapped from the
sidecar cache (§5.4), so resident memory stays bounded (NFR-7) and the OS page
cache does the work.

| Array | dtype | Bytes/entry | Meaning |
|---|---|---|---|
| `off` | `uint64[n+1]` | 8 | Byte offset of each entry's start; `off[n]` = end of indexed region. Entry length = `off[i+1] - off[i]`. |
| `ts` | `int64[n]` | 8 | `wall_us`, before the skew offset is applied. |
| `flags` | `uint8[n]` | 1 | bits 0–2: severity class (§10.2). bit 3: entry is multi-line. bit 4: timestamp was inferred, not read. bit 5: fast-path parse failed, scalar fallback used. |
| `line_no` | `uint32[n]` | 4 | **1-based line position of the entry within its own source file.** Required by the copy/export attribution in D9 (`app.log:1`, `SOA.log:7`). Not derivable from `entry_index`, because a multi-line entry consumes several lines. |
| `order` | `uint64[n]` | 8 *(only if needed)* | Stable argsort of `ts`. **Omitted** when `ts` is already non-decreasing, which is the overwhelmingly common case. Monotonicity is checked once at build time with `np.all(np.diff(ts) >= 0)`. |

**21 bytes per entry** in the normal monotonic case. A 1GB log with 10M entries
costs a 210MB sidecar — consistent with PRD Q8, which was accepted at this cost.

`line_no` is `uint32`, so line numbers are exact up to 4.29×10⁹ lines per file.
Beyond that — reachable only by a 64GB file of very short lines (NFR-13) — the
array saturates and the UI reports line numbers as unknown for the overflow
region rather than showing wrong ones. `uint64` was rejected as a 4-byte-per-entry
cost for a case that does not occur in practice.

Plus, per source, a fixed-size **density histogram** (§8.1) and scalar metadata
(path, size, file id, detected format, encoding, confidence, alias, colour, skew).

### 2.4 Why nothing larger is materialised

A merged set of 100M rows would need 800MB just to store the merge order as
`(file_id, entry_index)` pairs. §6 avoids that entirely with a checkpointed merge
whose index is ~2MB.

---

## 3. Source layer

### 3.1 The `LogSource` interface

This is the abstraction D3 and D6 depend on: Event Log and syslog become new
implementations, not new plumbing.

```python
class LogSource(ABC):
    id: SourceId
    meta: SourceMeta            # display name, alias, colour, origin string

    @abstractmethod
    def open(self) -> None: ...
    @abstractmethod
    def size(self) -> int:
        """Current extent in addressable units (bytes for files)."""
    @abstractmethod
    def read_range(self, start: int, end: int) -> bytes:
        """Random-access read. Must be cheap and repeatable."""
    @abstractmethod
    def watch(self, on_change: Callable[[ChangeEvent], None]) -> Watcher | None:
        """None if the source cannot grow."""
    @abstractmethod
    def identity(self) -> SourceIdentity:
        """Stable identity for rotation/truncation detection (§7.2)."""
    def close(self) -> None: ...
```

Everything above layer 1 sees only this. The indexer requires exactly two things:
random-access byte reads, and a growing extent. A record-oriented source (Event
Log, syslog) satisfies this by serialising its records into a local spill file and
exposing *that* — so one indexing path serves every source type. This is the key
design choice that makes phases 2 and 3 cheap.

### 3.2 v1 implementations

| Source | Notes |
|---|---|
| `FileSource` | Local and UNC paths. Opened via `winfile.py` on Windows (§7.1). `mmap` for local files; buffered `pread`-style reads for UNC, where mmap over SMB is unreliable. |
| `GzipSource` | **D12**: decompressed once into the cache directory, then indexed as a plain file, giving full random access. Checks free disk against the gzip ISIZE estimate before starting and refuses with a clear message if short. The decompressed copy is cache-managed under the same LRU as the sidecars (§5.4). |
| `CsvSource` | Timestamp taken from a chosen column (default: first). Implemented as a *projection*: the index records the byte range of each row, and the extractor is told where within the row the timestamp sits. Quoted newlines are handled by the detector, which falls back to a scalar `csv`-module pass if any quoting is found. Per **D17** the row is displayed verbatim; a header row, if detected, is excluded from the entries and offered as a tooltip over the message column so the field order is still discoverable. |
| `JsonlSource` | Timestamp from a chosen key path (default: first key matching `time`/`timestamp`/`ts`/`@timestamp`). One JSON object per line; each line is one entry. Per **D17** the message column shows the raw JSON verbatim — no field extraction, no JSON parse on the display path at all, which also keeps painting fast. Timestamp extraction does not need a full JSON parse either: the detector locates the value's byte offset within the line, and if that offset is stable across the sample the normal fixed-position vectorised path (§4.4) applies unchanged. |
| `SshFileSource` | **S priority (FR-7).** SFTP random reads via Paramiko; follow by polling `stat`. Reads are high-latency, so it keeps a large read-ahead buffer and, for files above a size threshold, offers to cache the file locally and index that instead. **Auth (D14, FR-7a):** key file, SSH agent (Pageant / OpenSSH agent), and interactive password, tried in that order. Host keys are verified against `known_hosts` with an explicit trust-on-first-use prompt. |

### 3.3 Encoding (FR-5, FR-6)

Detection order: BOM → a UTF-8 validity scan of the first 256KB → fall back to the
system ANSI codepage. UTF-16 is detected by BOM or by a high density of NUL bytes
at alternating positions.

**The index stores byte offsets, always.** Decoding happens only for the handful of
entries being painted, with `errors="replace"`. This means an encoding error can
never corrupt the index or the ordering, and changing the encoding override is
instant — no re-index. UTF-16 sources set a flag so the line splitter looks for
2-byte line terminators.

Line terminators: the splitter scans for `\n` and treats a preceding `\r` as part
of the terminator; a lone `\r` is handled by a secondary scan only when the file
contains `\r` not followed by `\n` (detected in the first chunk).

---

## 4. Timestamp format detection

This is where logmerger is strongest, and where we need to extend it.

### 4.1 What is reused from logmerger

logmerger's `timestamp_wrapper.py` declares ~20 format classes, each with a regex
and a `strptime` format. **The format catalogue is excellent and is adopted
wholesale** (with attribution, PRD Q9), covering:

`YYYY-MM-DD HH:MM:SS` with `,`/`.` fractions and optional zone · the same with a
`T` separator · syslog `Mon DD HH:MM:SS` · `DD/Mon/YYYY HH:MM:SS` · Apache access
`DD/Mon/YYYY:HH:MM:SS ±ZZZZ` · Apache error `[Day Mon DD HH:MM:SS.ffffff YYYY]` ·
strace `HH:MM:SS.ffffff` · strace with leading PID · epoch seconds / milliseconds /
float seconds · Python `http.server`.

### 4.2 What must change, and why

| logmerger behaviour | Problem | LogWeave behaviour |
|---|---|---|
| Detects from **the first line only** | A banner, a blank line, or a partial first line picks the wrong format or none at all. This is risk R4. | Sample **up to 200 lines** from the head, plus 50 from the middle and 50 from the tail. Score every candidate format by match rate. |
| First matching subclass in declaration order wins | `SecondsSinceEpoch` (`\d{10}`) can shadow `MilliSecondsSinceEpoch` (`\d{13}`); ordering is implicit and fragile. | Formats carry an explicit integer **specificity**. Candidates are ranked by `(match_rate, specificity, anchoring)`. Ties are a detection warning surfaced in the UI. |
| Yearless formats take the year from `st_ctime`, **forward** | On Linux `st_ctime` is the inode change time, not creation. More importantly, a file written over New Year gets every pre-January entry stamped with the wrong year. | **Anchor at the tail.** Assume the last entry is at or just before `mtime`, then walk *backwards*, decrementing the year at each backwards discontinuity (§4.5). |
| No confidence reporting | A silently wrong format silently mis-orders a file. | Report match rate and confidence per source in the UI; warn below 90%; always offer a one-click override (FR-13). |
| Custom formats via a `(...)` regex template | Powerful but obscure. | Keep the template mechanism verbatim for power users, and add a UI: paste a sample line, pick the timestamp span by selection, and the format is generated. |
| `strptime` per line | ~3–8µs per line; at 10M lines that alone blows NFR-3. | A vectorised extractor (§4.4). `strptime` is never on the hot path. |

### 4.3 Detection algorithm

1. Read the first 256KB, plus 64KB at 50% and 64KB at the tail.
2. Split into lines, discard the first and last (possibly partial).
3. For each candidate format, run its regex against each sampled line; record
   match count and, for matches, the byte offset and length of the timestamp.
4. Reject any candidate whose matches produce timestamps outside
   `[1990-01-01, now + 1 year]` — this kills most spurious numeric matches.
5. Rank by match rate, then specificity, then "anchored at offset 0" (preferred).
6. **Synthesise a layout** by measuring, across the sampled matches, the offset
   and width of every named group. A format's regex says what *may* appear; the
   file says what *does*. `\d{1,9}` fractional digits means the field width is
   not knowable from the pattern, so it is measured rather than assumed, and a
   layout is produced only when the measurements agree. One generic kernel
   (§4.4) is then driven by that layout, so adding a format costs a regex and
   no new kernel. Specifics:
   - The timestamp offset is taken as the **mode**, not by unanimity, requiring
     ≥90% agreement. A stray byte-order mark shifts the first line of a file and
     nothing else; demoting a 2 GB log to the scalar path over one line would be
     absurd. Step 6b makes this safe.
   - A format declaring an `anchor_byte` may instead be located from the first
     occurrence of that byte in each line, which keeps variable-prefix formats
     (Apache and IIS access logs, whose timestamps follow a client address) on
     the vectorised path. Finding every such byte in a chunk at once and
     reducing to one per line is two sorted-array operations, not *n* searches.
   - Every byte inside the timestamp that no group claims is recorded as a
     separator **only if it is constant across the sample**. This makes the
     optional parts of the patterns (`\s?`, `:?`) self-resolving.
   - Any inconsistency means no layout, and the file takes the scalar path:
     ~20× slower, never wrong.
6b. **Validate the layout against the scalar regex path on the sample, and
   discard it on any disagreement.** This is the direct mitigation for risk R4 —
   a format that is silently wrong, parses cleanly, produces plausible
   timestamps, and mis-orders a whole file. A test suite catches that only for
   formats someone thought to test. Three things are checked: the fast path must
   not claim a row the regex rejects; where both produce a timestamp the two
   must be **equal**; and the fast path must not lose meaningfully more rows
   (tolerance 2%). The cost is a few hundred regex searches per file, once. A
   clever kernel nobody checks is a liability, so the check lives in the engine
   and not only in the tests.
7. Record whether the **severity word** also sits at a constant offset, by
   searching each sampled line for a level word and taking the modal offset. If
   ≥90% of the lines that have a level agree on one offset, that offset unlocks
   the vectorised severity path (§10.3); otherwise the source falls back to the
   regex scan, which is 40× slower and does not meet NFR-3. This step was added
   after M0 measured the cost of getting it wrong.
8. If the best match rate is below a floor (default 25%), the file has no
   recognised format → FR-19 (show as timestamp-less, assign manually, or skip).

### 4.4 Extraction — the performance core

This is the mechanism that makes NFR-3 (≥100 MB/s) reachable in pure Python. Risk
R1 is retired here, or not at all.

**Vectorised path** (fixed-position formats — the common case):

```python
chunk        = source.read_range(a, b)                 # ~64MB
buf          = np.frombuffer(chunk, np.uint8)
nl           = np.flatnonzero(buf == 0x0A)             # C-speed line split
starts, lens = ...                                     # from nl; CR stripped

long_enough  = lens >= W                               # see note 1
base         = np.where(long_enough, starts, 0)
idx          = np.empty(n, np.int64)                   # ONE reused scratch index

def col(j):                                            # see note 2
    np.add(base, j, out=idx)
    return buf[idx]

valid = long_enough.copy()
for pos, ch in SEPARATORS:
    valid &= col(pos) == ch
for pos in DIGIT_POSITIONS:                            # see note 3
    v = col(pos) - np.uint8(48)                        # wraps below '0'
    valid &= v <= np.uint8(9)
    d[pos] = v

pair  = lambda a, b: d[a] * np.uint8(10) + d[b]        # see note 4
year  = pair(0, 1).astype(np.int32) * 100 + pair(2, 3)
month, day, hh, mm, ss = pair(5,6), pair(8,9), pair(11,12), pair(14,15), pair(17,18)
us    = (pair(20,21).astype(np.int32) * 100 + pair(22,23)) * 100 + pair(24,25)
sod   = hh * 3600 + mm * 60 + ss                       # second of day

days  = scalar_day_number if one_date_in_chunk else days_from_civil(...)  # note 5
ts    = (days * 86400 + sod) * 1_000_000 + us
```

**Five details that are the difference between 97 and 149 MB/s**, all established
by the M0 benchmark rather than reasoned about:

1. **The length guard removes the need to clip.** Every line in `starts` is
   terminated, so a line whose length clears `W` is guaranteed in range. Short
   lines are redirected to offset 0, read garbage, and are excluded anyway. A
   `np.clip` per column cost ~25% of the kernel.
2. **Gather one column at a time into a reused index.** The obvious `(n, W)` 2D
   form needs a 208MB `int64` index at 1M lines, which makes the kernel
   memory-bound.
3. **One unsigned comparison per digit, not two.** For a byte below `'0'` the
   subtraction wraps to ≥246, so the upper bound alone rejects it — and the
   subtracted value is then reused as the digit, saving 20 further passes.
4. **Combine digit pairs in 1-byte lanes** before widening; two digits fit a
   `uint8` (≤99).
5. **Constant-date fast path.** A 64MB chunk of one log almost always covers a
   single calendar day, so `days_from_civil` runs once as a scalar instead of per
   line — it was 75ms of a 511ms kernel. Date constancy is tested over *valid*
   lines only, because continuation lines hold arbitrary bytes. A chunk crossing
   midnight falls back to the vector form, which is still exact.

No Python-level loop over lines, no regex, no `datetime`. Every operation is a
NumPy kernel over the whole chunk. Entries then fall out directly:

```
entry_lines  = np.flatnonzero(valid)   # <- line ordinal within the chunk
entry_starts = starts[entry_lines]     # lines with a timestamp start an entry
entry_ts     = ts[entry_lines]         # continuation lines are the gaps between them
entry_lineno = entry_lines + chunk_base_line + 1      # 1-based, file-wide
```

— which gives FR-15 (multi-line attachment) for free, vectorised, with no special
case. `off` is `entry_starts` plus the chunk base; the last entry of a chunk is
closed by the first entry of the next.

**`line_no` costs nothing to compute.** `np.flatnonzero(valid)` *is* the line
ordinal of each entry within the chunk — the same array already needed to select
entry starts. Adding the chunk's base line number and 1 makes it the file-wide
1-based line number required by D9. The only extra work is the cross-chunk fixup
in §5.2.

**Scalar fallback** per line, used only for `~valid` lines and for
non-fixed-position formats: the compiled regex, then integer arithmetic (still not
`strptime`). Lines that fail both are continuations.

Transient memory is bounded by the chunk: the reused scratch index plus the
per-column byte arrays for a 64MB chunk of ~1M lines is ~30MB.

**Measured (M0):** 149 MB/s on a 2019 Coffee Lake core, against 14 MB/s for a
per-line compiled regex and 3.2 MB/s for `datetime.strptime` — the approach
logmerger uses today. The fast path is ~47× `strptime`.

### 4.5 Inference and rollover (FR-14)

Two catalogue formats do not write a complete timestamp, and **the direction of
inference decides whether the answer is right**. logmerger anchors from
`st_ctime` and infers *forwards*, which mis-years every pre-January line of a
file spanning New Year: December entries get January's year, landing eleven
months in the future and scattering to the far end of the merged view. LogWeave
anchors at the **tail**, from `mtime`, and walks **backwards** — the last entry
is the one whose date can actually be pinned down, because the file was last
written at `mtime`.

Because the year is unknown, the extraction kernel cannot produce a `wall_us`
for these formats at all. It returns the civil *components* instead, and the
indexer completes them once the whole file has been seen — inference is global
to a file, so it cannot be done per chunk.

- **Yearless** (syslog `Mon DD HH:MM:SS`): a rollover is a point where the month
  jumps backwards by **at least 6 months** between consecutive entries. The
  per-entry year is `anchor_year - total_rollovers + cumsum(rollovers)`. The
  anchor year is `mtime`'s, less one if the final entry's month/day is later in
  the calendar than `mtime`'s — the last write is an upper bound.
- **Dateless** (strace `HH:MM:SS.ffffff`): the same shape one level down. A
  rollover is a backwards jump of **at least 12 hours**, and the anchor is
  `mtime`'s date, less a day if the final entry's time of day is after
  `mtime`'s.
- **The minimum-jump guards are load-bearing.** Without them two entries logged
  a millisecond out of order across a boundary read as a rollover and shift a
  whole year, or day, of entries — and the result looks entirely plausible.
  That failure is silent, which makes it part of risk R4.
- `mtime` must be supplied as **naive local `wall_us`**, not a UTC epoch (D1).
  `st_mtime` is an epoch, so the caller converts with
  `civil.epoch_us_to_wall_us`. Mixing the two shifts a file by the local UTC
  offset, which looks fine almost everywhere and mis-years a syslog across New
  Year; the parameter is named `mtime_wall_us` to make the mistake hard to
  repeat.
- Both set `flags` bit 4 so the UI can show that these timestamps are inferred,
  and a manual anchor date is offered per source.
- `tests/unit/test_rollover.py` holds this honest, including the logmerger bug
  written as a test that must fail if the forward approach is ever adopted, and
  the corpus carries `newyear_syslog.log` and `midnight_strace.log` whose mtimes
  the generator sets to their last entry so the anchor is faithful.

### 4.6 Out-of-order files (FR-16)

Monotonicity is checked once per source at build time. If `ts` is not
non-decreasing, a stable `order` permutation is built and stored, and all merge
reads for that source go through it. No other code needs to know. Tests use
logmerger's existing out-of-order corpus (`tests/log1_ooo_*.txt`).

---

## 5. Indexing pipeline

### 5.1 Stages

```
detect format (§4.3, ~400KB sampled)       ──►  ~5ms   ──► UI can show a format badge
index head chunk (64MB)                    ──►  <200ms ──► FIRST SCREEN PAINTS (NFR-1)
index remaining chunks, parallel            ──►  background, progress reported
build density histogram (§8.1)              ──►  folded into the chunk passes
check monotonicity, build `order` if needed ──►  one vectorised pass
write sidecar, build MergeIndex checkpoints ──►  §6.2
```

The first screen never waits for a full index. The view is usable from the head
chunk onward, and the scrollbar range and strips grow as indexing proceeds.

### 5.2 Chunking

Chunks are 64MB, aligned **down** to a line boundary — a worker reads an extra
1KB past its nominal end to find the next `\n`, so chunk boundaries never split a
line, and chunks are therefore fully independent.

**Line-number fixup.** A worker does not know how many lines precede its chunk, so
it writes `line_no` relative to its own chunk and returns its line count. Once all
chunks are done, the parent takes a prefix sum of those counts and adds each
chunk's base to its `line_no` slice — one vectorised add per chunk over the
memmapped array, a few milliseconds for a 1GB file. This is the only ordering
dependency in an otherwise fully parallel build, and it is why `line_no` is stored
rather than derived on demand.

### 5.3 Parallelism and the GIL

Indexing is CPU-bound and the hot kernels are mixed: NumPy releases the GIL, but
`re` does not. Threads would therefore serialise the regex work.

**A `ProcessPoolExecutor` is used, with workers writing directly into the
memory-mapped sidecar arrays.** Bulk data never crosses a process boundary; a
worker returns only a small summary (entry count, min/max ts, histogram deltas).
The pool is created warm at application start, because Windows `spawn` costs
100–300ms per worker.

Files are indexed concurrently, and chunks within a file are indexed concurrently.

> If free-threaded Python 3.14 plus free-threaded NumPy and PySide6 wheels prove
> solid, this becomes a thread pool with no other change. The process pool is the
> portable choice today.

**As built (M2b).** `logweave/session/prepare.py`, one file per worker. A worker
does not write into arrays the parent holds; it calls `open_index`, which writes
a sidecar, and returns a `Prepared` summary of a few hundred bytes. The parent
then opens the same path and maps that sidecar. The effect SPEC asked for --
bulk data never crossing a process boundary -- holds literally, and the parent
never has to coordinate concurrent writers into one array.

Measured on 4 x 104.9MB (12 cores, 4 workers): **27.9MB/s in process, 61.7MB/s
pooled, a 2.21x speedup.** Not 4x, because once a few cores are reading the
build is memory-bandwidth-bound, and the four workers share one disk.

The pool is **not** created warm at application start. It is started per batch,
and only when there are at least 32MB to index across more than one file
(`PARALLEL_THRESHOLD`): below that, spawning an interpreter and importing NumPy
costs more than the indexing. Starting it warm would pay the spawn cost on every
launch including the overwhelmingly common one -- reopening a cached set, which
indexes nothing at all.

**Chunks within a file are still indexed serially.** A chunk cannot be indexed
without the carry state from the chunk before it: the open multi-line entry, the
inferred year, the rollover count (§5.2). Splitting one file means either
speculating about the carry or stitching it afterwards, and neither is needed to
meet NFR-5 -- the window stays responsive because the work is in another
process, not because it is spread across cores. A single multi-gigabyte file
therefore still indexes at one-core speed, which is the honest limit of M2b.

### 5.4 Sidecar cache (PRD Q8)

Location: `%LOCALAPPDATA%\LogWeave\index\` (`$XDG_CACHE_HOME/logweave/index` on
Linux, `~/Library/Caches/LogWeave/index` on macOS).

Filename: `<blake2b-16 of canonical path>-<size>-<mtime_ns>.lwi`.

Layout: a JSON header (length-prefixed) followed by the arrays, each page-aligned
so they can be `np.memmap`-ed directly with zero copy.

```json
{ "format_version": 1, "parser_version": 7,
  "source": {"path": "...", "size": 2147483648, "mtime_ns": ..., "file_id": "..."},
  "head_sha": "<blake2b-16 of the first 64KB, or of the whole file if smaller>",
  "head_len": 65536,
  "ts_format": "YMDHMSdotF", "confidence": 0.997, "encoding": "utf-8",
  "entries": 10342118, "lines": 11904233, "indexed_to": 2147483648,
  "monotonic": true, "has_order": false, "lineno_saturated": false,
  "open_method": "share_all",
  "hist": {"bucket_us": 1000000, "t0": ..., "buckets": 86400},
  "arrays": [{"name":"off","dtype":"uint64","len":10342119,"offset":4096},
             {"name":"ts","dtype":"int64","len":10342118,"offset":...},
             {"name":"flags","dtype":"uint8","len":10342118,"offset":...},
             {"name":"line_no","dtype":"uint32","len":10342118,"offset":...}]
}
```

**As built (M2b).** The header above is the full design. What
`logweave/index/sidecar.py` writes today is that minus the fields no
implemented feature reads: `file_id`, `lineno_saturated`, `open_method` and
`hist` (the minimap histogram, M5) are absent, and `layout`, `message_offset`
and `severity_offset` are present -- the layout because an append has to index
the tail with the *same* layout the prefix was indexed with (§5.4 below), the
offsets because they are what the view slices a row by. A `format_version` bump
retires every older sidecar, so the header may grow freely.

**Validation and partial reuse.** A sidecar is reusable if `parser_version`,
`format_version` and `head_sha` match. Then:

- size and mtime unchanged → fully valid, reopen is a memmap (NFR-4, < 2s for 10GB).
- size **grew**, `head_sha` unchanged, and the byte at `indexed_to - 1` is still a
  line terminator → the index is valid **for its prefix**; only the appended region
  is indexed. This is what makes reopening a growing production log fast, and it is
  the same code path as follow (§7.3).
- size **shrank**, or `head_sha` changed → discarded, full re-index.

An LRU sweep enforces a configurable cap (default 10GB), with "clear cache" in the
UI (NFR-22). Eviction is by access time, not creation time: the set of logs
someone keeps coming back to predicts what they will open next far better than
the order they first opened them in.

**`head_len`, and why the head hash is not a fixed window.** The digest is
compared over the number of bytes the sidecar recorded, not over a fixed 64KB.
A log under 64KB would otherwise hash its entire contents, so appending a line
would change the digest, growth would be indistinguishable from rotation, and
the append path could never fire for a young file -- precisely the file someone
is tailing. The size comparison is made *before* the head comparison, so a
truncated file is reported as "file shrank" rather than as a changed head.

**Retiring a superseded sidecar.** Size and mtime are in the filename, so a
growing log earns a new sidecar on each reopen; the old one is pruned after a
successful write. On Windows that prune fails silently while the old sidecar is
still memory-mapped, so `FileIndex.release()` drops the mappings as soon as the
prefix has been spliced into the new index. Without it a log revisited a few
times leaves a near-full-size cache entry per visit and the LRU sweep spends its
whole budget on dead copies of one file.

**Measured (M2b).** On a 104.9MB, 1,580,073-entry log:

| | measured |
|---|---|
| First index (cold, single process) | 3.91s, 26.8MB/s |
| Warm reopen, steady state | **6.6ms** per file, independent of size |
| Sidecar size | 33.2MB, **21.0 bytes/entry** |
| 10GB set, warm, 10 files | 0.07s (NFR-4 budget 2.0s) |
| 10GB set, warm, 100 files | 0.66s (NFR-4 budget 2.0s) |

A warm reopen costs a stat, a 64KB hash and a memory map -- all per *file*, none
per byte -- which is why the budget is met with two orders of magnitude to spare
and why the number of files matters and their size does not.

21.0 bytes/entry is exactly `off` (8) + `ts` (8) + `flags` (1) + `line_no` (4).
The sidecar's size relative to the log therefore depends entirely on mean entry
length: 32% of this corpus, whose entries average 66 bytes, and about 4% of a
production log averaging 500.

---

## 6. Merge engine

### 6.1 The problem

Random access to merged row `i` must be O(1)-ish (NFR-5, NFR-6) across up to
2×10⁹ rows (NFR-15), while resident memory stays under 500MB (NFR-7). Materialising
the merge order costs 8 bytes a row — 16GB at the top of the range. Not viable.

### 6.2 Checkpointed k-way merge

The merge order is fully determined by the per-source `ts` arrays, the sort key
being `(ts + skew, source_order, entry_index)` (FR-17: ties break by file-open
order, then line number). So the order need not be stored — only enough state to
**resume** the merge at intervals.

Every `C = 4096` merged rows, store a checkpoint:

```python
class Checkpoint:
    row:     int          # = c * C
    ts:      int64        # timestamp at this row
    cursors: uint32[k]    # next unconsumed entry index, per source
```

Cost: `N/C` checkpoints × (8 + 8 + 4k) bytes. For 100M rows and 8 sources:
**~1.7MB**. For the 2×10⁹-row limit with 8 sources: ~34MB. Trivially resident.

**`row_at(i)`** → binary-search the checkpoints for `c = i // C`, then replay the
merge forward at most `C` steps from `cursors[c]`. The replay is vectorised: take
the next ≤C entries of each source's `ts` slice, `np.lexsort` the small combined
array, and get all 4096 rows of the block at once (~1ms). The block is then held
in a **BlockCache** (LRU, 64 blocks × 4096 × 8B = 2MB), so painting ~60
consecutive rows costs one replay, amortised to nothing while scrolling.

**`time_to_row(t)`** → `sum(np.searchsorted(src.ts, t - src.skew) for src in srcs)`,
which is O(k log n) with no replay at all: a handful of microseconds. This is
NFR-6, and it is why the timeline strip can seek instantly.

**`row_to_time(i)`** → `row_at(i).ts`.

### 6.3 Building the checkpoints

The build is **embarrassingly parallel**, because `time_to_row` gives an exact row
number for any time, so any time window can be processed independently with exact
global row numbering.

1. Split the global time span into windows each estimated (by `searchsorted`) to
   hold ~1M rows.
2. Each worker, for its window: slice each source's `ts`, build a combined
   `(ts, source_order, entry_index)` array, `np.lexsort` it, and emit the
   checkpoints whose row numbers fall inside the window.
3. Concatenate. Checkpoint row numbers are exact by construction.

For 100M rows: ~100 windows × ~150ms ≈ 15s single-threaded, ~2s across 8 workers,
with peak transient memory per worker of ~24MB. Re-running it is also how a
changed skew offset (D2, FR-18) is applied — and because the per-window work is
independent, only the affected windows need rebuilding when one source grows.

### 6.4 Incremental update under follow (FR-21)

Appended entries almost always have timestamps at or after the current tail, so:

- **Fast path** — new entries sort after the last checkpoint: extend the final
  partial block and append checkpoints. O(appended), no rebuild.
- **Slow path** — an appended entry sorts earlier than the current tail (a lagging
  writer, or a corrected skew): invalidate checkpoints from the affected time
  onward and rebuild only those windows. Row numbers after the insertion point
  shift, so the view holds its position by **timestamp**, not row number — which is
  the correct behaviour anyway when scrolled back in history.

A small **pending tail buffer** holds entries from the last ~2 seconds of wall
clock before committing them, which absorbs ordinary writer lag without ever
taking the slow path.

### 6.5 Enabling and disabling sources (FR-9)

A disabled source is dropped from the cursor set and the checkpoints are rebuilt —
a few seconds at the top of the size range, run in the background with the old
index still serving paints. Its `FileIndex` and sidecar are untouched, so
re-enabling is immediate.

---

## 7. Follow engine

### 7.1 Opening files without breaking the writer (NFR-20)

**A real trap worth stating plainly.** Python's `open()` on Windows requests
`FILE_SHARE_READ | FILE_SHARE_WRITE` but **not** `FILE_SHARE_DELETE`. A file held
open that way cannot be renamed or deleted by its writer — so merely watching a log
would make the application's own rotation fail. That is unacceptable (NFR-20).

`source/winfile.py` therefore opens via `ctypes` `CreateFileW` with
`GENERIC_READ`, `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`, and wraps
the handle with `msvcrt.open_osfhandle` to get a normal Python file descriptor. On
POSIX, ordinary `open()` already has these semantics.

### 7.1.1 Exclusively-locked files (FR-4a, Q10)

Some Windows applications open their log with no sharing at all, so even the
share-everything open above fails with `ERROR_SHARING_VIOLATION`. The instruction
is to try as hard as possible, so `winfile.py` escalates through a ladder and
stops at the first rung that works:

| # | Technique | Needs | Notes |
|---|---|---|---|
| 1 | `CreateFileW` with all three share flags | — | The normal path. Works for the large majority of writers, including anything using the CRT or .NET defaults. |
| 2 | Retry briefly with backoff (3 attempts over ~300ms) | — | Clears transient violations from antivirus scanners and indexers, which are a common false positive. |
| 3 | `FILE_FLAG_BACKUP_SEMANTICS` with `SeBackupPrivilege` enabled for the thread | Backup Operators or Administrators | Bypasses the sharing check the way a backup agent does. Privilege is enabled only for the open and dropped immediately after. |
| 4 | Read from a **VSS shadow copy** of the volume, created on demand | Administrator | Gives a consistent point-in-time snapshot. Expensive and non-live: the snapshot does not grow, so **follow is disabled** for a source read this way, and the UI says so rather than silently showing a frozen tail. |
| 5 | Give up | — | Report the failure, naming the rung that failed and what elevation would buy, and leave the rest of the session working (NFR-21). |

Rung 4 is offered, never automatic — it creates a system-wide snapshot, so it
requires explicit confirmation. Each rung's outcome is recorded on the source and
shown in the source panel, because a user needs to know whether they are looking
at a live file or a frozen snapshot. Risk R9 records that rungs 3 and 4 may simply
be unavailable to a non-admin user.

### 7.2 Identity, rotation, truncation (FR-23)

Path is never used as identity. `SourceIdentity` is:

- Windows: volume serial number + 64-bit (or 128-bit) file index, from
  `GetFileInformationByHandle` / `FILE_ID_INFO`.
- POSIX: `(st_dev, st_ino)`.
- Plus `size` and the `head_sha` of the first 64KB, as a cross-check.

| Observation | Interpretation | Action |
|---|---|---|
| Same identity, size grew | Normal append | Index the appended region (§7.3) |
| Same identity, size shrank | Truncated in place | Keep history in the view, mark the discontinuity, re-index from 0 into a new index generation |
| Path now resolves to a **different** identity | Rotated | Keep the old index as a frozen source; open the new file as a successor and continue following it. Per PRD Q7 they remain distinct sources in v1 |
| Path gone, identity still open | Rotated away or deleted | Keep reading the open handle to EOF, then mark stale (FR-25) |
| Identity unreachable (share dropped) | Disconnect | Mark stale, retry with backoff, keep history viewable (FR-25) |

### 7.3 Incremental read

The **partial-line rule**: a read that does not end at a line terminator must not
produce an entry. Bytes after the last terminator are held in a pending buffer and
prepended to the next read. Without this, a half-written line is indexed as a
truncated entry and then duplicated when the writer finishes it.

Appended regions are indexed by exactly the §4.4 kernels, just on a smaller chunk.

### 7.4 Watching

| Source kind | Mechanism | Latency |
|---|---|---|
| Local file | `ReadDirectoryChangesW` on the parent directory, filtered to the file, debounced 50ms | < 100ms |
| UNC / SMB | **Polling** `stat` at 250ms (configurable). Change notifications over SMB are unreliable and cannot be trusted (D5) | < 350ms |
| SSH / SFTP | Polling `stat` at 1s, adaptive: faster while the file is actively growing | < 1.2s |
| Linux / macOS | `inotify` / `FSEvents` via `watchdog`, with the poller as fallback | < 100ms |

NFR-8 (200ms) is met for local files. UNC and SSH are documented as poll-bounded;
the poll interval is exposed per source.

All watchers run off the UI thread and deliver `ChangeEvent`s onto the Qt event
loop through a queued connection.

---

## 8. Overview strips

Both strips (D4, FR-32, FR-33) are fed by **one** precomputed structure, which is
what keeps risk R3 contained: a repaint is a re-aggregation of a small array, never
a scan of the log.

### 8.1 Density histogram

Built during indexing, per source, stored in the sidecar:

```
bucket_us = max(1_000_000, span_us // 1_000_000)      # ≥1s, ≤1M buckets
hist      = uint32[buckets, 8]                         # counts per severity class
```

Computed per chunk, vectorised: `b = (ts - t0) // bucket_us`, then `np.bincount` on
`b * 8 + sev` (measured 46M entries/s, and faster than `np.add.at`). Cost is a few
MB per source and a single pass folded into the indexing pass that already touches
every entry.

**Alongside it, a prefix sum `cum = cumsum(hist)` is kept, rebuilt only when the
histogram changes — that is, on append.** M0 measured a repaint that re-ran the
`cumsum` over a 1M-bucket histogram at **8.3ms**, half a 60fps frame (NFR-5),
spent re-deriving data that had not changed. With the prefix sum prepared, a
repaint is **74µs**: two fancy-index reads and a subtraction.

### 8.2 Timeline strip (time-proportional)

Vertical axis is wall-clock time over the visible span. For a strip `H` pixels
tall, pixel `p` covers a time range; its counts are a range-sum read straight out
of the prepared prefix sum (§8.1) — `cum[e[1:]] - cum[e[:-1]]`, O(H·k), measured at
74µs for 800 pixels. The `cumsum` is **never** inside the repaint. Idle gaps appear
as blank pixels, bursts as dense bands, exactly as the chosen option showed. Click
maps a pixel to a time and calls `time_to_row` (§6.2).

### 8.3 Minimap strip (row-proportional)

Vertical axis is merged row index. Pixel `p` covers rows
`[p·N/H, (p+1)·N/H)`; `row_to_time` on each boundary (a checkpoint lookup) converts
that to a time range, and the same `hist` range-sum gives its colours. So the
second strip costs one extra array of H time values — no second data structure, no
second indexing pass.

The minimap drives the vertical scrollbar and draws the viewport box; the timeline
is purely a seek control.

### 8.4 Overlays

Search hits (FR-44) and bookmarks (FR-37) are sparse row lists, converted to pixel
rows and drawn over both strips as separate marks. Search hits arrive incrementally
from the search job and are drawn as they accumulate.

### 8.5 Scrollbar range

Qt scrollbars are 32-bit, so a 2×10⁹-row set cannot be addressed directly with any
headroom. The vertical scrollbar is therefore given a fixed virtual range of
0–1,000,000 units, mapped to rows. Keyboard and wheel navigation address rows
directly, so single-row stepping stays exact regardless of scale.

---

## 9. Rendering

### 9.1 `MergedLogView`

A `QAbstractScrollArea` subclass with custom painting, rather than
`QTableView`/`QTreeView`. The reasons: per-run styling inside a line (FR-30),
multi-line entries, the source-lane gutter, row counts beyond what an item model
handles comfortably, and full control of the frame budget (NFR-5).

The row-count reason is concrete rather than aesthetic: `QModelIndex.row()` is a
C++ `int`, and NFR-15's ceiling of 2×10⁹ rows is inside int32 by about 7%. That is
no margin for a view that also does arithmetic on row numbers.

**Three decisions taken during M3, with the reasoning, because each looks like a
shortcut and is not:**

- **Rows are a uniform height.** This section originally anticipated variable
  heights for multi-line entries. Implementing that needs a prefix sum of row
  heights to answer "which row is at pixel *y*" — 8 bytes a row, which is
  precisely the structure the checkpointed merge exists to avoid (§6.1: 16 GB at
  the row ceiling). So a multi-line entry paints its first line with a marker and
  expanding one is a per-entry affordance, never a global layout mode. Row-to-pixel
  mapping stays O(1) at any file size.
- **The vertical scrollbar is scaled past 10⁹ rows.** Qt scrollbars are `int`, so
  a 1:1 row mapping would run out at the row ceiling. Below the cap one unit is
  one row; above it the mapping is done in Python ints. A scaled bar can only put
  the top row on a multiple of the scale, so the guarantee a seek offers is *the
  target row is on screen*, not *the target row is at the top*. The maximum is
  rounded **up**: flooring it leaves the last `(total − page) % scale` rows
  unreachable, which during M3 meant you could scroll to within seven rows of the
  end of an eight-billion-row merge and no further.
- **The message is sliced to the visible character window before being drawn**,
  which bounds paint cost by window width instead of line length. Only valid
  because the font is fixed-pitch. §14.1b has the measurement that forced it.

`paintEvent` paints only the visible rows — about 60 at a typical window size:

1. From the scroll position, get the first visible row and offset.
2. `MergeIndex.row_at` for the visible span (one block replay at most, usually a
   BlockCache hit).
3. For each row, read its bytes from the source (`mmap` slice — no I/O syscall for
   local files), decode just that entry, and apply the compiled highlight rules
   (§10) to produce styled runs.
4. Draw, left to right: the marks gutter, the line-number gutter (FR-29a),
   the timestamp (pre-formatted via a per-format fast formatter), the message
   runs, then the alias column (FR-29) and the source lanes (FR-28).

**Column order, settled at M4.** The line number goes on the **left**, before
the timestamp. It was first built on the right beside the alias, because FR-29
and FR-29a both describe "where a row came from", but they are not the same kind
of thing: the alias answers *which file*, while the line number is a coordinate
*within* the row and belongs at the start of it — an editor gutter, `grep -n`, a
compiler error. Everything left of the message and everything right of it is
frozen against horizontal scroll either way (FR-36a).

**`paintEvent` must set the painter's font itself.** A `QPainter` constructed on
a widget does **not** inherit that widget's font; it starts at the application
default, which on Windows is a 9pt proportional UI face. Without that one call,
every message is drawn proportionally while every width in `paintEvent` is
computed from the monospace metrics, and a text-size change moves `_row_h` and
`_char_w` without changing a glyph. The viewport's font has to be set explicitly
too: a scroll area's viewport inherits it at construction and then stops, because
with a style sheet on the window Qt treats the viewport's font as its own and
ignores later propagation. **This is invisible to the offscreen test backend**,
which has no font database and renders every glyph 13×13 at any point size, so
it is asserted by spying on the painter and measured for real in
`tools/bench_ui.py` (§14.1b).

Per-frame work is bounded by the window size, not the data size. Decoded and
styled rows are held in a small LRU so re-paints during selection and resize are
free.

**The style cache is keyed by the row's text, not by `(source_id, entry_index)`
as planned.** A log repeats itself — the same heartbeat line thousands of times —
so keying by text makes all its copies share one scan, where keying by identity
would scan each separately. It also means a style entry can never go stale
against a re-indexed source, because nothing about the source is part of the
key.

**Scrolling does not repaint the screen.** `scrollContentsBy` blits the viewport
and lets Qt damage only the newly exposed band, so `paintEvent` honours
`event.rect()` and a three-row wheel step paints three rows. Measured at 0.98 ms
against 13.6 ms for the full frame (§14.1b) — a 14× difference, and the reason
scrolling has headroom while a seek does not. The blit is skipped when a scrollbar
unit is not one row, or the jump is bigger than a screen, since there is then
nothing worth preserving.

### 9.2 Source lanes (FR-28)

A fixed-width gutter on the right, `k` lanes of 4–12px. A row fills its own
source's lane with that source's colour. Colours come from a qualitative palette
with stable assignment by source order, chosen for distinguishability at 6px and
checked against both themes and common colour-vision deficiencies. Beyond ~20
sources, lanes compress to 2px and the alias column (FR-29) becomes the primary
cue.

### 9.3 Frame budget

Target < 16ms (NFR-5). Measures taken: no per-frame allocation of large objects;
glyph widths precomputed from the monospace metrics; styled-run construction
memoised; `QPainter` state changes batched by style.

M3 measured this and the results are in §14.1b. Two of the measures above turned
out to be irrelevant and one guess here was wrong, so, for the record:

- **Batching `QPainter` state changes by style buys nothing.** Drawing a row in
  one `drawText` call measured 14.94 ms against 15.03 ms for two. The cost is per
  *glyph*, not per call, so there is no batching win.
- The two things that did work are not in the list above: honouring
  `event.rect()` with a viewport blit (§9.1), and slicing the message to the
  visible character window.
- The `QImage`-strip fallback suggested here is still the right shape of idea —
  cache rasterised output rather than re-rasterising — but at the wrong
  granularity. Caching 4096-row strips means re-rendering a strip whenever
  anything in it changes, and it does complicate selection as predicted. A
  **per-glyph atlas** gets the same saving at a granularity that nothing
  invalidates, because the glyphs for a fixed-pitch font never change. §14.2
  item 4.

M4 added styled spans to pass 3, and the same lesson applied again for the same
reason (§14.1c):

- **A styled row is segmented, not overdrawn.** Drawing the line and then
  drawing each span over the top is the obvious implementation, and it cost
  **4×** what the shipped version costs at six spans a row, because it draws
  those characters twice and the cost is per glyph. The row is instead cut into
  runs that cover the visible window exactly once, the per-run pen and font are
  set only when they change, and the background pass is skipped entirely when no
  span carries one. Measured at +10.60 → +4.02 → +2.56 ms, in the proportional
  face the painter was wrongly using at the time (see §9.1); the shipped version
  re-measures at **+3.09 ms** (§14.1c).
- Styling is computed **once a row a frame**, before the passes, because pass 1
  needs the background and pass 3 the foreground. Besides saving the second
  scan, it makes it impossible for a row to end up with one rule's background
  under another rule's text.
- **Bold and italic faces are used only if the character advance is unchanged.**
  Span positions are arithmetic from one glyph width, so a wider bold face would
  displace every span after the first by a growing fraction of a character. A
  family whose bold is wider gets colour and no weight, which is a far better
  failure than creeping misalignment.

### 9.4 Opening files (FR-1, FR-19)

Dropping one or more files on the window opens and merges them; so does the open
dialog, and so do paths on the command line. All three go through one call,
`Session.add_files`, which indexes the batch and rebuilds the merge **once** —
dropping twelve files costs one checkpoint build, not twelve.

Two behaviours that are easy to get wrong:

- **A dropped directory is filtered to things that look like logs; a dropped file
  never is.** Dragging a single file is a decision about that file, and
  second-guessing it would make an unusual extension impossible to open. Dragging
  a folder is a bulk action, where pulling in every `README` and `.json` beside
  the logs is noise. Directory expansion is one level deep, because recursing a
  log tree can mean thousands of files with no way to take it back. The suffix set
  includes the empty suffix, since `syslog`, `messages` and `dmesg` have no
  extension and are exactly what someone drops a `/var/log` at.
- **Failures are reported in the status bar, never a modal dialog.** Dropping a
  folder of fifty logs where three are locked by another process is ordinary, and
  a modal that must be dismissed before the other forty-seven can be read is worse
  than useless. The detail goes in a tooltip. (A modal here also deadlocked the
  test suite, which is a fair indication of how it behaves for a user.)

A file whose format is not recognised still opens, and says so. "We could not
parse this" is information to show next to the data, not a reason to refuse the
file.

### 9.5 The timestamp column, and the duplicate it creates

The view shows the parsed `wall_us` in its own column. For most of the catalogue
the timestamp also *leads the line*, so without care the same value is printed
twice on every row — once parsed, once as the first thing in the message. On a
merged view of several files that is a column of noise down the middle of the
screen.

**Three rules, which together mean a row always shows its time exactly once:**

1. **The column is grey**, and a greyer grey when the timestamp was inferred
   rather than read (§4.5). It is the same value on every row, differing only in
   its last digits, so at full contrast it competes with the text being scanned.
   A guess and a fact should also not look identical.
2. **A leading timestamp is trimmed from the message.** `FileIndex.message_offset`
   records how much of the line the timestamp accounts for, computed from the
   proven layout at index time, and `Row.msg_from` carries it to the view.
   `Row.text` stays verbatim and the trim is applied by whoever displays it, so
   nothing is lost. A timestamp that is *not* at the front — an access log, where
   the leading field is the client address — is never trimmed, because the text
   in front of it is content rather than a repetition. The threshold is four
   characters of slack, enough for a level prefix and no more.
3. **The column can be hidden** (`Ctrl+T`), which is worth doing when every line
   carries a consistent timestamp of its own and the width is better spent on
   the message.

Rules 2 and 3 are interlocked: **hiding the column turns the trimming off.**
Trimming only ever removes a *duplicate*, so it must stop the moment the column
holding the other copy goes away. Without the interlock, hiding a redundant
column would delete the timestamp from the row altogether — the one outcome the
feature exists to prevent. `test_the_time_is_always_visible_one_way_or_the_other`
states that property directly.

The same reasoning applies to copy and export: the `annotated` preset prints a
timestamp column of its own, so it trims by the same rule (§11.3). `plain` prints
no timestamp, so it trims nothing and emits the line verbatim.

---

## 10. Highlighting

### 10.1 Rules (FR-30, FR-31)

```python
@dataclass
class HighlightRule:
    name: str
    pattern: str
    is_regex: bool = False
    case_sensitive: bool = False
    scope: Literal["line", "match"] = "line"
    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    italic: bool = False
    priority: int = 0            # higher wins on overlap
    enabled: bool = True
    severity: Severity | None = None   # if set, also sets the index severity class
```

All enabled rules compile into **one** alternation regex with named groups, so a
line is scanned once regardless of rule count. `scope="line"` rules style the whole
row; `scope="match"` rules style only the matched span. Overlaps resolve by
priority, then by rule order.

**As built at M4, with one limitation this design accepts.** `finditer` over a
single alternation returns **non-overlapping** matches, so a span nested inside
another span cannot both match: whichever alternative matches at the earliest
position consumes the text, and there is no second pass to recover the inner one.
Recovering it means scanning twice, which is the cost the one-regex design exists
to avoid, so the behaviour is asserted in the tests rather than worked around.

That makes *which* alternative wins at a given position the whole of overlap
resolution. The alternatives are therefore emitted in `(-priority, list order)`
order at compile time, which is what makes the sentence above true for spans as
well as for the line-scope winner.

Four further as-built details:

- Per-rule case sensitivity is a **scoped inline flag**, `(?i:...)`, so one
  pattern carries a mixture of case-sensitive and case-insensitive rules.
- A rule may ask for a **word boundary** without being a regex. This matters more
  than it looks: the shipped severity rules need the boundary to stop `DEBUGGER`
  reading as `DEBUG`, but they must stay *literals* to feed the vectorised
  index-time classifier (§10.3), which compares fixed byte columns and cannot run
  a regex. As regexes they painted correctly and left the strips uncoloured.
- Line-scope rules scan a **bounded prefix** (2048 bytes), and the styled result
  is memoised by row text — so a 40 000-character JSON record costs no more than
  an ordinary line, and a repeated line costs one scan between all its copies.
- Generated group names are prefixed, so a user pattern carrying its own named
  group cannot collide with the compiler's.

**Built-in rulesets, user-extensible (D11, Q2).** Three ship as defaults. **As
built at M4 the three are: severity words, exception and stack-trace *shapes*,
and syslog priorities (`<0>`–`<7>`)** — which is a different cut from the one
planned above, for two reasons. The shapes were split out of the severity set
because they are the only `scope="match"` rules among the built-ins: they colour
the exception class name or a `at com.acme.Gateway(...)` frame *inside* the line,
where the severity words colour the whole row, and someone who wants one does not
necessarily want the other. The planned numeric-level set was dropped: a bare
integer has no reliable meaning across formats, and the syslog set already covers
the one convention that does.

Severity is on, shapes are on, **syslog is off by default** — `<3>` at the start
of a line is rare outside a raw capture, and a rule that never fires still costs
a branch in the one regex every row is scanned with.

Users may add, edit, reorder, disable and delete rules freely, and may point any
rule at any severity class; a rule the engine cannot compile is reported in
`Ruleset.rejected` rather than discarded or raised, so one bad pattern in an
imported set does not take the rest down with it. Built-ins are never silently
overwritten: a user edit to a built-in rule shadows it (clearing its `builtin`
flag), while merely *disabling* one does not.

**Still unbuilt after M4**, and both needed before FR-31 is complete: the rule
editing dialog — the engine supports per-rule editing, the UI exposes only the
three set switches — and TOML import and export of rulesets, which is what makes
a team able to share one. Those are M8 with the rest of the settings work.

The eight severity **classes** (§10.2) are fixed, because they are three bits in
`flags` and their order is what lets `np.maximum` fold severities and the strips
aggregate them. Their *labels and colours* are editable, and arbitrarily many
patterns may map onto each. Needing a ninth class would be an index-layout change,
so the eight were chosen with headroom (`NOTICE` is included precisely so that
syslog's levels map without loss).

### 10.2 Severity classes

Three bits in `flags` (§2.3), fixed and ordered so `np.maximum` folds correctly:

```
0 NONE   1 TRACE   2 DEBUG   3 INFO   4 NOTICE   5 WARN   6 ERROR   7 FATAL
```

### 10.3 Severity at index time

The strips need severity for **every** row, not just visible ones, so severity
cannot be computed at paint time (SPEC 8). It is computed during indexing.

> **This section was rewritten after M0 measured it.** The original design ran one
> combined regex over every byte of every chunk. That measured **6–8 MB/s** and put
> the whole index pipeline at **7.4 MB/s** — 14× under NFR-3, with 7.8 of 8.6
> seconds inside a single `re.finditer`. A regex over every byte was never going to
> work. What follows is the design that does.

#### Primary path — fixed-offset vectorised classification

Once a fixed-width timestamp precedes it, the level word sits at a **constant byte
offset** from the start of the line. That is true of the overwhelming majority of
real formats, and it is the same observation the timestamp fast path (4.4) rests
on. Detection finds the offset (4.3 step 7); classification is then a handful of
vectorised comparisons:

```python
col = lazy_case_folded_column_gather(offset)   # gathers on first use only
first = col(0)
for level, words in ruleset.by_level:          # highest level first
    for w in words:
        head = first == w[0]
        if not head.any():
            continue                           # word absent from this file
        m = head & (lens >= offset + len(w))
        for j, ch in enumerate(w[1:], start=1):
            m &= col(j) == ch
        m &= (lens == offset + len(w)) | not_word_char(col(len(w)))   # \b
        np.copyto(sev, level, where=m & (sev == 0))                   # highest wins
```

Case folding is one pass. The trailing boundary check is what stops `DEBUGGER`
reading as `DEBUG` and `INFORMATIONAL` as `INFO`. Iterating
highest-severity-first and writing only where `sev` is still 0 gives
highest-wins without a separate fold.

Two refinements added at M1, when the shipped ruleset was widened from 10 words
to 24 so that it covers what syslog actually writes (`err`, `crit`, `emerg`) and
what application logs actually write (`FAIL`, `EXCEPTION`). Naively that cost
41% of throughput; these recovered essentially all of it:

- **First-letter prefilter.** Most words in a ruleset do not occur in a given
  file at all — a default ruleset carries two dozen, a log typically writes
  three or four. One pass over the first column rejects an absent word outright,
  instead of a pass per letter.
- **Lazy column gathers.** The longest word in the ruleset sets how many columns
  *could* be needed; a real file needs far fewer. A log writing only `INFO` and
  `ERROR` never looks past column 5, so eagerly gathering all twelve columns
  demanded by `INFORMATION` is mostly work thrown away.

**Measured: 320 MB/s at M0 with 10 words; 422 MB/s at M1 with 24 words**, against
8 MB/s for the regex. A ~50× improvement over the regex, and what takes the full
pipeline to 128 MB/s single-core.

#### Fallback — regex scan

For a format whose level position is not predictable (a variable-length logger
name before the level, for instance), one combined-alternation regex over the
chunk, then `searchsorted` to map match offsets to line indices, then a fold that
keeps the highest severity per line. The fold is a `lexsort` and a
last-of-each-group write, not `np.maximum.at` — M0 measured no advantage either
way, and `lexsort` is the clearer of the two.

**This fallback does not meet NFR-3.** Measured at 7.4 MB/s single-core, ~62 MB/s
projected aggregate, which is below the 100 MB/s budget. Consequences the UI must
honour:

- Format detection reports when a source is on the fallback, and the source panel
  shows it, because indexing that file will be visibly slower.
- A custom format definition should let the user *declare* the level offset, which
  moves that source onto the fast path.
- Closing this gap properly — a vectorised multi-pattern matcher built from NumPy
  primitives — is the first item of the post-M1 performance work, ahead of any
  consideration of native code (D8).

#### Rule changes

Only *severity-defining* rules participate here; cosmetic rules stay at paint time.
Editing a severity-defining rule invalidates `flags` and the histogram and requires
a re-scan. That is a bytes-only pass — no timestamp parsing, no line splitting — so
it is several times faster than a full index, and it runs in the background with
the old colours shown until it completes.

---

## 11. Search, filter, copy and export

### 11.1 Search (FR-44)

A cancellable background job over the merged stream in time order, scanning raw
bytes and reporting hits incrementally so the first results and the strip marks
appear immediately (NFR-10).

- Literal, case-sensitive: `bytes.find` over each chunk — memchr-class speed.
- Literal, case-insensitive: a compiled regex, or a cased-variant Aho-Corasick for
  long patterns.
- Regex: compiled `re` over chunk text.
- Hits are recorded as merged row indices in a growable array, and drive the hit
  count, next/previous, and strip overlays.

### 11.2 Filter (FR-45) — two tiers

A filter changes row numbering, so the set of passing rows must be materialised for
the scrollbar and row count to be correct. Two very different costs:

**Tier 1 — metadata filters** (severity set, source set, time window). Evaluated
directly on the `ts` and `flags` arrays as boolean masks, with no text access at
all. A filtered `MergeIndex` is simply one built over masked cursor arrays. For a
100M-row set this is a handful of vectorised passes — **this is what meets NFR-9
(< 500ms)**.

**Tier 2 — text filters** (include/exclude literal or regex). Requires reading
bytes, so it runs as a background job like search, producing a passing-row array
incrementally. The view becomes usable at once and its row count grows as the pass
proceeds. Governed by NFR-10, not NFR-9.

Filters compose: tier 1 narrows the candidate set first, so a tier-2 pass over
"ERROR lines in the last hour" reads only those bytes. Clearing a filter is
instant — the unfiltered `MergeIndex` is retained, never rebuilt (FR-45).

### 11.3 Copy and export (D9, FR-46a, FR-46b, FR-47)

The point of attribution is that a row pasted into a ticket stays traceable. Every
emitted row therefore carries its **source file** and its **original line number
within that file** — `line_no` from §2.3, not the merged row index.

**Formatters.** One interface, `RowFormatter`, serves clipboard and file export, so
there is exactly one implementation of attribution:

| Preset | Output |
|---|---|
| `annotated` *(default)* | `app.log:1      2026-10-04 14:22:08.113  Connection pool exhausted` |
| `aligned` | Exactly as displayed on screen, columns padded to the view's widths |
| `plain` | Message text only, no timestamp, no attribution |
| `markdown` | A table with Source, Line, Time, Severity, Message columns |
| `csv` | RFC4180-quoted, one row per entry, same columns |
| `template` | A user string over `{source} {alias} {path} {line} {timestamp} {severity} {message} {row}` |

**Built at M3 and M4: `annotated` and `plain`.** The other four extend this
module at M7; the interface is shaped for them now so that attribution has
exactly one implementation, as this section requires. M4 bound the two to
separate commands rather than to a preference — **Ctrl+C** annotated, **Ctrl+Shift+C**
plain — because they are wanted for different things within one session: the
first is evidence to paste into a ticket, the second is input for a parser, a
diff, or a search box that would choke on the prefix. A preference would mean
visiting a dialog between those two acts.

The row cap is on the *selection*, not on the preset, so dropping the
attribution is not a way around it; it is a refusal rather than a silent
truncation, because FR-47's export is the right tool for a whole view and it
streams.

**Rules that fall out of the data model:**

- **Source naming** renders as basename (default, matching `app.log` / `SOA.log`),
  alias, or full path. Basename is the default because it is what someone reading
  a ticket needs in order to find the file.
- **Column widths** in `annotated` are computed from the widest value in the
  *selection*, not the whole log, so a 40-row copy is not padded to the width of a
  41M-row file.
- **Multi-line entries** emit the annotation once, on the entry's first line;
  continuation lines are indented beneath it and inherit that line number. A
  setting annotates every physical line instead, with incrementing line numbers,
  for people pasting into tools that parse line references.
- **Line numbers are 1-based**, matching every editor a reader will open the file
  in.
- **Stale generations**: if a source was truncated or replaced since the rows were
  indexed (§7.2), those rows are marked in the output rather than being given line
  numbers that no longer resolve (risk R10).
- Export (FR-47) differs from copy only in destination and in offering the whole
  view rather than the selection; filters apply to both.

---

## 12. Profiles

### 12.1 Format

**TOML**, extension `.lwprofile`, registered for double-click open (FR-53).
Chosen over JSON for comments and diff-friendliness; read with stdlib `tomllib`,
written with `tomlkit` so user comments and ordering survive a round-trip.

```toml
format_version = 1
name = "Acme Gateway"
# Paths are absolute. No variable expansion, no relative root (D10) — a profile
# is machine-specific by design.

[view]
timestamp_format = "%H:%M:%S.%f"
relative_time = false
wrap = false
font = { family = "Cascadia Mono", size = 10 }
theme = "dark"
minimap = { visible = true, width = 80 }
timeline = { visible = true, width = 24 }
source_lanes = { visible = true, lane_width = 6 }
alias_column = true
line_number_column = false            # FR-29a, off by default

[copy]                                # FR-46a / FR-46b
format = "annotated"                  # annotated | aligned | plain | markdown | csv | template
attribute = true
source_as = "basename"                # basename | alias | path
annotate_continuations = false
# template = "{source}:{line}  {timestamp}  {message}"

[[source]]
glob = "C:/Acme/logs/gateway-*.log"
select = "newest"                     # all | newest | modified_within
count = 3                             # with select="newest"
alias = "gw"
colour = "#4C9AFF"
follow = true
# ts_format = "YMDHMSdotF"            # optional override (FR-13)
# encoding = "utf-8"
# skew_us = -252000000                # per-file offset, D2/FR-18

[[source]]
glob = "C:/Acme/iis/u_ex*.log"
select = "modified_within"
within = "7d"
alias = "iis"
colour = "#F5A623"
follow = true

[[source]]
ssh = "deploy@db01:/var/log/postgresql/postgresql.log"
auth = "agent"                        # agent | key | password  (D14, FR-7a)
# key_file = "C:/Users/dan/.ssh/id_ed25519"
# For auth="password": the password is NEVER stored here. It is prompted per
# session, or kept in the OS credential vault keyed by host+user (NFR-26).
credential_vault = true
alias = "pg"
colour = "#57D9A3"
follow = true

[filters.saved."errors only"]
severity = ["WARN", "ERROR", "FATAL"]

[highlight]
inherit_global = true
[[highlight.rule]]
name = "correlation id"
pattern = "cid=[0-9a-f]{16}"
is_regex = true
scope = "match"
fg = "#8777D9"
priority = 10

[[bookmark]]
source = "gw"
timestamp = "2026-10-04T14:22:08.120"
note = "pool exhausted"
```

### 12.2 Resolution at open (FR-50, FR-51, FR-52)

1. Take each `path` or `glob` as an absolute path, verbatim (D10). A relative path
   in a profile is a validation error, reported rather than guessed at.
2. Resolve each `glob` and apply `select` — `all`, `newest` + `count`, or
   `modified_within` + `within`.
3. Open each resolved file; collect failures rather than aborting.
4. Report the outcome in one non-modal panel: resolved, unmatched globs, missing
   paths, unreachable hosts — each with a retry. The session opens with whatever
   succeeded.

Bookmarks are stored as `(source alias, timestamp)`, not row numbers, so they
survive the file having grown, rotated, or been re-indexed.

### 12.3 Global settings

`%APPDATA%\LogWeave\settings.toml` holds the global highlight rules, default
theme and font, cache cap, poll intervals, default copy format, and recent lists
(FR-54). A profile may inherit or override.

**As built at M4, the view preferences live in `QSettings`, not in that file.**
Text size (FR-38), lane width (FR-28), the alias and line-number columns (FR-29,
FR-29a), the timestamp column and which highlight sets are active persist through
the platform store — the registry on Windows, a plist on macOS, an ini on Linux.
`settings.toml` arrives with the profile work at M8 and will hold the things a
*team* shares, which is a different problem from where one person left a window:
a TOML file under version control is the wrong place for "this user's font is
13pt", and the registry is the wrong place for a ruleset a team wants to review
in a pull request.

Two rules that the store itself cannot enforce, so the reader does:

- **Everything read back is clamped or intersected with what exists.** The store
  is a registry key or a text file someone may edit, so a font size outside
  6–32 point is clamped rather than honoured, and a highlight set named in the
  store but absent from this version is dropped rather than looked up. A
  preference file must not be able to produce a window that cannot be used to
  fix itself.
- **`QSettings` never fails.** It returns whatever is there, or a default, and
  reports nothing — so a test that touches it is reading and writing the real
  user's preferences by default. The GUI suite points it at a temporary file per
  test (§15.2). This is the same class of defect the sidecar cache had at M2b,
  and harder to notice, because there is no error to miss.

**No secrets, anywhere.** Neither a profile nor the settings file ever contains a
password or passphrase (NFR-26). Credentials live in the Windows Credential
Manager (DPAPI-protected), keyed by `logweave:ssh:<user>@<host>`, with the
Keychain and Secret Service as the portable equivalents. The profile stores only
whether to consult the vault. This matters because profiles are plaintext TOML
that users are expected to put under version control.

---

## 13. Threading and concurrency

| Work | Where |
|---|---|
| UI, painting, all Qt objects | Main thread only |
| Format detection | Worker thread (I/O-bound, small) |
| Index build, severity scan, checkpoint build | `ProcessPoolExecutor`, warm at start, writing into memmapped sidecars (§5.3) |
| Search, tier-2 filter | Worker thread per job, cancellable, chunked, progress reported |
| Watchers | One thread per watcher kind; events queued to the UI thread |
| SSH I/O | Dedicated thread per connection |

Rules:
- No Qt object is touched off the main thread. Cross-thread delivery is by
  `Signal` with a queued connection, or `QMetaObject.invokeMethod`.
- Every long operation is cancellable and reports progress (NFR-11).
- The paint path never blocks on a lock. Index arrays are append-only and
  published by an atomic generation-counter swap, so a paint always reads a
  consistent snapshot without locking.
- Paints read memmapped arrays directly; readers never wait on writers.

## 14. Performance plan

Risk R1 is the project's main technical risk, so measurement comes first, not last.

**`tools/bench.py` exists before the UI does.** It benchmarks, against a generated
corpus, each of: line splitting, timestamp extraction (vectorised and scalar),
severity scan, full index throughput, checkpoint build, `row_at`, `time_to_row`,
and paint-path row assembly. It runs in CI on every commit and fails the build on
a regression beyond a tolerance.

### 14.1 Measured — M0 result

Python 3.14.5, NumPy 2.5.0, 2019 Coffee Lake laptop (12 logical cores), 64MB
chunk of ~1M lines in `YYYY-MM-DD HH:MM:SS.ffffff`. Single-threaded kernels.

| Stage | Measured | Mechanism |
|---|---|---|
| Line split | **640 MB/s** | `frombuffer` + `flatnonzero` |
| Line split, lone-CR variant | 467 MB/s | FR-6 secondary scan (§3.3) |
| Timestamp extract, vectorised | **149 MB/s** | §4.4 column arithmetic |
| Timestamp extract, per-line regex | 14 MB/s | 11× slower |
| Timestamp extract, `strptime` | 3.2 MB/s | **47× slower** — logmerger's approach |
| Severity, fixed offset | **320 MB/s** | §10.3 primary path |
| Severity, regex fallback | 7.4 MB/s | §10.3 fallback; **below NFR-3** |
| Histogram | 46M entries/s | `bincount` |
| Strip repaint, 800px | 74µs | from the prepared prefix sum (§8.1) |
| **Full index pipeline** | **98 MB/s one core** | split + extract + severity + entries |
| **Projected aggregate** | **~820 MB/s** | 12 cores at a pessimistic 70% |
| Merge checkpoint build | 7.7M rows/s | §6.3, 8 sources |
| `time_to_row` (seek) | **27µs** | NFR-6 budget is 50ms |
| Checkpoint memory | **0.0196 MB / 1M rows** | vs 8 MB/1M materialised — **409×** |

**Verdict on risk R1 and decision D8: no native code is required.** NFR-3's
≥100 MB/s aggregate is met with roughly 8× margin. A 1GB log indexes in ~10s on
one core and ~1.5s across the pool. Note the hardware is a 2019 part; the budgets
in PRD §8.1 assume a 2024-era machine, so there is further headroom there.

Run-to-run variation of 15–20% is normal on a laptop under thermal load. Compare
components within one run, not across runs.

#### M1 result — the real modules

M0's figures came from prototype kernels. M1 replaced them with the shipped
modules, which differ in two ways that could each have cost throughput: the
extractor became **generic** (layout-driven, serving all 15 catalogue formats
rather than one hardcoded ISO layout) and it gained **field-range validation**
that the prototype skipped. Same machine, same 64MB chunk, measured in one run:

| Stage | M0 prototype | M1 shipped | Note |
|---|---|---|---|
| Line split | 963 MB/s | **962 MB/s** | unchanged; promoted verbatim |
| Timestamp extract | 275 MB/s | **229 MB/s** | −17%, buys generality + validation |
| Severity, fixed offset | 433 MB/s | **422 MB/s** | −3% while carrying 2.4× the words |
| **Full index pipeline** | 98–101 MB/s | **128 MB/s one core** | 76-byte lines |

The stage comparisons above are within one run on identical bytes, which is the
only way they mean anything. The **committed gate** (`tools/bench.py`, baseline
in `tools/bench_baseline.json`) uses its own synthetic corpus at ~67 bytes/line
and reports **90 MB/s single-core, ~758 MB/s projected aggregate**. The two
headline figures differ because MB/s is sensitive to line length — shorter lines
mean more lines per megabyte and so more per-line work — not because anything
changed. Treat the gate's number as the one to compare future runs against, and
never compare across workloads: `bench.py` enforces this with a
`same_workload` guard that refuses a baseline recorded at a different chunk size
or source count.

The 17% given up on extraction is a deliberate trade: a generic kernel means a
new format costs a regex and no new code, and the validation is what lets
detection cross-check itself against the regex oracle (§4.3 step 6b). NFR-3 is
met with ~7× margin either way.

**Both deltas were found by measurement, not assumed.** A first cut of the wider
severity ruleset cost 41% of that stage; §10.3's prefilter and lazy gathers
recovered it. Without the M0 benchmark harness to localise the cost, a 41%
regression inside a stage that is itself only a third of the pipeline would
simply have been absorbed and never noticed.

#### Merge engine result

Same machine, 2M rows across 8 sources, measured against the shipped
`MergeIndex`:

| Operation | Measured | Budget | Note |
|---|---|---|---|
| `time_to_row` (seek by time) | **26.6 µs** | NFR-6: 50 ms | no replay at all — ~1900× margin |
| 60 rows while scrolling | **49.4 µs** | NFR-5: 16 ms frame | the number that decides whether scrolling is smooth |
| 60 rows after a random jump | 2.77 ms | NFR-5: 16 ms frame | honest worst case: one full block replay |
| `row_at`, working set cached | **8.2 µs** | — | |
| `row_at`, cache cleared | 2.99 ms | — | **365× worse than warm** |
| Block cache hit rate, scrolling | **98.5%** | — | |
| Checkpoint build | 7.7 M rows/s | — | serial; SPEC 6.3 parallelises it |
| Checkpoint memory | **0.0196 MB / 1M rows** | vs 8 MB/1M materialised | **409× smaller** |

Two things worth drawing out. The **365× gap between a warm and a cold
`row_at`** is the entire justification for the block cache in SPEC 6.2 — without
it, every paint during a scroll would pay the 3 ms replay, and 60 fps would be
impossible. And a **cold jump costs 2.77 ms**, which fits a frame but is 56×
a scroll step, so the view should prefetch adjacent blocks after a seek rather
than assume a jump is as cheap as a scroll.

Both numbers took a corrected benchmark to see. The first attempt rebuilt the
whole index per probe, so "cold seek" actually measured 250 ms of checkpoint
construction; the second reused random probes for the warm case, which miss a
64-block cache almost every time and so measured the cold path twice under two
names.

### 14.1b Measured — M3 result (the paint budget)

Run `python tools/bench_ui.py`. Windows 11, Cascadia Mono 10pt, 8×17px cell,
1400px-wide window, one source, 20 000 entries.

Two measurement traps had to be removed before any of these numbers meant
anything, and both had produced optimistic figures:

- **The offscreen Qt platform has no font database.** Every family resolves to
  the empty string, `fixedPitch()` is False, and every glyph measures 13×13. Text
  timings taken there are a fallback renderer's and were out by roughly 2×. The
  bench opens a real window, parked off-screen.
- **`resize()` on a widget that was never shown does not reach the viewport.**
  The row count came back as 28 at every window height while the frame time still
  grew, which is how it was caught. The bench now shows the window and asserts
  the viewport actually grew.

A top-level window is also clamped to the physical screen, so heights above the
display cannot be measured this way and the bench says when it was clamped.

| Operation | Measured | Budget | Verdict |
|---|---|---|---|
| **Scroll step** (3 rows, ordinary lines) | **0.97 ms** | 16 ms | **16× under** |
| **Scroll step** (3 rows, long lines) | **1.27 ms** | 16 ms | **13× under** |
| Full repaint, 48 rows, ordinary lines | 13.6 ms (284 µs/row) | 16 ms | inside |
| Full repaint, 48 rows, long lines | **18.5 ms** (385 µs/row) | 16 ms | **over** |
| `session.rows(56)` | 0.63 ms | — | 4% of a frame |
| `format_wall_us` | 2.58 µs | — | 0.15 ms for 60 rows |

**The scroll step is the number that matters**, and it is comfortable. It is also
not free: it costs 1 ms instead of 14 ms because of two deliberate choices.

1. **`scrollContentsBy` blits the viewport** and lets Qt damage only the newly
   exposed band, so `paintEvent` honours `event.rect()` and paints three rows for
   a three-row wheel step instead of a screenful.
2. **The message is sliced to the visible character window** before being drawn.
   This is legitimate only because the font is fixed-pitch, and it is what makes
   paint cost independent of line length: a 4000-character stack trace measured
   **61 ms** a frame drawn whole against **5.6 ms** sliced. Stack traces and SQL
   are entirely ordinary log content, so without this the budget is missed by 4×
   on real data rather than in theory.

Painting in passes -- one per clip region rather than one per row -- and
freezing the timestamp column (FR-36a, which narrows the scrolling area and so
the glyph count) took long lines from 20.6 ms to 18.5 ms. Still over.

**The residual breach is a full repaint of long lines.** A full repaint happens on
a seek, a resize or a filter change — never on a scroll. Its cost is linear in
visible rows, so it crosses 16 ms somewhere around 40–55 rows depending on line
length, and a larger display makes it worse.

The cause is Qt's raster text path at roughly **1.8 µs a glyph**, and it is not
addressable by tuning. Measured and rejected:

| Attempt | Result |
|---|---|
| One `drawText` a row instead of two | 14.94 ms vs 15.03 ms — **no change** |
| `PreferNoHinting` | 16.7 ms — worse |
| `NoSubpixelAntialias` | 15.9 ms — worse |
| `NoAntialias` | 19.6 ms — much worse |
| Consolas instead of Cascadia Mono | 15.2 ms — noise |
| `ARGB32` vs premultiplied vs `RGB32` | identical |
| `QStaticText`, cached across frames | 1.86× faster **on a cache hit only** |

Merging two draw calls into one changing nothing is the decisive result: the cost
is per *glyph*, not per call, so there is no batching win to be had. `QStaticText`
is the only real speedup and it only helps re-display — a seek misses the cache by
definition, which is exactly the case that is over budget.

So the fix, when it is needed, is to stop asking Qt to shape and rasterise text
every frame: a **fixed-pitch glyph atlas**, each ASCII glyph rastered once and
blitted per character, which is what terminal emulators do. That is ADR-4's
native-accelerator question, which now has a concrete trigger (>40 visible rows
of long lines) and a concrete target (1.8 µs a glyph). It may not need native
code at all — blitting from an atlas into the backing store is expressible in
NumPy.

### 14.1c Measured — M4 result (highlighting in the frame)

`python tools/bench_ui.py`, same machine, font and method as §14.1b, 48 visible
rows, four runs. The "exception spans" case is a synthetic trace corpus
averaging six highlighted spans a row.

**Taken after the painter-font defect in §9.1 was fixed.** Everything measured
during M4 before that point — including the span-cost progression in §9.3 — was
drawn in a 9pt proportional UI face, not the monospace one, and is not
comparable with §14.1b or with these.

| Case | Rules off | Rules on | Delta | Verdict |
|---|---|---|---|---|
| Full repaint, ordinary lines | 10.85 ms | 10.94 ms | **+0.09 ms** | inside |
| Full repaint, long lines | 17.63 ms | 18.12 ms | **+0.46 ms** | over, as at M3 |
| Full repaint, exception spans | 20.09 ms | 23.14 ms | **+3.09 ms** | over |
| **Scroll step** (worst case) | — | **1.48 ms** | — | **11× under** |

The ordinary-lines delta is inside the run-to-run spread, which was −0.12 ms to
+0.11 ms over the four runs. The claim that holds is "not measurable at this
resolution", not "+0.09 ms".

**The exception-spans breach is not caused by highlighting.** It was already
20.1 ms with every rule off, which is §14.1b's residual breach — Qt's raster
text at ~1.8 µs a glyph on a full repaint of long content — and it dominates the
3.09 ms the rules add. The conclusion of §14.1b is unchanged and item 4 below is
still the fix; M4 makes it slightly more urgent without changing its shape.

Getting the span cost there took two changes, described in §9.3, from a starting
point 4× higher. The headline is the same as M3's: the cost is per glyph, so the
win came from not drawing any character twice, not from batching calls.

### 14.2 Remaining performance work

In priority order. Native code stays off the table until these are exhausted (D8).

1. **A vectorised multi-pattern matcher** to replace the regex severity fallback,
   built from NumPy primitives (case-folded buffer, candidate filtering on a rare
   byte signature, verification on candidates only). The fallback is the one path
   that misses NFR-3, so this is the only outstanding budget breach.
2. **Extraction is now the dominant cost** at 54% of the pipeline. Of its ~430ms
   per 64MB: ~165ms gathers, ~90ms arithmetic, ~21ms validation. The gathers are
   the target.
3. **`merge_row_at` is 1.2ms cold** because it replays up to 4096 rows. Harmless
   with the BlockCache (§6.2) that the prototype lacks, but worth confirming once
   the cache exists — a 60-row paint must stay well inside the 16ms frame.
4. **A fixed-pitch glyph atlas** for the merged view, replacing `drawText` in
   `paintEvent`. The only remaining budget breach after M3 (§14.1b): a full
   repaint of long lines costs 18.5 ms against a 16 ms frame, at ~1.8 µs a glyph,
   and no Qt-level tuning moves it. Scrolling is unaffected, so this is a seek-
   and-resize problem rather than a smoothness one, which is why it sits here and
   not above item 1. M4 confirmed the diagnosis from the other direction: a
   span-heavy full repaint is 23.1 ms, of which 20.1 ms is already there with
   every highlight rule switched off (§14.1c).
5. Only then: an isolated native accelerator (Rust via PyO3, or Cython) behind a
   feature flag, with the pure-Python path retained as the reference implementation
   and the two cross-checked against the corpus in tests. Items 1 and 4 are the
   two candidates; item 4 may well be achievable in NumPy alone.

## 15. Testing

### 15.1 Corpus (`tools/gen_corpus.py`)

Generates logs with **known ground-truth ordering**, so merge correctness is
checkable exactly:

- every format in the §4.1 catalogue
- multi-line entries and stack traces
- out-of-order timestamps, duplicate timestamps
- midnight and New Year rollover for yearless and dateless formats
- mixed line endings; UTF-8, UTF-8-BOM, UTF-16LE, CP1252; invalid byte sequences
- a 1MB single line; a file with no timestamps; an empty file; a file of one
  partial line
- CSV with quoted newlines; JSONL with varying key order
- size tiers: 1KB, 1MB, 100MB, 1GB, 10GB (large tiers generated on demand, not
  committed)
- **known line numbers**: every generated entry records the physical line it starts
  on, so copy/export attribution (D9) is checked against ground truth, including
  across chunk boundaries and for multi-line entries
- a file with more than 4.29×10⁹ lines is *not* generated; `line_no` saturation is
  covered by a unit test that injects the boundary directly

logmerger's own test fixtures (`tests/log1_ooo_*.txt`, `log0_no_timestamps.txt`,
`log1_*_with_multiline.txt`) are imported as a regression baseline.

### 15.2 Layers

| Level | Scope |
|---|---|
| Unit | Format detection and ranking; extraction against ground truth; rollover; `days_from_civil`; severity folding; checkpoint arithmetic; sidecar validation; profile round-trip; `line_no` chunk-base fixup and saturation; every copy/export formatter preset |
| Attribution | For a merged selection spanning several sources, assert each emitted row's `source:line` resolves to byte-identical text when that file is independently read at that line number (D9). Run after append, after rotation, and after truncation |
| Locked files | The §7.1.1 ladder: a writer holding an exclusive lock, each rung forced to fail in turn, non-admin rung-3/4 refusal, and the "follow disabled on a VSS snapshot" behaviour |
| Property | Generate random entry sets, index them, assert the merged order is exactly `sorted(by (ts, source_order, entry_index))`; assert `row_at(time_to_row(t)).ts >= t` and adjacency invariants |
| Integration | Open → index → merge → read, headless, no Qt. Append, rotate, truncate via `tools/rotate_sim.py`. Disconnecting a share. Vanishing files |
| Rotation matrix | Copy-truncate, rename-create, move-to-dated-name, delete-recreate, writer holding an exclusive lock, antivirus-induced sharing violations |
| Performance | §14, in CI, with regression gates |
| GUI | Plain `pytest` with a module-scoped `QApplication` fixture, on the offscreen platform, covering the view's row arithmetic, damage regions, selection, keyboard navigation, copy, and drag-and-drop. `pytest-qt` is not used — nothing here needs to wait on a signal or pump an event loop, so it would be a dependency for the sake of it. A small set of screenshot comparisons follows for the strips (M5). Four cautions, all learned the hard way. (1) The offscreen platform has **no fonts**, so it cannot measure text (§14.1b) — and it therefore cannot see a *wrong* font either, which is how the §9.1 painter defect survived a green suite; defects of that kind are asserted by spying on the painter, which works on any backend. (2) A `QDropEvent` holds a **non-owning** pointer to its `QMimeData` — letting that fall out of scope in a test is an access violation that takes the interpreter down rather than an exception. (3) `QSettings` must be redirected to a temporary file per test, or the suite reads and writes the real user's preferences (§12.3). (4) A `QThread` parented to a window and destroyed while still running **aborts** the process at interpreter shutdown rather than raising, so the window joins its indexing thread in `closeEvent` and the test fixture joins it too |

CI runs every non-GUI test on both Windows and Linux from the first commit (NFR-18).

## 16. Packaging

| Artefact | Tooling |
|---|---|
| Single signed Windows exe (NFR-19) | PyInstaller one-file, Qt modules pruned to QtCore/QtGui/QtWidgets, UPX off (antivirus false positives), Authenticode-signed. Nuitka is the fallback if cold start misses NFR-12's 2s |
| Python package | `pip install logweave`, standard `pyproject.toml`, console entry point |
| Linux / macOS | Unsupported but installable from PyPI; CI proves the engine builds and passes headlessly |

Dependencies, kept deliberately few: `PySide6`, `numpy`, `tomlkit`, `watchdog`,
`keyring` (credential vault, NFR-26); `paramiko` only if FR-7 ships in v1. No
dependency may be added without an ADR.

### 16.1 Licensing (D15, NFR-25)

- `LICENSE` — MIT, covering our code.
- `NOTICE` — credits **logmerger** (Paul McGuire, MIT) for the timestamp-format
  catalogue adopted in §4.1, reproducing its MIT text. Attribution is a condition
  of that licence, and §4.1 adopts the catalogue wholesale, so this is required,
  not courtesy.
- **PySide6 / Qt is LGPLv3.** To keep the distributed bundle compliant: Qt is used
  unmodified, linked dynamically, its licence text is included in the installer and
  in About, and the Qt shared libraries in the bundle remain **replaceable** by the
  user. A PyInstaller one-file build satisfies the last point because it unpacks the
  Qt DLLs at runtime; if a future packaging change were to statically fuse Qt, that
  would break LGPL compliance and needs an ADR.
- A third-party licence manifest is generated at build time from the resolved
  dependency set and shipped alongside the binary.

## 17. Milestones

| M | Deliverable | Proves |
|---|---|---|
| **M0** ✅ | Corpus generator, bench harness, CI on Windows + Linux | The ground truth and the performance gate exist before the code they judge. **Done.** Retired risk R1 ahead of schedule and rewrote §4.4, §8.1–8.2, §10.3 and §4.3 on the evidence (§14.1) |
| **M1** ✅ | Format detection + vectorised extraction + `FileIndex`, headless; plus the whole source layer (§3, §7.1.1) | **Retires risk R4.** All 15 catalogue formats detect and index with `wall_us`, `line_no` and `byte_off` matching corpus ground truth exactly, at 21.0 bytes/entry. 128 MB/s single-core (§14.1). **Done.** |
| **M2a** ✅ | `MergeIndex` with checkpoints and a block cache, headless | **NFR-6 measured at 26.6 µs**, merged order matches corpus ground truth row for row, 409× memory saving confirmed (§14.1). **Done.** |
| **M2b** ✅ | Sidecar cache, parallel indexer, indexing off the UI thread | **NFR-4 met with two orders of magnitude to spare**: a warm reopen is 6.6 ms per file and does not scale with size, so a 10 GB set in 10 files reopens in 0.07 s against a 2.0 s budget (§5.4). Parallel indexing measured at 2.21x on four files, bounded by memory bandwidth rather than scheduling (§5.3). Found three cache bugs that each produced a *correct* index while silently halving or removing the speedup, and one where the test suite was being served by its own previous runs. Within-file chunk parallelism remains unbuilt and is not needed for NFR-5. **Done.** |
| **M3** ✅ | Qt shell: `MergedLogView`, scrolling, timestamp and message columns, drag-and-drop, source lanes, source dock, copy with attribution | **NFR-5 measured (§14.1b): a scroll step is 0.98 ms, 16× under the frame budget.** First genuinely usable build. Found and fixed an unreachable-last-rows scrollbar bug and an unbounded paint cost on long lines; left one measured breach, a full repaint of long lines at 18.5 ms, with the fix scoped in §14.2 item 4. **Done.** |
| **M4** ✅ | Highlighting, severity classes, source lanes, aliases, text size, copy in two modes | **FR-28 to FR-31, FR-38 (size and zoom) and FR-46a met.** Highlighting is not measurable on ordinary lines (within the run-to-run spread) and costs **+3.09 ms** on a synthetic corpus averaging six spans a row, against a worst scroll step of 1.48 ms — 11× under budget (§14.1c). The span-heavy full repaint is over budget at 23.1 ms, but 20.1 ms of that is M3's glyph-cost breach with every rule off, so §14.2 item 4 remains the fix and nothing new is owed. Established that one-pass alternation scanning cannot style nested spans, and documented it as a limitation rather than hiding it (§10.1); found that overlap resolution was ignoring `priority` entirely, that the shipped severity rules were contributing nothing to the index-time classifier, and that overdrawing spans cost 4× what segmenting them does. The `highlight` package is Qt-free and enforced as an engine layer. Two further defects surfaced in use rather than in the suite and are recorded in §9.1: the painter was never handed the view's font, so the merged view drew in the application's proportional UI face and a zoom changed no glyph; and the line-number column had been built on the right instead of the left. **Done.** |
| **M5** | Both strips (D4) | FR-32 to FR-34; confirms risk R3 is contained |
| **M6** | Follow engine, rotation matrix green | FR-21 to FR-26, NFR-8, NFR-20. **Retires risk R5** |
| **M7** | Search, filters, selection, copy, export | FR-41 to FR-48, NFR-9, NFR-10 |
| **M8** | Profiles, settings, file association, recent lists; the highlight-rule editing dialog and TOML ruleset import/export left over from M4 (§10.1) | FR-49 to FR-54, and FR-31 completed |
| **M9** | Packaging, signing, cold-start tuning, docs | NFR-12, NFR-19. **v1.0** |

Phase 2 (Event Log) begins with a new `LogSource` implementation and a spill file;
by construction it needs no change to layers 2–4.

---

## Appendix A — Decisions deferred to ADRs

Each gets a short `docs/ADR/NNN-*.md` when it is taken:

1. Whether to materialise the full merge-order array below a row-count threshold as
   an optimisation over §6.2.
2. The qualitative source-lane palette, validated for 6px width, both themes, and
   colour-vision deficiency.
3. Whether tier-2 filter results are cached to disk per predicate.
4. Native accelerator: whether, and Rust/PyO3 vs Cython (gated on M1). M3 added a
   second candidate alongside the severity matcher — a fixed-pitch glyph atlas for
   the view (§14.1b) — and the atlas may be achievable in NumPy, which would
   settle this ADR without any native code.
5. A readable-message-plus-detail-pane treatment for CSV and JSONL, post-v1 — the
   enhancement D17 defers.
6. Whether a VSS snapshot read (§7.1.1 rung 4) may be re-taken periodically to
   approximate follow on a locked file, or stays strictly one-shot.

*Closed by the Q&A in PRD §11.1: gzip block indexing (Q1, now D12) and rotated-file
source groups (Q7, now D16).*
