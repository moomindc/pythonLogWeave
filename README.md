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
python -m pytest                                  # the suite: 744 tests
python -m pytest --ignore=tests/ui                # the engine only, no Qt needed
python -m ruff check .                            # lint
python tools/bench.py                             # engine kernels
python tools/bench_ui.py                          # the paint budget, needs a real window
```

The corpus is generated, never committed, so a stale or hand-edited one cannot
make a test pass. The GUI tests skip themselves entirely when Qt is absent,
which is what lets the engine CI job install numpy and pytest and nothing else;
`tests/test_architecture.py` is what stops Qt creeping below the UI layer.

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
| Ctrl+Shift+C | Copy the same rows verbatim — no attribution, no timestamp column |
| Ctrl+A | Select all (a copy past 200 000 rows is refused rather than truncated) |
| Ctrl+T | Hide the timestamp column — see below |
| Ctrl+M / Ctrl+L | Collapse the minimap / the timeline — see below |
| Ctrl++ / Ctrl+- / Ctrl+0 | Text size, also Ctrl+wheel; remembered between runs |
| Arrows, PgUp/PgDn, Home/End | Move; hold Shift to extend the selection |
| Scrollbar at the bottom | Scrolls the message only; the line number, timestamp, alias and lanes stay put |
| View ▸ Source Alias Column | The short name of each row's file, as words rather than a colour |
| View ▸ Line Number Column | Each entry's line number *within its own file*, in a gutter on the left |
| View ▸ Lane Width | 4–12px per lane |
| Right edge | Two overview strips: the minimap, then the timeline. Click or drag either to go there — see below |
| View ▸ Highlighting | Which of the shipped rule sets are in force — see below |
| Double-click a dock row | Rename that source; clear the name to go back to the default |
| File ▸ Clear Index Cache | Delete the saved indexes — see below |

**The index cache.** The first open of a large log has to read all of it; after
that the index is saved beside the cache and reopening the same file costs about
7 ms however big it is. A file that has merely *grown* since — a production log
you keep coming back to — only has its new tail read. Nothing is reused unless
the file's head still hashes the same, so a rotated log is re-indexed rather
than mis-read.

The saved indexes cost about 21 bytes an entry, plus a few megabytes for the
overview histogram the two strips are drawn from, and live in
`%LOCALAPPDATA%\LogWeave\index` (`$XDG_CACHE_HOME/logweave/index` on Linux,
`~/Library/Caches/LogWeave/index` on macOS) — never beside the log, which is
often read-only or on a share. They are capped and swept oldest-used-first, and
**File ▸ Clear Index Cache** deletes them and reports what it freed. Files
already open keep working.

Indexing happens in worker processes while the window stays usable, with
progress and a Cancel in the status bar. Cancelling keeps whatever had already
finished — those files are in the cache, so they cost nothing to open.

**Copying.** Two modes, because they are wanted for different things. **Ctrl+C**
carries the file and line each row came from — `app.log:1`, `SOA.log:7` — which
is the whole reason to merge files in the first place, and is evidence when it is
pasted into a ticket. **Ctrl+Shift+C** gives the lines exactly as they appear in
their own files, which is what you want when pasting into a parser, a diff, or a
search box that would choke on the prefix. The attribution names the *file*, not
the alias, so a renamed source is still cited by something a colleague can find.

**Text size.** Ctrl+wheel, Ctrl+plus and Ctrl+minus between 6 and 32 point, and
the size is remembered — set it once when you move the window to a different
monitor, not once a session. Ctrl+0 returns to the shipped 10pt. Zooming keeps
the row you were reading at the top of the window rather than the scroll
position, because fewer rows fit at a larger size.

A highlight rule may ask for bold or italic, and LogWeave uses the face **only
if it does not change the character advance**. Spans are positioned
arithmetically from one glyph width — the single reason painting is independent
of line length — so a bold face a fraction wider would displace every span after
the first by a growing fraction of a character. Most monospace families keep the
advance; the ones that do not get the colour and no weight.

**Highlighting.** Three rule sets ship. *Severity* colours the level words.
*Shapes* picks out the things a stack trace is made of — the exception class
name, Java and Python frames, `Caused by:` — as spans inside the line rather
than colouring the whole row. *Syslog* reads `<0>`–`<7>` priorities and is off
by default, because that prefix is rare outside a raw capture and a rule that
never fires still costs a branch in the one regex every row is scanned with.
Which sets are on is remembered.

All the enabled rules compile into **one** alternation regex, so a row is
scanned once however many rules there are, and the result is memoised by the
row's text. Colouring an ordinary file is lost in the run-to-run noise; a file
that is all stack traces, six highlighted spans to a row, costs about 3 ms a
frame (see M4 below).

**The two overview strips.** On the right, outside the scrollbar, and both on
by default. They look similar and answer opposite questions.

The **minimap** is the inner one, and its axis is *rows*: every pixel covers
the same share of the merged stream, so it is a scrollbar you can read. Drag it
and you move linearly through the log; the lighter box on it is where the
viewport currently is.

The **timeline** is the outer one, and its axis is *wall-clock time* across the
whole merged span. An idle hour is an inch of blank strip; a burst is a dense
band. That is the thing the minimap physically cannot show — a row axis has no
rows in a gap with which to draw the gap. The bright line on it is where you
are. It seeks but deliberately does not drive the scrollbar, because a drag
down the timeline advances through rows at wildly uneven rates and a scrollbar
that behaved that way would be broken rather than informative.

Both are coloured by severity, and both are drawn from the same counts, so the
second one costs nothing to keep. **A class with any entries at all is given at
least two pixels**, taken from the widest quieter class beside it: one ERROR
among ten thousand INFO lines is a hundredth of a pixel by proportion, and
finding that one line is the entire reason to look at an overview. Hover either
strip for the time, the row and what is in that pixel.

Ctrl+M and Ctrl+L collapse them independently, and the choice is remembered.

**The line-number gutter.** Each entry's line number *within its own file*,
not its position in the merged view — which is a number about the viewer rather
than about the log. It sits on the left, before the timestamp, where an editor
gutter and `grep -n` put it. Right-aligned, so six figures line up with three
and the column reads as numbers. The value is always in the index and always
goes out with an annotated copy, so turning the column on reveals it rather than
enabling it.

**Which file a row came from.** Three answers, in increasing width. The lanes on
the right are a colour. **View ▸ Source Alias Column** adds the short name as
words, which is the one that survives a screenshot printed in grey. The alias
itself is the shortest *distinguishing* part of the path: one file is `app`, but
open `prod/service.log` beside `staging/service.log` and both grow a directory,
because `service` would tell you nothing. Double-click a dock row to rename a
source; a name you type is never recomputed when another file arrives.

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
| **M4** | Highlighting, severity classes, source lanes, aliases, text size, copy with and without attribution | ✅ **done** |
| **M5** | Minimap and timeline strips | ✅ **done** |
| **M6** | Follow engine; rotation matrix green | ⬜ next |
| **M7** | Search, filters, selection, copy, export | ⬜ |
| **M8** | Profiles, settings, file association — plus the highlight-rule editor and ruleset import/export that complete FR-31 | ⬜ |
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

## M4 — what it delivers and what it found

Highlighting (FR-30, FR-31), the alias and line-number columns (FR-29, FR-29a),
a configurable lane width (FR-28), a user-settable and remembered text size
(FR-38), and copy in two modes (D9, FR-46a).

The rule engine is in `logweave/highlight/`, which is **Qt-free and enforced as
such** — it is the fifth layer `tests/test_architecture.py` checks, although
SPEC §1 numbers it with the UI. The rules decide that a run of text is "error
red"; only the view knows what a `QColor` is. That split is what lets the whole
engine be tested headlessly, and what will let the same rules drive the HTML
export (FR-47) without the exporter importing a widget toolkit.

### Highlighting is free on ordinary lines and costs ~3 ms on stack traces

`python tools/bench_ui.py`, Cascadia Mono 10pt, 48 visible rows, four runs.
**These figures are the ones taken after bug 6 below** — everything measured
before it was drawn in a proportional 9pt UI face and is not comparable.

| Case | Rules off | Rules on | Delta | Spans a row |
|---|---|---|---|---|
| Ordinary lines | 10.85 ms | 10.94 ms | **+0.09 ms** | 0 |
| Long lines | 17.63 ms | 18.12 ms | **+0.46 ms** | 0 |
| Exception spans | 20.09 ms | 23.14 ms | **+3.09 ms** | 6 |

The ordinary-lines delta is **smaller than the run-to-run spread**, which was
−0.12 ms to +0.11 ms across the four runs — so the honest statement is that
highlighting an ordinary log is not measurable here, not that it costs 0.09 ms.

The scroll step — the number that actually decides whether the window feels
right — is **1.48 ms at worst, 11× under the 16 ms budget**, because a wheel
step still repaints three rows rather than a screenful.

**The exception-spans full repaint is over budget at 23.1 ms, and the honest
reading is that highlighting is not why.** It was already 20.1 ms with every
rule off. That is M3's measured breach — Qt's raster text at ~1.8 µs a glyph on
a full repaint of long lines (SPEC §14.1b) — and it dominates. A full repaint is
a seek or a resize, never a scroll. The fix is still SPEC §14.2 item 4, the glyph
atlas, which this makes slightly more urgent without changing what it is.

### The span cost was 4× larger before two changes

The first working version drew the whole line, then drew each styled span over
the top. It was correct, and it cost **four times** what the shipped version
does on the span-heavy case. Two changes closed it:

| Change | Span-heavy frame |
|---|---|
| Draw the line, then overdraw each span | +10.60 ms |
| Draw each character exactly once, as segments | +4.02 ms |
| …skip the background pass when no span has a background, and set the pen and font only when they change | **+2.56 ms** |

Those three absolute figures were taken **before** the painter-font fix, so they
are in the wrong face and only the ratios between them carry over; the shipped
version re-measures at +3.09 ms. Rerunning the two superseded implementations to
restate them in the right font would mean reimplementing both, which is not
worth it for a number whose conclusion does not change.

Overdrawing was the big one, and it is the same lesson as M3: the cost is per
*glyph*. Painting a 200-character line with six spans over it drew roughly 240
glyphs where 200 were needed. Cutting the row into runs that cover the visible
window exactly once draws 200.

### One regex, one pass — and what that forbids

All the enabled rules compile into one alternation with generated group names, so
a row is scanned once however many rules there are, and the styled result is
memoised by text (1024 rows). Per-rule case sensitivity rides along as a scoped
inline flag, `(?i:...)`, rather than a second pattern.

The consequence is a real limitation and it is worth stating plainly:
**`finditer` over one alternation returns non-overlapping matches, so a span
nested inside another span cannot both match.** Whichever alternative matches at
the earliest position consumes the text, and there is no second pass to recover
the inner one. Two passes would be the fix, and two passes is the cost this
design exists to avoid, so `tests/unit/test_highlight.py` asserts the behaviour
rather than wishing it away.

That makes *which* alternative wins at a given position the whole of overlap
resolution, so the alternatives are emitted in `(-priority, list order)` order at
compile time. SPEC §10.1 said "overlaps resolve by priority, then by rule
order", and before this they resolved by rule order alone — `priority` was being
read for the line-scope winner and ignored for spans.

### Five bugs found by building it

1. **The line-scope winner sentinel compared backwards.** The "nothing has won
   yet" value was `(-(1<<30), 1<<30)` where the comparison wanted
   `(1<<30, 1<<30)`, so no rule ever beat it and no row was ever styled by a line
   rule. The tests that would have caught it were the ones asserting a
   *particular* rule won, not that any did.
2. **The built-in severity rules contributed nothing to the index.** They were
   regexes, for the word boundary; the vectorised index-time classifier (SPEC
   §10.3) compares fixed byte columns and skips regexes by necessity. So the
   paint was right and the minimap would have been uncoloured. Fixed by giving a
   rule a `word_boundary` flag, so the shipped severity rules stay literals and
   still do not match `DEBUGGER` or `INFORMATIONAL`.
3. **A rename edited the row's entry count.** `QListWidgetItem` keeps
   `DisplayRole` and `EditRole` in the same place, so double-clicking "app 504"
   handed all of that to the editor and whatever was left behind became the name.
   A `QStyledItemDelegate` is the supported way to separate them.
4. **The lane-width menu could not select the shipped width.** It offered 4, 6,
   8, 10 and 12 px against a default of 7, so nothing was ticked when the window
   opened and the default was unreachable from the menu. Found by a test
   asserting that exactly one item is ticked; the menu now offers every width in
   the range.
5. **The alias algorithm grew every name to full depth** chasing two paths it
   could never separate. The same file opened twice never distinguishes however
   deep you look, so `app` and `app` became `var/log/app` and `var/log/app~2`,
   widening the column for every other source to no purpose. It now takes the
   shallowest depth that tells as many of them apart as any depth can, and falls
   back to a numeric suffix for the genuinely indistinguishable. A name the user
   has typed is never recomputed, but it does take part in deciding how much of
   the path the automatic ones need to show.

### Two more found in use, which the tests could not see

Both reported after the suite was green, and worth recording because of *why* it
was green.

6. **The merged view was drawing in the wrong font entirely.** A `QPainter`
   constructed on a widget does not inherit that widget's font — it starts at
   the application default, a 9pt proportional UI face on Windows. So every
   message was drawn proportionally while every width in `paintEvent` was
   computed from the monospace metrics, and a zoom grew the row height and the
   character cell without changing a single glyph. That last part is how it
   surfaced: *"ctrl + just makes the gaps around the text bigger but the font
   size does not increase."*

   There was a second half. A scroll area's viewport inherits the font when it
   is created and then stops — with a style sheet on the window, Qt treats the
   viewport's font as its own and later propagation is ignored — so
   `self.setFont()` reached nothing that paints. Both are now explicit.

   **The GUI suite could not have caught this**, and that is the lesson rather
   than an excuse. The offscreen platform has no font database: every family
   resolves empty and every glyph is 13×13 whatever the point size, which is
   exactly why §14.1b moved all text measurement to `tools/bench_ui.py` and a
   real window. The fix is tested by spying on the painter — asserting it is
   handed a font at the current point size before anything is drawn — which
   works on any backend, and the ink was verified against the real font stack
   separately: painted width now tracks `len(text) × char_w` to within 2% at
   10, 16 and 24pt, having been invariant before.

7. **The line-number column was built on the right**, beside the alias, because
   both were specified together as things that say where a row came from. They
   are not the same kind of thing: the alias answers *which file*, the line
   number is a coordinate within the row, and it belongs at the start of it
   where an editor gutter puts it. Moved to the left, before the timestamp.

   Every test still passed after the move, which is the tell: they asserted the
   column took width *from the message*, true on either side. They now assert
   the side.

### Preferences, and keeping the real registry out of it

Text size, lane width, the two columns, the timestamp column and which rule sets
are on persist through `QSettings`. Everything read back is clamped or
intersected with what exists: a hand-edited store cannot produce a 900-point
font, and a rule set removed in a later version cannot come back as a name
nothing answers to.

`QSettings` never fails — it quietly returns whatever somebody else put there —
so the GUI suite points it at a temporary ini file per test. Without that, a test
could be handed a font size left behind by the last run, and *running the tests
would change the application's appearance for the person who ran them*. It is the
same failure the sidecar cache had at M2b, in a place that is easier to miss.

---

## M5 — what it delivers and what it found

Both overview strips (D4, FR-32, FR-33), either collapsible and remembered
(FR-34), fed by one density histogram built during indexing and cached in the
sidecar (SPEC §8.1).

The two strips answer different questions and neither can answer the other's,
which is the whole argument for shipping both:

- The **minimap** is row-proportional — a scrollbar you can read. Every pixel
  covers the same share of the merged rows, so dragging it moves linearly
  through the stream, and it draws a box where the viewport currently is.
- The **timeline** is time-proportional. An idle hour is an inch of blank
  strip and a burst is a dense band. That is the one thing a row axis cannot
  show: there are no rows in a gap to draw the gap with.

Both are drawn from the same `uint32[buckets, 8]` of entries per time bucket
per severity class. The second strip costs one extra array of H times, no
second data structure and no second indexing pass — which is how risk R3, "two
minimap render paths become the frame-budget bottleneck", was meant to be
contained. It is contained, but not where the spec was looking.

### The strips are 4× under budget, and the cost was never the aggregation

`python tools/bench.py` and `python tools/bench_ui.py`, same machine and method
as M3 and M4. Strips measured at 844 px, which is what a 1080p screen allows.

| What | Cost | Against |
|---|---|---|
| Build the histogram | 38.2 M entries/s | ~3% of the indexing budget |
| Build the prefix sum | 3.24 ms | once per index or append |
| Range-sum one strip, 800 px | **83 µs** | SPEC §8's claim, confirmed |
| **Repaint both strips (a scroll)** | **0.63 ms** | **26× under** a 16 ms frame |
| **Re-aggregate both (a resize)** | **3.85 ms** | **4× under** |
| Histogram in memory, 64 MB chunk | 4.67 MB | "a few MB a source" |

SPEC §8 spent its care on the aggregation — the prefix sum exists so that a
repaint never re-runs a `cumsum`. That part was right, and the aggregation is
0.15 ms. But the first working version of the strips cost **5.65 ms each**, and
all of it was *after* the aggregation, in the code that turns counts into
pixels. Two strips at 11.3 ms left 4.7 ms of a frame for the view beside them.
R3 came in through a door the spec was not watching.

Two changes took it to 0.84 ms:

- **A pixel row is nine contiguous runs** — eight severity classes and the
  background — so `np.repeat` builds the whole row-major class map in one
  call. What it replaced ran eight broadcast comparisons over the entire
  buffer to work out which class owned each pixel: 1.2 ms for an answer
  already implicit in the widths it had just computed.
- **`np.take(..., out=buf)` writes colours straight into the destination.**
  Indexing a three-channel *view* of the RGBA buffer instead allocates the
  result and copies it across a stride of four: 1.5 ms for a slicing
  convenience.

Same lesson as M3's glyph cost and M4's span cost, from a third direction: in
this application the data structure is never what costs, the per-pixel work at
the end of it is.

### Two numbers in SPEC §8 were wrong, in opposite directions

**§8.1 asked for a million buckets and claimed "a few MB per source" in the
same paragraph.** Those are not compatible. A million buckets is 32 MB of
counts plus 64 MB of prefix sum — 96 MB a source, 770 MB across eight, against
NFR-7's 500 MB for the whole application. The cap is now 65,536, which costs
4.67 MB on a 64 MB chunk and loses nothing: the buckets exist to be aggregated
onto pixels, and 65,536 is 32× the height of a 2000-pixel strip. The resolution
being paid for could not be seen on any display.

**§8.3 asked for too few lookups, not too many.** It said a minimap pixel's row
boundaries are converted to times by "`row_to_time` on each boundary (a
checkpoint lookup)". Checkpoints sit 4096 rows apart, so a strip pixel only
contains one once the merge is over ~3.3M rows. Below that, the times between
them are interpolated at a constant rate — and a log with a genuine idle gap
*inside* one checkpoint block has that gap smeared across the rows around it.
Built that way, a 7000-row merge drew ten blank minimap pixels in the middle
and piled a thousand rows into the last one: an idle period that did not exist,
beside a burst that did not either.

The fix is a **fixed-size row-to-time anchor table**: 4096 anchors spread
evenly over the merged rows, read out of the merge window while
`build_checkpoints` already has it in hand. Fixed is the point — 32 KB whatever
the merge's size, where one anchor per *n* rows would be 64 MB at the row
ceiling. Against an 800-pixel strip every pixel holds about five anchors, and
for any merge under 4096 rows the stride is one and the table is exact.

### Four bugs found by building it

1. **`Signal(int)` is a 32-bit C++ int.** A `wall_us` is around 1.8×10¹⁵, six
   orders of magnitude past it. Every click on the timeline truncated the
   timestamp and seeked to row 0. The only symptom was a `RuntimeWarning` on
   stderr that a release build does not print — the seek simply did nothing,
   repeatably, in a way that reads like a geometry mistake.

   SPEC §9.1 already makes this exact argument about row numbers and
   `QModelIndex.row()`, which is why the view paints from an int64 row index.
   The argument had not been carried across to the signals, and
   `currentRowChanged` had the same latent defect waiting at the row ceiling.
   Both are now `Signal("qlonglong")`.

2. **A menu built before the window is shown reads `isVisible()` False** for a
   child that is perfectly visible — `setVisible(True)` does not make a widget
   visible until its ancestors are mapped. So both strips were drawn on screen
   and ticked *off* in the View menu from the moment the window opened. This is
   the M4 lane-width trap in a new place: a menu built from one source of truth
   and a widget set from another. `isVisibleTo(self)` asks the question that is
   actually being asked.

3. **The strip reported 6,996 of 7,000 rows.** A pixel's range is a half-open
   run of buckets `[lo, hi)`, and the last edge is placed at the last entry's
   own timestamp — which lands *inside* the bucket holding it, excluding that
   bucket. Four rows missing in seven thousand is invisible on a strip. It was
   caught by a test that summed the counts, which is now the first test in the
   file.

4. **The pixel buffer must outlive the `QImage` over it.** A `QImage` built on
   a buffer does not copy it and does not own it. Letting the NumPy array fall
   out of scope leaves the image pointing at freed memory — wrong pixels most
   of the time, an access violation the rest, and neither reproducible.

### Screenshot comparisons, without the screenshots

SPEC §15 asked for "a small set of screenshot comparisons" for the strips. They
are tested on their **exact pixels** instead, and that part of the suite runs in
the engine CI job with no Qt installed at all — because `session/overview.py`
renders a strip into an RGBA buffer with NumPy and `ui/strips.py` only blits it.

So a test asserts that a single ERROR among ten thousand INFO lines is visible,
at a known pixel, in the palette's error colour. By proportion that error is a
hundredth of a pixel wide; finding it is the one thing a user scans an overview
for, so any class with entries at all is guaranteed two pixels, funded by the
widest class *below* it so that severity order survives the pressure. A
similarity score against a reference image would have called that noise.

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
    histogram.py     the density histogram both overview strips are drawn from
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
    overview.py      the two strips as data, rendered to pixels without Qt
    aliasing.py      shortest distinguishing source names, for the column and copy
  highlight/   layer 5 by number, Qt-free by rule: the rules, not the colours
    rules.py         HighlightRule, Ruleset, the one-pass compiled scanner
    builtins.py      the three shipped rulesets: severity, shapes, syslog
  ui/          layer 5: the only layer allowed to import Qt
    mergedview.py    QAbstractScrollArea, custom paint, int64 row space
    strips.py        the minimap and timeline widgets: one blit and the mouse
    mainwindow.py    drag-and-drop, source dock, menus, status reporting
    indexing.py      the worker thread that keeps the window answering
    settings.py      the view preferences that outlive the window
    theme.py         fonts, chrome colours, stylesheet
    app.py           QApplication entry point
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
    conftest.py      points QSettings at a temp file, never the real store
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
