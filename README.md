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

Requires Python 3.12+. The engine needs only NumPy; Qt arrives with M3.

```bash
python -m pip install "numpy>=2.0" "pytest>=8.0"

python tools/gen_corpus.py --out tests/corpus     # generate corpus + ground truth
python -m pytest tests -q                         # run the suite
python tools/bench.py                             # measure the kernels
```

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
| **M1** | Format detection, vectorised extraction, `FileIndex`, headless | ⬜ next |
| **M2** | Sidecar cache, parallel indexer, checkpointed `MergeIndex` | ⬜ |
| **M3** | Qt shell: merged view, scrolling, drag-and-drop | ⬜ |
| **M4** | Highlighting, severity classes, source lanes, aliases | ⬜ |
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

## Layout

```
docs/          PRD.md, SPEC.md
logweave/      the application (M1 onward)
tools/
  gen_corpus.py      corpus + ground truth generator
  bench.py           benchmark harness and regression gate
  _prototypes.py     candidate kernels, measured before M1 commits to them
  bench_baseline.json
tests/
  conftest.py        session-scoped corpus fixture
  test_corpus.py     the ground truth is internally consistent
  test_prototypes.py the kernels match the ground truth
  test_architecture.py  SPEC §1: engine layers stay Qt-free
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
