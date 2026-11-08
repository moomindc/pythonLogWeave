#!/usr/bin/env python3
"""
LogWeave benchmark harness  --  milestone M0.

Exists before the code it judges, on purpose. PRD risk R1 is that Python cannot
reach NFR-3's >=100 MB/s indexing throughput, and PRD D8 forbids native code
until a benchmark proves it necessary. This is that benchmark.

    python tools/bench.py                          # synthetic 64MB chunk
    python tools/bench.py --chunk-mb 8 --reps 1    # quick, for CI
    python tools/bench.py --file tests/corpus/perf-1gb/single.log
    python tools/bench.py --json out.json
    python tools/bench.py --gate                   # fail on regression vs baseline
    python tools/bench.py --update-baseline

The committed baseline lives in tools/bench_baseline.json. Shared CI runners are
far too noisy for a tight gate, so CI runs with a deliberately generous
tolerance and the real gate is the local one.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Pessimistic scaling factor for projecting the single-core kernel figures onto
# the process pool of SPEC 5.3. Chunks and files are independent, so the real
# figure is closer to linear, but memory bandwidth is shared.
PARALLEL_EFFICIENCY = 0.70

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The shipped modules. From M1 the gate measures the code that actually runs;
# before that it measured prototypes, which is no longer honest now that real
# ones exist.
# Still only in the prototype, and deliberately so:
#   * extract_fixed_iso    -- M0's hardcoded-ISO kernel, kept as a reference row
#                             so the cost of the shipped kernel's generality and
#                             field validation stays visible instead of becoming
#                             the new normal.
#   * extract_scalar_strptime -- what logmerger does today. There is no shipped
#                             equivalent and there should not be.
#   * histogram/cumulative/strip -- M0's prototypes, kept as reference rows
#                             beside the shipped `index.histogram` ones. They
#                             run at the million buckets SPEC 8.1 originally
#                             specified, which is exactly the measurement that
#                             justified capping the shipped version at 65536:
#                             the prototype's `build_cumulative` row is the
#                             7.4ms repaint the cap and the prefix sum exist to
#                             remove.
from _prototypes import (  # noqa: E402
    build_cumulative,
    extract_fixed_iso,
    extract_scalar_strptime,
    histogram,
    strip_aggregate,
)

from logweave.index import extract as _ex  # noqa: E402
from logweave.index.detect import detect  # noqa: E402
from logweave.index.fileindex import FileIndex  # noqa: E402
from logweave.index.histogram import MAX_BUCKETS, Histogram  # noqa: E402
from logweave.index.lines import group_entries, split_lines  # noqa: E402
from logweave.index.severity import (  # noqa: E402
    severity_at_offset,
    severity_scan,
)
from logweave.merge import MergeIndex, merge_of  # noqa: E402


def fake_index(ts: np.ndarray) -> FileIndex:
    """
    A `FileIndex` carrying only what the merge reads.

    The merge benches run on synthetic timestamp arrays rather than real indexed
    files, so that row counts can be set independently of how much log text
    would be needed to produce them -- two million rows is the interesting scale
    and generating two million real lines per run is not.
    """
    n = int(ts.size)
    return FileIndex(
        ts=np.ascontiguousarray(ts, dtype=np.int64),
        off=np.arange(n + 1, dtype=np.uint64) * 100,
        flags=np.zeros(n, dtype=np.uint8),
        line_no=np.arange(n, dtype=np.uint32) + 1,
    )

SEV_WORDS = ["INFO", "INFO", "INFO", "DEBUG", "INFO", "WARN", "INFO",
             "DEBUG", "ERROR", "INFO", "TRACE", "INFO", "WARN", "INFO",
             "INFO", "NOTICE"]
MSGS = ["request accepted", "pool resized 32 -> 64", "upstream timeout after 30s",
        "retry 1/3 scheduled", "flushed 240 records", "slow query took 2841ms",
        "worker 7 started", "connection pool exhausted", "checkpoint written",
        "resolved upstream to 10.2.0.14:8443", "heartbeat ack in 4ms"]


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------

def synth_chunk(target_bytes: int) -> bytes:
    """A chunk in the fixed-position ISO format the fast path targets."""
    out = bytearray()
    us = 1_791_500_000_000_000
    i = 0
    while len(out) < target_bytes:
        us += 97_000
        secs, frac = divmod(us, 1_000_000)
        days, rem = divmod(secs, 86400)
        # cheap civil date for a fixed reference day; exact values are irrelevant
        # to throughput, only the byte layout is
        y, mo = 2026, 10
        d = 4 + (days % 20)
        h, r = divmod(rem, 3600)
        mi, s = divmod(r, 60)
        out += (f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}.{frac:06d} "
                f"{SEV_WORDS[i & 15]} [{i:09d}] {MSGS[i % len(MSGS)]}\n").encode("ascii")
        i += 1
    return bytes(out)


def load_chunk(path: Path | None, mb: int) -> tuple[bytes, str]:
    target = mb << 20
    if path is None:
        return synth_chunk(target), f"synthetic {mb}MB"
    with path.open("rb") as fh:
        data = fh.read(target)
    cut = data.rfind(b"\n")
    if cut > 0:
        data = data[:cut + 1]
    return data, f"{path.name} first {len(data) / (1 << 20):.1f}MB"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

@dataclass
class Result:
    name: str
    unit: str
    value: float
    seconds: float
    higher_is_better: bool = True
    note: str = ""


@dataclass
class Bench:
    results: list[Result] = field(default_factory=list)
    reps: int = 3

    def time(self, fn, reps: int | None = None) -> float:
        n = reps or self.reps
        best = float("inf")
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    def throughput(self, name: str, fn, nbytes: int, *, reps=None, note=""):
        s = self.time(fn, reps)
        self.results.append(Result(name, "MB/s", nbytes / (1 << 20) / s, s, True, note))

    def rowrate(self, name: str, fn, nrows: int, *, reps=None, note=""):
        s = self.time(fn, reps)
        self.results.append(Result(name, "Mrow/s", nrows / 1e6 / s, s, True, note))

    def latency(self, name: str, fn, calls: int, *, reps=None, note=""):
        s = self.time(fn, reps)
        self.results.append(Result(name, "us/call", s / calls * 1e6, s, False, note))


# ---------------------------------------------------------------------------
# benchmarks
# ---------------------------------------------------------------------------

SCALAR_CAP = 150_000     # scalar paths are slow; cap and extrapolate


def run_index_benches(b: Bench, chunk: bytes) -> dict:
    nbytes = len(chunk)
    buf = np.frombuffer(chunk, np.uint8)

    # lone_cr=False measures the single-pass fast path; the lone-CR variant is
    # measured separately so the cost of FR-6's secondary scan is visible.
    b.throughput("split_lines", lambda: split_lines(buf, lone_cr=False), nbytes)
    b.throughput("split_lines_lone_cr", lambda: split_lines(buf, lone_cr=True), nbytes,
                 note="FR-6 secondary scan")
    starts, lens = split_lines(buf, lone_cr=False)
    n = starts.size

    # Detection runs once per file, so it is not on the throughput path -- but it
    # decides which path everything else takes, so its own cost is reported and
    # the layout it produces is what the kernels below are measured against.
    b.latency("detect_format", lambda: detect(chunk, buf, starts, lens), 1,
              note="once per file, over the whole bench chunk; the indexer "
                   "samples only DETECT_BYTES, so this is an upper bound")
    det = detect(chunk, buf, starts, lens)
    if det.layout is None:
        raise SystemExit(
            f"benchmark chunk did not reach the vectorised path: {det.describe()}\n"
            f"notes: {det.notes}"
        )

    b.throughput("extract_vectorised", lambda: _ex.extract(buf, starts, lens, det.layout),
                 nbytes, note=f"SPEC 4.4 fast path, {det.layout.describe()}")
    res = _ex.extract(buf, starts, lens, det.layout)
    valid = res.valid
    b.rowrate("extract_vectorised_rows",
              lambda: _ex.extract(buf, starts, lens, det.layout), n)

    # M0's hardcoded-ISO kernel, for reference only. The shipped kernel is
    # layout-driven and range-validates its fields, which costs ~17%; keeping
    # this row means that trade stays visible rather than becoming invisible.
    b.throughput("extract_vectorised_m0_hardcoded",
                 lambda: extract_fixed_iso(buf, starts, lens), nbytes,
                 note="M0 prototype, one format, no field validation")

    # Scalar comparisons, on a capped slice, reported at the same scale.
    cap = min(SCALAR_CAP, n)
    cap_starts = starts[:cap]
    cap_lens = lens[:cap]
    cap_bytes = int(starts[cap] if cap < n else nbytes)
    b.throughput("extract_scalar_regex",
                 lambda: _ex.extract_scalar(chunk, cap_starts, cap_lens,
                                            det.fmt, det.pattern),
                 cap_bytes, reps=1,
                 note=f"shipped fallback + oracle, {cap} lines extrapolated")
    b.throughput("extract_scalar_strptime",
                 lambda: extract_scalar_strptime(chunk, cap_starts), cap_bytes, reps=1,
                 note="what logmerger does today")

    # Severity: the fixed-offset fast path versus the general regex fallback.
    sev_off = det.severity_offset
    if sev_off is not None:
        b.throughput("severity_fixed_offset",
                     lambda: severity_at_offset(buf, starts, lens, sev_off), nbytes,
                     note=f"vectorised, level word at offset {sev_off}")
    b.throughput("severity_regex_insensitive",
                 lambda: severity_scan(chunk, starts, n), nbytes,
                 reps=1, note="fallback: regex over every byte (risk R11)")

    sev = (severity_at_offset(buf, starts, lens, sev_off) if sev_off is not None
           else severity_scan(chunk, starts, n))
    opens, _spans = group_entries(valid)
    ets = res.ts[opens]
    esev = sev[opens]
    nent = ets.size

    if nent > 1:
        t0 = int(ets[0])
        span = max(1, int(ets[-1]) - t0)
        nbk = max(1, min(1_000_000, span // 1_000_000 + 1))
        bk = max(1, span // nbk + 1)
        b.rowrate("histogram", lambda: histogram(ets, esev, t0, bk, nbk), nent)
        hist = histogram(ets, esev, t0, bk, nbk)
        b.latency("build_cumulative", lambda: build_cumulative(hist), 1,
                  note="once per append, not per repaint")
        cum = build_cumulative(hist)
        edges = np.linspace(0, nbk, 801).astype(np.int64)
        b.latency("strip_repaint_800px", lambda: strip_aggregate(cum, edges), 1,
                  note="SPEC 8.2/8.3; must fit inside a 16ms frame (NFR-5)")
        run_overview_benches(b, ets, esev)

    # The headline number: everything NFR-3 covers, in one pass.
    def full_pipeline():
        bb = np.frombuffer(chunk, np.uint8)
        st, ln = split_lines(bb, lone_cr=False)
        r = _ex.extract(bb, st, ln, det.layout)
        sv = (severity_at_offset(bb, st, ln, sev_off) if sev_off is not None
              else severity_scan(chunk, st, st.size))
        o, _ = group_entries(r.valid)
        return o.size, sv.size

    b.throughput("INDEX_TOTAL", full_pipeline, nbytes,
                 note="NFR-3 gate: split + extract + severity + entries")

    def full_pipeline_fallback():
        bb = np.frombuffer(chunk, np.uint8)
        st, ln = split_lines(bb, lone_cr=False)
        r = _ex.extract(bb, st, ln, det.layout)
        sv = severity_scan(chunk, st, st.size)
        o, _ = group_entries(r.valid)
        return o.size, sv.size

    b.throughput("INDEX_TOTAL_regex_fallback", full_pipeline_fallback, nbytes, reps=1,
                 note="worst case: unpredictable severity position")

    return {"lines": int(n), "entries": int(nent), "severity_offset": sev_off}


def run_overview_benches(b: Bench, ts: np.ndarray, sev: np.ndarray) -> None:
    """
    The shipped overview histogram -- SPEC 8.1, risk R3.

    The rows above measure M0's prototype at the million buckets SPEC 8.1
    originally asked for. These measure what ships, at the 65536-bucket cap,
    and the pair is the argument for the cap: the prototype's prefix sum is
    half a frame and the shipped one is a small fraction of one, for resolution
    that no display can show.

    `strip_repaint_shipped` is the number risk R3 turns on. It must stay far
    inside the 16 ms frame budget (NFR-5) *per strip*, because D4 ships two of
    them and they repaint together.
    """
    nent = ts.size
    b.rowrate("overview_histogram", lambda: Histogram.build(ts, sev), nent,
              note=f"shipped, <= {MAX_BUCKETS} buckets")

    hist = Histogram.build(ts, sev)
    b.latency("overview_prefix_sum", lambda: _fresh_cum(hist), 1,
              note="once per index or append, never in a repaint")

    _ = hist.cum
    edges = np.linspace(hist.t0, hist.t0 + hist.nbuckets * hist.bucket_us,
                        801).astype(np.int64)
    b.latency("strip_repaint_shipped", lambda: hist.range_counts(edges), 1,
              note="one strip, 800px; D4 paints two (risk R3)")

    print(f"  overview: {hist.nbuckets} buckets of {hist.bucket_us / 1e6:.0f}s, "
          f"{hist.nbytes() / 1e6:.2f} MB with the prefix sum "
          f"(SPEC 8.1 budget: a few MB a source)")


def _fresh_cum(hist: Histogram):
    """Force the prefix sum to be rebuilt, which is what an append costs."""
    hist.invalidate()
    return hist.cum


def run_merge_benches(b: Bench, rows: int, k: int, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    per = max(2, rows // k)
    arrays = []
    for _ in range(k):
        steps = rng.integers(1, 200_000, size=per, dtype=np.int64)
        arrays.append(np.cumsum(steps) + 1_791_500_000_000_000)
    total = sum(a.size for a in arrays)

    def fresh() -> MergeIndex:
        return merge_of([fake_index(a) for a in arrays])

    # Construction builds the checkpoints, so this measures SPEC 6.3 end to end.
    b.rowrate("merge_build_checkpoints", fresh, total, reps=1,
              note=f"SPEC 6.3, {k} sources x {per} rows")

    m = fresh()
    lo, hi = int(min(a[0] for a in arrays)), int(max(a[-1] for a in arrays))
    probes = rng.integers(lo, hi, size=2000, dtype=np.int64).tolist()

    b.latency("merge_time_to_row",
              lambda: [m.time_to_row(t) for t in probes], len(probes),
              note="NFR-6 seek; O(k log n), no replay")

    # Cold: clear the block cache before each probe, so every seek pays a full
    # replay from its preceding checkpoint. Rebuilding the index instead would
    # measure checkpoint construction, which is ~250ms and has nothing to do
    # with the cost of a seek.
    rowprobes = rng.integers(0, total, size=200, dtype=np.int64).tolist()

    def cold_seeks():
        for i in rowprobes:
            m.clear_cache()
            m.row_at(int(i))

    b.latency("merge_row_at_cold", cold_seeks, len(rowprobes),
              note="cold: full replay from the preceding checkpoint")

    # Warm: a working set small enough to stay cached, which is what a user
    # reading one region of the file actually does. Reusing the random probes
    # here would measure misses again -- 200 probes over 488 blocks miss a
    # 64-block cache almost every time, which is the cold case under a new name.
    nearby = (rng.integers(0, max(1, total - 60), size=8, dtype=np.int64).tolist() * 25)
    for i in nearby[:8]:
        m.row_at(int(i))

    b.latency("merge_row_at_warm",
              lambda: [m.row_at(int(i)) for i in nearby], len(nearby),
              note="warm: working set resident in the block cache")

    blocks = rng.integers(0, max(1, total - 4096), size=200, dtype=np.int64).tolist()
    b.latency("merge_block_4096",
              lambda: [m.rows(int(i), 4096) for i in blocks], len(blocks),
              note="one block replay; the cache absorbs repeats")

    # The number that actually decides whether scrolling is smooth: 60 rows at a
    # time, walking forward, which is what a real scroll does.
    seq = list(range(0, min(total - 60, 120_000), 60))
    scroll = fresh()

    def scroll_pass():
        for s in seq:
            scroll.rows(s, 60)

    b.latency("merge_paint_60rows_scrolling", scroll_pass, len(seq), reps=1,
              note="NFR-5: one frame of rows, sequential; must fit 16ms")

    starts = rng.integers(0, max(1, total - 64), size=2000, dtype=np.int64).tolist()
    b.latency("merge_paint_60rows_random",
              lambda: [m.rows(int(i), 60) for i in starts], len(starts),
              note="NFR-5: worst case, jumping around")

    cp = m.checkpoint_bytes
    b.results.append(Result("checkpoint_bytes_per_1M_rows", "MB/1M",
                            m.stats.mb_per_1m_rows, 0.0, False,
                            f"vs 8 MB/1M if materialised ({cp / 1e6:.2f}MB total)"))
    b.results.append(Result("merge_cache_hit_rate_scrolling", "ratio",
                            scroll.stats.hit_rate, 0.0, True,
                            "SPEC 6.2 block cache, during a sequential scroll"))


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def run_cache_benches(b: Bench, chunk: bytes) -> None:
    """
    The sidecar cache -- NFR-4 (SPEC 5.4).

    Measured on a real file, because the whole claim is about what the OS does
    with a page-aligned mapping and the thing being measured is *not reading*.
    A warm reopen's cost is per file -- a stat, a 64 KB hash, a memmap -- so it
    is reported as a latency and not a throughput: the figure that matters is
    milliseconds per file, and it should not move when the file gets bigger.
    """
    from logweave.index.cached import open_index
    from logweave.index.civil import epoch_us_to_wall_us
    from logweave.source.file import FileSource

    with tempfile.TemporaryDirectory(prefix="logweave-bench-") as tmp:
        work = Path(tmp)
        log = work / "bench.log"
        log.write_bytes(chunk)
        root = work / "cache"
        mtime = epoch_us_to_wall_us(int(log.stat().st_mtime * 1_000_000))

        def once():
            src = FileSource(log)
            src.open()
            try:
                out = open_index(src, mtime_wall_us=mtime, path=log, root=root)
                out.index.release()
                return out
            finally:
                src.close()

        t0 = time.perf_counter()
        cold = once()
        cold_s = time.perf_counter() - t0
        if cold.status != "miss":
            raise SystemExit(f"cache bench did not start cold: {cold.status}")

        # Once only, and not best-of-three: the second run would be a hit.
        b.results.append(Result(
            "cache_first_index", "MB/s",
            len(chunk) / (1 << 20) / cold_s, cold_s, True,
            note=f"one run, cold; {cold.index.n:,} entries, includes writing "
                 f"the sidecar"))

        warm = once()
        if warm.status != "hit":
            raise SystemExit(f"cache bench did not warm: {warm.status}")

        # One call per rep, and the best of them: a warm open is a few
        # milliseconds, so an average would mostly report the scheduler.
        b.latency("cache_warm_reopen", once, 1,
                  note="NFR-4: per file, not per byte; budget 2.0s for a "
                       "whole 10GB set")

        size = sum(p.stat().st_size for p in root.glob("*.lwi"))
        # The histogram is in the sidecar from M5 (SPEC 8.1) and is a fixed
        # cost rather than a per-entry one, so it is subtracted out before the
        # per-entry figure is reported and stated separately. Left in, this row
        # would rise or fall with the span of the log rather than with its
        # entries, and the number would stop meaning what its name says.
        overview = warm.index.hist.counts.nbytes if warm.index.hist else 0
        b.results.append(Result(
            "cache_bytes_per_entry", "B/entry",
            (size - overview) / max(1, warm.index.n), 0.0, False,
            note=f"off+ts+flags+line_no; {size / (1 << 20):.1f}MB sidecar "
                 f"including {overview / (1 << 20):.1f}MB of overview histogram"))


def report(results: list[Result], baseline: dict | None, tolerance: float) -> list[str]:
    name_w = max(len(r.name) for r in results) + 1
    print(f"\n{'benchmark':<{name_w}} {'value':>12} {'unit':<9} {'time':>9}  "
          f"{'vs base':>9}  note")
    print("-" * (name_w + 56))
    failures = []
    for r in results:
        base = (baseline or {}).get(r.name, {}).get("value")
        if base:
            ratio = r.value / base if r.higher_is_better else base / r.value
            delta = f"{(ratio - 1) * 100:+7.1f}%"
            if ratio < 1 - tolerance:
                delta += " !"
                failures.append(f"{r.name}: {r.value:.3g} vs baseline {base:.3g} {r.unit}")
        else:
            delta = "       --"
        print(f"{r.name:<{name_w}} {r.value:>12.4g} {r.unit:<9} {r.seconds:>8.3f}s  "
              f"{delta:>9}  {r.note}")
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LogWeave benchmark harness")
    ap.add_argument("--file", type=Path, default=None, help="read the chunk from a real log")
    ap.add_argument("--chunk-mb", type=int, default=64)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--merge-rows", type=int, default=2_000_000)
    ap.add_argument("--merge-sources", type=int, default=8)
    ap.add_argument("--skip-merge", action="store_true")
    ap.add_argument("--skip-cache", action="store_true",
                    help="skip the sidecar cache benches, which write to a "
                         "temporary directory")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--baseline", type=Path,
                    default=Path(__file__).resolve().parent / "bench_baseline.json")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--gate", action="store_true", help="exit non-zero on regression")
    ap.add_argument("--tolerance", type=float, default=0.25)
    ap.add_argument("--cores", type=int, default=0,
                    help="cores to project the aggregate over (default: detected)")
    args = ap.parse_args(argv)

    chunk, source = load_chunk(args.file, args.chunk_mb)
    print(f"LogWeave bench  --  {source}")
    print(f"python {platform.python_version()}  numpy {np.__version__}  "
          f"{platform.system()} {platform.machine()}  {os.cpu_count()} cores")
    print(f"{platform.processor() or 'unknown cpu'}")
    print("note: run-to-run variation of 15-20% is normal on a laptop under "
          "thermal load;\n      compare components within one run, not across runs.")

    b = Bench(reps=args.reps)
    info = run_index_benches(b, chunk)
    print(f"\n{info['lines']} lines, {info['entries']} entries in "
          f"{len(chunk) / (1 << 20):.1f}MB "
          f"({len(chunk) / max(1, info['lines']):.0f} bytes/line)")
    if not args.skip_merge:
        run_merge_benches(b, args.merge_rows, args.merge_sources)
    if not args.skip_cache:
        run_cache_benches(b, chunk)

    # A baseline is only comparable to a run of the same shape. Several
    # benchmarks (histogram, build_cumulative) scale with bucket count rather
    # than bytes, so comparing a 1MB run against a 64MB baseline reports
    # regressions that are not regressions.
    baseline = None
    if args.baseline.exists() and not args.update_baseline:
        doc = json.loads(args.baseline.read_text(encoding="utf-8"))
        same_workload = (
            doc.get("chunk_bytes") == len(chunk)
            and doc.get("merge_rows") == args.merge_rows
            and doc.get("merge_sources") == args.merge_sources
        )
        if same_workload:
            baseline = doc.get("results")
        else:
            print(f"\nbaseline NOT compared: recorded for a different workload "
                  f"({doc.get('chunk_bytes', 0) / (1 << 20):.0f}MB chunk, "
                  f"{doc.get('merge_rows')} merge rows, "
                  f"{doc.get('merge_sources')} sources).\n"
                  f"  to compare, rerun with --chunk-mb "
                  f"{doc.get('chunk_bytes', 0) >> 20} --merge-rows "
                  f"{doc.get('merge_rows')} --merge-sources "
                  f"{doc.get('merge_sources')}")

    failures = report(b.results, baseline, args.tolerance)

    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "platform": f"{platform.system()} {platform.machine()}",
        "cores": os.cpu_count(),
        "chunk_bytes": len(chunk),
        "merge_rows": args.merge_rows,
        "merge_sources": args.merge_sources,
        "lines": info["lines"],
        "entries": info["entries"],
        "severity_offset": info["severity_offset"],
        "results": {r.name: {"value": r.value, "unit": r.unit,
                             "higher_is_better": r.higher_is_better}
                    for r in b.results},
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"\nwrote {args.json}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"\nbaseline updated -> {args.baseline}")

    total = next((r for r in b.results if r.name == "INDEX_TOTAL"), None)
    if total:
        # NFR-3 is an AGGREGATE figure: ">=100 MB/s aggregate, parallel across
        # files". These kernels are single-threaded, and SPEC 5.3 indexes chunks
        # and files concurrently in a process pool, so the single-core number is
        # projected across cores at a deliberately pessimistic efficiency.
        cores = args.cores or (os.cpu_count() or 1)
        projected = total.value * cores * PARALLEL_EFFICIENCY
        verdict = "PASS" if projected >= 100 else "FAIL"
        print(f"\nsingle-core index throughput : {total.value:.1f} MB/s")
        print(f"projected aggregate          : {projected:.0f} MB/s "
              f"({cores} cores x {PARALLEL_EFFICIENCY:.0%} efficiency)")
        print(f"NFR-3 (>=100 MB/s aggregate) : [{verdict}]")
        print(f"1GB single-core              : {1024 / total.value:.1f}s")
        fallback = next((r for r in b.results
                         if r.name == "INDEX_TOTAL_regex_fallback"), None)
        if fallback:
            fb = fallback.value * cores * PARALLEL_EFFICIENCY
            print(f"worst case (regex severity)  : {fallback.value:.1f} MB/s single-core, "
                  f"{fb:.0f} MB/s aggregate  "
                  f"[{'PASS' if fb >= 100 else 'FAIL'}]")
        if args.gate and projected < 100:
            return 1

    if failures:
        print(f"\n{len(failures)} regression(s) beyond {args.tolerance:.0%}:")
        for f in failures:
            print(f"  - {f}")
        if args.gate:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
