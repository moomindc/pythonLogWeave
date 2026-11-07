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
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Pessimistic scaling factor for projecting the single-core kernel figures onto
# the process pool of SPEC 5.3. Chunks and files are independent, so the real
# figure is closer to linear, but memory bandwidth is shared.
PARALLEL_EFFICIENCY = 0.70

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _prototypes import (  # noqa: E402
    SEV_RE_INSENSITIVE,
    SEV_RE_SENSITIVE,
    MergePrototype,
    build_cumulative,
    detect_severity_offset,
    entries_from,
    extract_fixed_iso,
    extract_scalar_regex,
    extract_scalar_strptime,
    histogram,
    severity_at_offset,
    severity_scan,
    split_lines,
    strip_aggregate,
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

    b.throughput("extract_vectorised", lambda: extract_fixed_iso(buf, starts, lens), nbytes,
                 note="SPEC 4.4 fast path")
    ts, valid = extract_fixed_iso(buf, starts, lens)
    b.rowrate("extract_vectorised_rows", lambda: extract_fixed_iso(buf, starts, lens), n)

    # Scalar comparisons, on a capped slice, reported at the same scale.
    cap = min(SCALAR_CAP, n)
    cap_starts = starts[:cap]
    cap_bytes = int(starts[cap] if cap < n else nbytes)
    b.throughput("extract_scalar_regex",
                 lambda: extract_scalar_regex(chunk, cap_starts), cap_bytes, reps=1,
                 note=f"per-line regex, {cap} lines extrapolated")
    b.throughput("extract_scalar_strptime",
                 lambda: extract_scalar_strptime(chunk, cap_starts), cap_bytes, reps=1,
                 note="what logmerger does today")

    # Severity: the fixed-offset fast path versus the general regex fallback.
    sev_off = detect_severity_offset(chunk, starts, lens)
    if sev_off is not None:
        b.throughput("severity_fixed_offset",
                     lambda: severity_at_offset(buf, starts, lens, sev_off), nbytes,
                     note=f"vectorised, level word at offset {sev_off}")
    b.throughput("severity_regex_insensitive",
                 lambda: severity_scan(chunk, starts, n, pattern=SEV_RE_INSENSITIVE), nbytes,
                 reps=1, note="fallback: regex over every byte")
    b.throughput("severity_regex_sensitive",
                 lambda: severity_scan(chunk, starts, n, pattern=SEV_RE_SENSITIVE), nbytes,
                 reps=1, note="fallback, case-sensitive")
    b.throughput("severity_fold_maxat",
                 lambda: severity_scan(chunk, starts, n, method="maxat"), nbytes, reps=1,
                 note="np.maximum.at fold instead of lexsort")

    sev = (severity_at_offset(buf, starts, lens, sev_off) if sev_off is not None
           else severity_scan(chunk, starts, n))
    line_no, offs, ets = entries_from(ts, valid, starts)
    esev = sev[np.flatnonzero(valid)]
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

    # The headline number: everything NFR-3 covers, in one pass.
    def full_pipeline():
        bb = np.frombuffer(chunk, np.uint8)
        st, ln = split_lines(bb, lone_cr=False)
        t, v = extract_fixed_iso(bb, st, ln)
        sv = (severity_at_offset(bb, st, ln, sev_off) if sev_off is not None
              else severity_scan(chunk, st, st.size))
        _, _, e = entries_from(t, v, st)
        return e.size, sv.size

    b.throughput("INDEX_TOTAL", full_pipeline, nbytes,
                 note="NFR-3 gate: split + extract + severity + entries")

    def full_pipeline_fallback():
        bb = np.frombuffer(chunk, np.uint8)
        st, ln = split_lines(bb, lone_cr=False)
        t, v = extract_fixed_iso(bb, st, ln)
        sv = severity_scan(chunk, st, st.size)
        _, _, e = entries_from(t, v, st)
        return e.size, sv.size

    b.throughput("INDEX_TOTAL_regex_fallback", full_pipeline_fallback, nbytes, reps=1,
                 note="worst case: unpredictable severity position")

    return {"lines": int(n), "entries": int(nent), "severity_offset": sev_off}


def run_merge_benches(b: Bench, rows: int, k: int, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    per = max(2, rows // k)
    arrays = []
    for _ in range(k):
        steps = rng.integers(1, 200_000, size=per, dtype=np.int64)
        arrays.append(np.cumsum(steps) + 1_791_500_000_000_000)
    total = sum(a.size for a in arrays)

    b.rowrate("merge_build_checkpoints",
              lambda: MergePrototype(arrays).build_checkpoints(), total, reps=1,
              note=f"SPEC 6.3, {k} sources x {per} rows")

    m = MergePrototype(arrays)
    m.build_checkpoints()
    lo, hi = int(min(a[0] for a in arrays)), int(max(a[-1] for a in arrays))
    probes = rng.integers(lo, hi, size=2000, dtype=np.int64).tolist()

    b.latency("merge_time_to_row",
              lambda: [m.time_to_row(t) for t in probes], len(probes),
              note="NFR-6 seek; O(k log n), no replay")

    rowprobes = rng.integers(0, total, size=500, dtype=np.int64).tolist()
    b.latency("merge_row_at", lambda: [m.row_at(int(i)) for i in rowprobes], len(rowprobes),
              note="cold: worst case within a checkpoint")

    blocks = rng.integers(0, max(1, total - 4096), size=200, dtype=np.int64).tolist()
    b.latency("merge_block_4096",
              lambda: [m.block(int(i), 4096) for i in blocks], len(blocks),
              note="one block replay; the engine caches these")

    starts = rng.integers(0, max(1, total - 64), size=2000, dtype=np.int64).tolist()
    b.latency("merge_paint_60rows",
              lambda: [m.block(int(i), 60) for i in starts], len(starts),
              note="NFR-5: rows for one frame")

    cp = m.checkpoint_bytes()
    b.results.append(Result("checkpoint_bytes_per_1M_rows", "MB/1M", cp / 1e6 / (total / 1e6),
                            0.0, False, f"vs 8 MB/1M if materialised ({cp / 1e6:.2f}MB total)"))


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

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
