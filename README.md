# pythonLogWeave

**Merge many log files into one time-ordered, live-following, syntax-highlighted view.**

Drop five log files on the window and immediately see what happened, in order, as
it happens. A Windows-first desktop app that works out each file's timestamp
format automatically, interleaves every entry in true chronological order, follows
the files as they grow, and stays responsive from kilobytes to multiple gigabytes.

Inspired by [logmerger](https://github.com/ptmcg/logmerger) (whose timestamp-format
catalogue it builds on), [BareTail](https://www.baremetalsoft.com/baretail/), and
Notepad++ DocMonitor — combining what each does well: correct timestamp
interleaving, a real follow-tail engine, and a GUI that does not fall over on large
files.

> **Status: M0 complete.** Ground truth and the performance gate exist; the
> application does not yet. See [Roadmap](#roadmap).

- [`docs/PRD.md`](docs/PRD.md) — product requirements: goals, 55 functional
  requirements, performance budgets, decisions, risks
- [`docs/SPEC.md`](docs/SPEC.md) — technical specification: architecture, data
  structures, algorithms, milestones

---

## First-time repository setup

Not yet a git repository. These are the commands to run; **run them yourself** —
nothing in this project commits or pushes on your behalf.

```bash
git init
git add .
git commit -m "M0: PRD, spec, corpus generator, benchmark harness, CI"
git branch -M main
git remote add origin https://github.com/moomindc/pythonLogWeave.git
git push -u origin main
```

<details>
<summary>The original GitHub snippet, for reference</summary>

```bash
echo "# pythonLogWeave" >> README.md
git init
git add README.md
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/moomindc/pythonLogWeave.git
git push -u origin main
```

The first two lines differ above: this README already exists with real content, so
there is nothing to `echo`, and `git add .` stages the whole M0 deliverable rather
than the README alone.
</details>

---

## Quickstart

Requires Python 3.12 or newer.

### 1. Create a virtual environment

**Windows (PowerShell)**

```powershell
cd path\to\pythonLogWeave
py -3.13 -m venv .venv
py -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\Activate
python -m pip install --upgrade pip
```

If PowerShell refuses to run the activate script, allow it for your user once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Or use
`.\.venv\Scripts\activate.bat` from `cmd`, which needs no policy change.

**Linux / macOS**

```bash
cd path/to/pythonLogWeave
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 2. Install dependencies

```bash
python -m pip install -r requirements-dev.txt    # app + tests + lint
```

| File | Installs | For |
|---|---|---|
| `requirements.txt` | numpy, PySide6 | running the GUI |
| `requirements-dev.txt` | the above, plus pytest and ruff | working on it |
| `python -m pip install "numpy>=2.0"` | numpy alone | the engine, headless, no Qt |

That last row is not a curiosity. Layers 1–4 never import Qt (SPEC §1), a CI job
installs no Qt to prove it, and the whole engine is usable from a script or a
notebook with NumPy as its only dependency.

`pyproject.toml` remains the source of truth; the requirements files exist so a
checkout you are editing works without installing the package. To install it as
a package instead: `python -m pip install -e ".[gui,dev]"`, which also puts a
`logweave` command on your PATH.

### 3. Run it

```bash
python -m logweave                 # empty window; drag files onto it
```

### 4. Make yourself some logs to try it on

```bash
python tools/make_test_logs.py     # writes C:\temp\logweave-logs
python -m logweave C:/temp/logweave-logs
```

Seven files telling one incident from six different angles — a slow dependency
backs up a connection pool, the breaker opens, requests fail, an operator
restarts a worker, it recovers. The causal order is only visible once they are
interleaved, which is the whole point of the program. Deliberately included:
six timestamp formats, differing sub-second precision, multi-line stack traces,
3000-character SQL lines, a UTF-16 file with a BOM, and a rotated pair that has
to sort back into one stream. `--help` for the options.

This is **not** the test corpus: `tools/gen_corpus.py` writes machine-checkable
ground truth that the suite asserts against, while these files exist to be
looked at.

### 5. Tests and benchmarks

```bash
python tools/gen_corpus.py --out tests/corpus     # corpus + ground truth
python -m pytest                                  # the suite
python tools/bench.py                             # engine kernels
python tools/bench_ui.py                          # the paint budget
```

### Using the GUI

Drag one or more files onto the window, or a folder to open the logs inside it.
A named file is always opened as named; a folder is filtered to things that look
like logs, one level deep.

| | |
|---|---|
| Left dock | Each source with its lane colour, entry count and detected format. Untick one to hide its rows without closing the file; hover for the encoding and detection confidence |
| Right gutter | One lane per source, so you can see at a glance which file a run of rows came from |
| Left edge | A severity bar, so errors are findable without reading the level word |
| Ctrl+C | Copy the selected rows with `app.log:1` attribution |
| Ctrl+A | Select all (a copy past 200 000 rows is refused rather than truncated) |
| Ctrl+T | Hide the timestamp column — see below |
| Arrows, PgUp/PgDn, Home/End | Move; hold Shift to extend the selection |
| Scrollbar at the bottom | Scrolls the message only; timestamp and lanes stay put |
| File ▸ Clear Index Cache | Delete the saved indexes — see below |

**The index cache.** The first open of a large log has to read all of it; after
that the index is saved beside the cache and reopening the same file costs about
7 ms however big it is. A file that has merely *grown* since — a production log
you keep coming back to — only has its new tail read. Nothing is reused unless
the file's head still hashes the same, so a rotated log is re-indexed rather
than mis-read.

The saved indexes cost about 21 bytes an entry and live in
`%LOCALAPPDATA%\LogWeave\index` (`$XDG_CACHE_HOME/logweave/index` on Linux,
`~/Library/Caches/LogWeave/index` on macOS) — never beside the log, which is
often read-only or on a share. They are capped and swept oldest-used-first, and
**File ▸ Clear Index Cache** deletes them and reports what it freed. Files
already open keep working.

Indexing happens in worker processes while the window stays usable, with
progress and a Cancel in the status bar. Cancelling keeps whatever had already
finished — those files are in the cache, so they cost nothing to open.

**The timestamp column.** LogWeave shows the timestamp it parsed in its own
column, in grey, and greyer still when it had to infer the value rather than
read it. Because that column is already showing the time, a timestamp that
*led* the line is trimmed off the front of the message instead of being printed
twice. A timestamp *inside* the line — an access log, where the first field is
the client address — is left exactly where it is.

Ctrl+T hides the column, which is worth doing when every line carries its own
timestamp anyway. Hiding it also stops the trimming, so the time never
disappears from the row entirely: you are choosing which copy to keep, not
whether to have one.

Larger corpora, generated on demand and never committed:

```bash
python tools/gen_corpus.py --out tests/corpus --perf 1mb,100mb
python tools/gen_corpus.py --out D:/big --perf 1gb --only perf --perf-multi
python tools/bench.py --file tests/corpus/perf-100mb/single.log
```

Performance gate, and updating the committed baseline:

```bash
python tools/bench.py --gate                      # fails on regression
python tools/bench.py --update-baseline           # after an intentional change
```

---

## Roadmap

Milestones are defined in [`docs/SPEC.md` §17](docs/SPEC.md). This table is the
living record — updated as each lands.

| M | Deliverable | Status |
|---|---|---|
| **M0** | Corpus generator, benchmark harness, CI on Windows + Linux | ✅ **done** |
| **M1** | Format detection, vectorised extraction, `FileIndex`, headless — plus the whole source layer | ✅ **done** |
| **M2a** | Checkpointed `MergeIndex` with a block cache | ✅ **done** |
| **M3** | Qt shell: merged view, scrolling, drag-and-drop | ✅ **done** |
| **M2b** | Sidecar cache, parallel indexer, indexing off the UI thread | ✅ **done** |
| **M4** | Highlighting, severity classes, source lanes, aliases | ⬜ next |
| **M5** | Minimap and timeline strips | ⬜ |
| **M6** | Follow engine; rotation matrix green | ⬜ |
| **M7** | Search, filters, selection, copy, export | ⬜ |
| **M8** | Profiles, settings, file association | ⬜ |
| **M9** | Packaging, signing, cold-start tuning — **v1.0** | ⬜ |

Then: phase 2 Windows Event Log, phase 3 syslog listener.

---

## M0 — what it delivers and what it found

M0 deliberately builds the measuring instruments before the thing being measured.
PRD risk **R1** was that Python cannot reach NFR-3's ≥100 MB/s indexing
throughput, and **D8** forbids writing native code until a benchmark proves it
necessary. M0 answers that question.

### Verdict: no native code needed

Measured on a 2019 Coffee Lake laptop (Python 3.14.5, NumPy 2.5.0), 64MB chunk,
~1M lines:

| Stage | Throughput | Note |
|---|---|---|
| Line splitting | ~640 MB/s | `frombuffer` + `flatnonzero` |
| Timestamp extraction (vectorised) | ~149 MB/s | SPEC §4.4 column arithmetic |
| Severity, fixed offset | ~320 MB/s | vectorised |
| **Full index pipeline** | **~98 MB/s single-core** | **~820 MB/s projected across 12 cores** |
| NFR-3 (≥100 MB/s aggregate) | **PASS**, ~8× margin | |

For comparison, on the same data:

| Approach | Throughput |
|---|---|
| Vectorised columns (ours) | **149 MB/s** |
| Per-line compiled regex | 14 MB/s |
| `datetime.strptime` per line (what logmerger does) | 3.2 MB/s |

A 1GB log indexes in ~10s on one core, ~1.5s across the pool.

### Three findings that changed the spec

**1. A regex severity scan over every byte would have sunk the project.** The
initial pipeline measured **7.4 MB/s** — 14× under budget — with 7.8 of 8.6
seconds inside one `re.finditer` over the chunk. Classifying severity at a
*detected fixed byte offset*, the same trick the timestamp fast path uses, took it
to 320 MB/s (44×) and the whole pipeline to 98 MB/s. The regex fallback remains
for formats whose level position varies, but it measures 62 MB/s even aggregate,
i.e. **below NFR-3** — so the fast path is load-bearing, not an optimisation.

**2. The overview strips were re-deriving data on every repaint.** Re-running
`cumsum` over a 1M-bucket histogram cost **8.3ms per repaint** — half a 60fps
frame (NFR-5). Prefix-summing once per append and leaving the repaint as a diff
took it to **74µs**, a 110× reduction.

**3. Extraction was memory-bound, not compute-bound.** Building the `(n, 26)`
gather index as `int64` is 208MB at 1M lines. Per-column gathers with a reused
scratch index, one unsigned comparison per digit instead of two, pairwise digit
combination in 1-byte lanes, and a constant-date fast path that calls
`days_from_civil` once per chunk rather than once per line took extraction from
97 to 149 MB/s.

All three are now in [`docs/SPEC.md`](docs/SPEC.md) with the measurements attached.

### The checkpointed merge holds up

SPEC §6.2 claims the merge order never needs materialising. Measured: checkpoints
cost **0.0196 MB per million rows** against **8 MB/million** to materialise —
**409× smaller**. Seeking to an arbitrary timestamp is **~27µs** against NFR-6's
50ms budget. Building checkpoints runs at 7.7M rows/s single-threaded, and the
work is embarrassingly parallel.

---

## M1 — what it delivers and what it found

A headless engine that reads a log file and tells you, for every entry, exactly
when it happened. All 15 catalogue formats, verified entry by entry against the
corpus manifest — not just timestamps but **line numbers and byte offsets too**,
because a test that checks only timestamps passes with completely broken copy
attribution.

### Risk R4 is retired: the engine checks itself

R4 was "auto-detection silently picks a wrong format and mis-orders a file". The
word that matters is *silently* — it parses, the timestamps look plausible, and
the file is simply in the wrong order. A test suite catches that only for the
formats somebody thought to test.

So the check is in the engine, not just the tests. Detection measures a format's
real byte positions from a sample, synthesises a vectorised layout, and then
**proves that layout against an independently written per-line regex parser on
the same sample**, discarding it on any disagreement. Two parsers written
differently agreeing is evidence; one clever parser nobody checks is a
liability. A file that fails the check still opens — it just takes the slow,
correct path.

### Layouts are measured, not declared

A format's regex says what *may* appear; a file says what *does*. `\d{1,9}`
fractional digits means the field width is not knowable from the pattern, so it
is measured. One generic kernel is then driven by the result, which is why
adding a format costs a regex and no new code — and why an Apache access log,
whose timestamp sits after a variable-length IP address, still runs vectorised.

### The logmerger New Year bug, fixed

syslog writes no year; strace writes no date. logmerger infers *forwards* from
the file's creation time, which dates every December line of a file spanning New
Year with January's year — eleven months in the future, scattered to the far end
of the merged view. LogWeave anchors at the **tail**, from `mtime`, and walks
backwards. A rollover is only believed on a jump of six months (or twelve
hours), so two entries logged a little out of order can't shift a whole year.

### Three bugs found by building it

- **In the contract I wrote myself.** Rotation detection compared file-head
  hashes taken over different prefix lengths, so *every append to any log under
  64 KB* read as a truncate-and-rewrite — a full re-index of every young log,
  forever.
- **A units bug at the mtime boundary.** `st_mtime` is a UTC epoch; everything
  inside the engine is naive local (D1). syslog passed by luck and would have
  mis-yeared at New Year. The parameter is now called `mtime_wall_us`.
- **The corpus was never reproducible.** `hash(str)` is salted per process, so
  `--seed` reproduced nothing. Now byte-identical across hash seeds.

Measured throughput is in [`docs/SPEC.md` §14.1](docs/SPEC.md). The gate now
benchmarks the shipped modules, keeping the M0 prototypes as reference rows so
the cost of the shipped kernel's generality stays visible instead of becoming
the new normal.

---

## M3 — what it delivers and what it found

A window you drag log files onto. Merged, time-ordered, severity-coloured, with a
source lane per file, a source dock, keyboard navigation, and copy with
`file:line` attribution. The engine layers stay Qt-free and a CI job installs no
Qt to prove it.

### Scrolling is 16× under budget; a seek is not

Full numbers in [`docs/SPEC.md` §14.1b](docs/SPEC.md).

| Operation | Measured | Budget |
|---|---|---|
| Scroll step, 3 rows | **0.97 ms** | 16 ms |
| Scroll step, 3 rows of long lines | **1.27 ms** | 16 ms |
| Full repaint, 48 rows | 13.6 ms | 16 ms |
| Full repaint, 48 rows of long lines | **18.5 ms** | 16 ms — **over** |

Scrolling is cheap because of two choices. The view **blits the viewport** and
repaints only the band Qt reports as damaged, so a three-row wheel step paints
three rows rather than a screenful. And the message is **sliced to the visible
character window** before being drawn, which makes paint cost depend on window
width instead of line length: a 4000-character stack trace measured **61 ms** a
frame drawn whole against **5.6 ms** sliced. Stack traces and SQL are ordinary log
content, so that was a 4× budget miss on real data, not a theoretical one.

A full repaint — a seek, a resize, a filter change, never a scroll — is still over
budget on long lines, and that is recorded rather than smoothed over. The cost is
Qt's raster text path at ~1.8 µs a glyph and no tuning touches it: hinting,
antialiasing, image format and font all measured as noise or worse, and drawing a
row in **one** `drawText` call instead of two changed nothing, which proves the
cost is per glyph rather than per call. The fix is a fixed-pitch glyph atlas,
scoped in §14.2 item 4 — possibly without native code.

### Two measurement traps, both flattering

Both produced *better* numbers than the truth, which is the dangerous direction.

- **Qt's offscreen platform has no font database.** Every family resolves empty,
  `fixedPitch()` is False, every glyph measures 13×13. Text timings taken there
  were out by roughly 2×. The paint bench now opens a real window, parked
  off-screen.
- **`resize()` on a never-shown widget does not reach the viewport.** The row
  count came back as 28 at every window height while the frame time still grew —
  the benchmark was dividing real work by a fictional row count.

### Three bugs found by building it

- **The last rows of a huge file were unreachable.** The scrollbar maximum was
  floored by the row-space scale, so you could scroll to within seven rows of the
  end of an eight-billion-row merge and no further. Found by a test that asked for
  the plain user-facing property — is the last row reachable — across the scale
  boundary.
- **Hiding a source un-hid itself.** Visibility was derived from `follow`, which
  is about tailing, so every merge rebuild silently showed everything again.
  They are now separate attributes.
- **Copy mangled multi-line entries.** Each row was emitted as one line, so a
  stack trace interleaved annotations with Java frames. The spec already said
  what to do; the implementation hadn't.

### One thing deliberately pulled forward

Copy with attribution belongs to M7, and M3 implements the `annotated` preset
anyway: a log viewer you cannot copy a line out of is not a usable build, and
attribution was an explicit requirement from the start. It lives at layer 4, so
M7 extends it rather than replacing it.

---

## M2b — what it delivers and what it found

Reopening a log you have already indexed costs a memory map and nothing else, a
drop no longer freezes the window, and the saved indexes have a size, a cap and
a button to delete them.

### Reopening is 122× faster, and the size of the file stops mattering

On a 104.9MB, 1,580,073-entry log:

| | Measured | Budget |
|---|---|---|
| First index (cold, one process) | 3.91 s, 26.8 MB/s | — |
| Warm reopen, steady state | **6.6 ms** | — |
| 10 GB set, warm, in 10 files | **0.07 s** | NFR-4: 2.0 s |
| 10 GB set, warm, in 100 files | **0.66 s** | NFR-4: 2.0 s |
| Sidecar on disk | 33.2 MB, **21.0 bytes/entry** | — |

A warm reopen is a stat, a 64 KB hash and an `np.memmap`. Every one of those is
per *file*; none is per byte. That is why the NFR-4 budget is met with two
orders of magnitude to spare, and why the number of files matters while their
size does not.

21.0 bytes/entry is exactly `off` (8) + `ts` (8) + `flags` (1) + `line_no` (4),
so what the cache costs relative to the log depends entirely on mean entry
length: 32% of this corpus, whose entries average 66 bytes, and around 4% of a
production log averaging 500.

### Parallel indexing: 2.21×, not 4×

Four 104.9MB files, four workers on twelve cores: **27.9 MB/s in process,
61.7 MB/s pooled.** The shortfall against a notional 4× is memory bandwidth and
one shared disk, not scheduling, and it is recorded rather than tuned around.

Chunks *within* a file are still indexed serially, because a chunk cannot be
indexed without the carry state from the one before it — the open multi-line
entry, the inferred year, the rollover count. A single multi-gigabyte file
therefore still indexes at one-core speed. That is the honest limit of M2b, and
it is not what NFR-5 is about: the window stays responsive because the work is
in another process, not because it is spread across cores.

### The window stays usable because the session stays single-threaded

The obvious implementation — run `Session.add_files` on a worker thread — is a
data race on every array the view paints from. So the work is split instead of
moved: the worker only guarantees **a sidecar exists** for each path, sharing no
object with the window and returning a few hundred bytes of summary, and the GUI
thread then opens the same files as cache hits at ~7 ms each. Progress and a
cancel live in the status bar rather than a modal, so the window answers
throughout. A second drop while indexing is queued, not raced: two pools on one
machine fight for the same disk and finish later than either alone.

### Four bugs found by building it

- **Every *second* append quietly became a full re-index.** The splice that
  joins a cached prefix to a freshly indexed tail dropped `layout_json`, so the
  sidecar written from a spliced index carried no layout, so the next append
  could not rebuild the detection it needed. One append always worked; two in a
  row did not. Found by a test that appends three times and demands each result
  be byte-identical to a full re-index.
- **The append path could never fire for a file under 64 KB.** The head digest
  covered a fixed 64 KB window, which for a small file is the whole of it, so
  appending a line looked exactly like log rotation. The hash is now compared
  over the length that was recorded — which is the case of a young log being
  tailed, the one that matters most.
- **A superseded sidecar would not delete on Windows.** Size and mtime are in
  the filename, so a growing log earns a new sidecar on each reopen and the old
  one is pruned — except that the prune fails silently while the old sidecar is
  still mapped. A log revisited a few times left a near-full-size cache entry
  per visit. The mappings are now released once the prefix has been spliced.
- **The test suite was getting cache hits from previous runs.** Tests wrote
  real sidecars into the user's own cache directory, so a later test could be
  served by an earlier one's leftovers and the miss path silently stopped being
  tested. Every test now gets its own empty cache root.

### What a worker may not do

`prepare` is an optimisation that runs in other processes on someone else's
machine, where pools break, sandboxes forbid spawning and files vanish between
the stat and the read. Every one of those comes back as a missed speedup, never
a log the user cannot see: the session opens the same path straight afterwards
and reports any real error against the file it belongs to. `BrokenProcessPool`
is a `RuntimeError` rather than an `OSError`, which the obvious `except OSError`
would miss — and it is what happens when antivirus or an OOM killer takes a
worker.

---

## Layout

```
docs/          PRD.md, SPEC.md
requirements.txt      numpy + PySide6, to run it
requirements-dev.txt  the above + pytest + ruff, to work on it
logweave/
  source/      layer 1: bytes in, however they are stored
    base.py          the LogSource contract everything else codes against
    winfile.py       CreateFileW with FILE_SHARE_DELETE; the SPEC 7.1.1 ladder
    encoding.py      BOM / UTF-16 / UTF-8 / ANSI detection, decode on demand
    file.py          local and UNC files, mmap or buffered
    gzipped.py       .gz decompressed once into the cache (D12)
  index/       layer 2: bytes to timestamps, headless and Qt-free
    layouts.py       the 15-format catalogue and the layout model
    detect.py        sampled detection, layout synthesis, oracle cross-check
    extract.py       the one vectorised kernel, plus the scalar oracle
    civil.py         calendar arithmetic and local-zone conversion
    severity.py      severity classification, fast path and fallback
    lines.py         line splitting and entry grouping
    indexer.py       chunked build and rollover inference
    fileindex.py     the four parallel arrays, 21 bytes an entry
    sidecar.py       the on-disk index cache: format, validation, LRU sweep
    cached.py        hit / append / miss -- indexing only what is not cached
    layoutio.py      the proven layout, serialised so an append can reuse it
  merge/       layer 3: k-way merge over the indexes, Qt-free
    mergeindex.py    checkpointed cursors, block cache, random access by row
  session/     layer 4: what the UI asks for, still Qt-free
    session.py       open files, rebuild the merge, hand out decoded rows
    prepare.py       warm the cache in worker processes, before opening
    rowformat.py     copy and export formatting, with attribution (D9)
    palette.py       severity ramp and source-lane colours
  ui/          layer 5: the only layer allowed to import Qt
    mergedview.py    QAbstractScrollArea, custom paint, int64 row space
    mainwindow.py    drag-and-drop, source dock, status reporting
    indexing.py      the worker thread that keeps the window answering
    theme.py         fonts, chrome colours, stylesheet
    app.py           QApplication entry point
  highlight/   layer 5: M4
tools/
  gen_corpus.py      corpus + ground truth generator, with ground truth
  make_test_logs.py  realistic logs to try the GUI on, written to C:/temp
  bench.py           benchmark harness and regression gate (no Qt, by test)
  bench_ui.py        the paint budget; the one tool that needs Qt
  _prototypes.py     M0 kernels kept only as reference rows in the gate
  bench_baseline.json
tests/
  conftest.py        corpus fixture and an in-memory LogSource stub
  test_corpus.py     the ground truth is internally consistent
  test_prototypes.py the M0 kernels match the ground truth
  test_architecture.py  SPEC §1: engine layers stay Qt-free
  unit/              the shipped engine modules, against ground truth
  ui/                the Qt shell; skipped entirely when Qt is absent
.github/workflows/ci.yml
```

### The corpus

`tools/gen_corpus.py` writes log files *together with their exact ground truth* —
every entry's `wall_us`, its 1-based line number in its own file, its byte offset,
its severity class, and for a scenario the exact expected merged order. Nothing
downstream is ever checked against itself.

Three scenarios, ~1.4MB, generated in under a second:

- **basic** — three files over one overlapping window, with a merged-order manifest
- **formats** — one file per supported timestamp format (15 of them)
- **edge** — empty file, no timestamps, leading banner, unterminated final line, a
  1MB single line, out-of-order and duplicate timestamps, New Year and midnight
  rollover, mixed CRLF/LF/lone-CR, UTF-8/BOM/UTF-16LE/CP1252/invalid bytes, CSV
  with a quoted newline, JSONL with varying key order, and a file whose severity
  word sits at a varying offset

Plus performance tiers from 1KB to 10GB, generated on demand with sampled truth.

---

## Design decisions worth knowing

Full rationale in [`docs/PRD.md` §5](docs/PRD.md); the ones that most shape the code:

- **PySide6, not PyQt6** — LGPL, so it can ship in a closed binary.
- **No Qt below the UI layer.** Enforced by `tests/test_architecture.py` and by a
  CI job that installs no Qt at all. Keeps the engine headlessly testable and keeps
  Qt out of the indexing worker processes.
- **Naive local time only.** Simple and fast; the accepted cost is that logs from
  different timezones interleave wrongly, mitigated by a per-file ± skew offset.
- **The merge order is never materialised** — checkpointed cursors instead.
- **Copy and export attribute every row** to its source file and original line
  number (`app.log:1`, `SOA.log:7`).
- **Windows files are opened with `FILE_SHARE_DELETE`**, which Python's `open()`
  does not request — without it, merely following a log would break the writing
  application's own log rotation.

---

## Licence

MIT — see [`LICENSE`](LICENSE). [`NOTICE`](NOTICE) credits logmerger (MIT) for the
timestamp-format catalogue and records Qt's LGPL terms.
