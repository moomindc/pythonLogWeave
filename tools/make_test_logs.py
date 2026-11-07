"""
Write a set of realistic log files for hands-on testing of the GUI.

    python tools/make_test_logs.py              -> C:\\temp\\logweave-logs
    python tools/make_test_logs.py --out D:/x   -> somewhere else
    python tools/make_test_logs.py --minutes 90 --rate 40

Then drag the folder onto the LogWeave window, or:

    python -m logweave C:/temp/logweave-logs/*.log

This is **not** the test corpus. `tools/gen_corpus.py` writes machine-checkable
ground truth and is what the test suite asserts against; nothing here is checked
by anything. These files exist to be looked at, so they are shaped for the eye:
one coherent incident told from six angles, so that interleaving them actually
shows you something a single file would not.

## The scenario

A checkout service degrades and recovers over a window ending now. A slow
dependency backs up a connection pool, the pool exhausts, the circuit breaker
opens, requests start failing at the front door, an operator restarts a worker,
and it recovers. Each file sees a different part of that, and the causal order
is only visible when they are merged -- which is the entire point of the
program.

Deliberately included, because each one exercises something that has broken at
least once during development:

* **Six different timestamp formats**, including a yearless syslog and an
  access log whose timestamp sits inside the line rather than leading it.
* **Sub-second precision that differs between files** -- microseconds, commas,
  whole seconds -- so the merge has to order entries that claim the same second.
* **Multi-line stack traces**, which are one entry spanning many lines.
* **Very long lines** (a 3000-character SQL statement), which is what the
  paint budget is measured against.
* **A UTF-16 file with a BOM**, which Windows tools produce constantly.
* **A rotated pair** (`gateway.log` and `gateway.1.log`), to see two halves of
  one stream sort back together.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_OUT = Path(r"C:\temp\logweave-logs") if sys.platform == "win32" \
    else Path("/tmp/logweave-logs")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# The incident, as fractions through the window. Everything else is background
# noise arranged around these.
CALM, SLOWING, SATURATED, FAILING, RESTART, RECOVERED = 0.0, 0.35, 0.55, 0.68, 0.82, 0.88


def phase(frac: float) -> str:
    if frac < SLOWING:
        return "calm"
    if frac < SATURATED:
        return "slowing"
    if frac < FAILING:
        return "saturated"
    if frac < RESTART:
        return "failing"
    if frac < RECOVERED:
        return "restart"
    return "recovered"


class Clock:
    """Hands out timestamps across the window, in order, with jitter."""

    def __init__(self, start: datetime, span: timedelta, rng: random.Random):
        self.start, self.span, self.rng = start, span, rng

    def at(self, frac: float, jitter_s: float = 0.0) -> datetime:
        t = self.start + self.span * max(0.0, min(1.0, frac))
        if jitter_s:
            t += timedelta(seconds=self.rng.uniform(-jitter_s, jitter_s))
        return t

    def series(self, n: int) -> list[tuple[float, datetime]]:
        fracs = sorted(self.rng.random() for _ in range(n))
        return [(f, self.at(f, jitter_s=0.4)) for f in fracs]


# ---------------------------------------------------------------------------
# the files
# ---------------------------------------------------------------------------

def app_log(clock: Clock, rng: random.Random, n: int) -> list[str]:
    """`2026-10-04 14:20:00.123456 LEVEL message` -- microseconds, leading stamp."""
    out = []
    for frac, t in clock.series(n):
        p = phase(frac)
        stamp = t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond:06d}"
        if p == "calm":
            level, msg = rng.choice([
                ("INFO", f"checkout completed order={rng.randrange(10**6):06d}"),
                ("DEBUG", f"pool idle={rng.randrange(8, 16)} busy={rng.randrange(0, 4)}"),
                ("INFO", f"worker {rng.randrange(1, 9)} heartbeat"),
                ("TRACE", "exit forward() rc=0"),
            ])
        elif p == "slowing":
            level, msg = rng.choice([
                ("WARN", f"pool utilisation {rng.randrange(78, 94)} percent"),
                ("INFO", f"checkout completed order={rng.randrange(10**6):06d}"),
                ("DEBUG", f"pool idle={rng.randrange(0, 3)} busy={rng.randrange(12, 16)}"),
                ("WARN", f"slow dependency pricing-svc took {rng.randrange(900, 2600)}ms"),
            ])
        elif p == "saturated":
            level, msg = rng.choice([
                ("ERROR", "connection pool exhausted, 0 of 16 available"),
                ("WARN", f"request queued {rng.randrange(2000, 9000)}ms"),
                ("ERROR", "timeout acquiring connection after 5000ms"),
            ])
        elif p == "failing":
            level, msg = rng.choice([
                ("CRITICAL", "circuit breaker OPEN for pricing-svc"),
                ("ERROR", "checkout failed: upstream unavailable"),
                ("ERROR", "rejecting request, breaker open"),
            ])
        elif p == "restart":
            level, msg = rng.choice([
                ("WARN", "shutdown signal received, draining"),
                ("INFO", "worker 3 restarting"),
                ("INFO", "pool reinitialised, 16 connections"),
            ])
        else:
            level, msg = rng.choice([
                ("INFO", "circuit breaker CLOSED for pricing-svc"),
                ("INFO", f"checkout completed order={rng.randrange(10**6):06d}"),
                ("DEBUG", f"pool idle={rng.randrange(9, 16)} busy={rng.randrange(0, 5)}"),
            ])
        out.append(f"{stamp} {level} {msg}")

        # A stack trace: one entry, many lines. The continuation lines carry no
        # timestamp, which is exactly how the indexer recognises them.
        if level in ("ERROR", "CRITICAL") and rng.random() < 0.18:
            out += [
                "    at com.example.checkout.PoolManager.acquire(PoolManager.java:214)",
                "    at com.example.checkout.OrderService.price(OrderService.java:88)",
                "    at com.example.checkout.OrderService.submit(OrderService.java:51)",
                "    at com.example.http.Dispatcher.handle(Dispatcher.java:140)",
                "Caused by: java.net.SocketTimeoutException: Read timed out",
                "    ... 14 more",
            ]
    return out


def soa_log(clock: Clock, rng: random.Random, n: int) -> list[str]:
    """`2026-10-04 14:20:00,574 LEVEL message` -- comma millis, the Java house style."""
    out = []
    for frac, t in clock.series(n):
        p = phase(frac)
        stamp = t.strftime("%Y-%m-%d %H:%M:%S,") + f"{t.microsecond // 1000:03d}"
        if p in ("calm", "recovered"):
            level, msg = rng.choice([
                ("INFO", f"resolved upstream to 10.2.0.{rng.randrange(2, 40)}:8443"),
                ("DEBUG", f"serialised {rng.randrange(400, 4000)} bytes"),
                ("INFO", f"flushed {rng.randrange(60, 400)} records"),
            ])
        elif p == "slowing":
            level, msg = rng.choice([
                ("WARN", f"retry 1 of 3 for pricing-svc after {rng.randrange(500, 2000)}ms"),
                ("INFO", f"flushed {rng.randrange(60, 400)} records"),
                ("WARN", "response time degraded, p99 above threshold"),
            ])
        else:
            level, msg = rng.choice([
                ("ERROR", "pricing-svc returned 503 Service Unavailable"),
                ("ERROR", f"retry 3 of 3 exhausted for request {rng.randrange(10**6):06d}"),
                ("WARN", "falling back to cached price list"),
            ])
        out.append(f"{stamp} {level} {msg}")
    return out


def gateway_lines(clock: Clock, rng: random.Random, n: int) -> list[tuple[float, str]]:
    """Apache/IIS style: the timestamp is *inside* the line, not leading it."""
    out = []
    for frac, t in clock.series(n):
        p = phase(frac)
        # The real local offset, not a hardcoded one. An access log states its
        # zone and LogWeave converts it to local wall time (D1), so writing
        # "+0100" in a GMT month would put these files an hour before the rest
        # and make the interleaving look broken when it is in fact correct.
        stamp = t.strftime("%d/%b/%Y:%H:%M:%S ") + t.astimezone().strftime("%z")
        ip = f"91.194.60.{rng.randrange(2, 250)}"
        path = rng.choice(["/api/v2/order", "/api/v2/price", "/health",
                           "/api/v2/basket", "/static/app.js"])
        if p in ("calm", "slowing", "recovered"):
            status = rng.choice([200, 200, 200, 304, 404])
        elif p == "saturated":
            status = rng.choice([200, 500, 502, 504])
        else:
            status = rng.choice([502, 503, 503, 504])
        size = rng.randrange(400, 90000)
        out.append((frac, f'{ip} - - [{stamp}] "GET {path} HTTP/1.1" {status} {size}'))
    return out


def syslog_lines(clock: Clock, rng: random.Random, n: int) -> list[str]:
    """`Oct  4 14:20:00 host daemon[123]: msg` -- no year anywhere in the file."""
    out = []
    for frac, t in clock.series(n):
        p = phase(frac)
        stamp = f"{MONTHS[t.month - 1]} {t.day:2d} {t:%H:%M:%S}"
        pid = rng.randrange(400, 9000)
        if p == "restart":
            msg = rng.choice([
                "systemd[1]: Stopping checkout.service...",
                "systemd[1]: checkout.service: Succeeded.",
                "systemd[1]: Started checkout.service.",
            ])
        elif p in ("saturated", "failing"):
            msg = rng.choice([
                f"kernel: TCP: out of memory -- consider tuning tcp_mem pid={pid}",
                f"checkout[{pid}]: health probe failed, 3 consecutive",
                f"haproxy[{pid}]: backend checkout has no server available!",
            ])
        else:
            msg = rng.choice([
                f"checkout[{pid}]: health probe ok",
                f"cron[{pid}]: (root) CMD (/usr/local/bin/rotate-logs)",
                f"sshd[{pid}]: Accepted publickey for deploy from 10.2.0.9",
            ])
        out.append(f"{stamp} app-host-01 {msg}")
    return out


def db_log(clock: Clock, rng: random.Random, n: int) -> list[str]:
    """Whole seconds only, and the very long lines the paint budget cares about."""
    out = []
    cols = ", ".join(f"o.col_{i} AS alias_{i}" for i in range(120))
    for frac, t in clock.series(n):
        p = phase(frac)
        stamp = t.strftime("%Y-%m-%dT%H:%M:%S")
        if p in ("slowing", "saturated", "failing") and rng.random() < 0.45:
            ms = rng.randrange(2000, 30000)
            out.append(
                f"{stamp} WARN slow query {ms}ms: SELECT {cols} "
                f"FROM orders o JOIN basket b ON b.order_id = o.id "
                f"JOIN price p ON p.sku = b.sku WHERE o.created_at > now() - interval "
                f"'1 hour' AND o.status IN ('pending','authorising') ORDER BY o.created_at DESC"
            )
        else:
            out.append(f"{stamp} INFO {rng.choice([
                f'checkpoint complete, {rng.randrange(10, 900)} buffers written',
                f'autovacuum: table orders, {rng.randrange(100, 9000)} dead rows',
                f'connection from 10.2.0.{rng.randrange(2, 40)} authorised',
            ])}")
    return out


def worker_lines(clock: Clock, rng: random.Random, n: int) -> list[str]:
    """A Windows-flavoured file: UTF-16 with a BOM, which Windows tools emit."""
    out = []
    for frac, t in clock.series(n):
        p = phase(frac)
        stamp = t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond:06d}"
        if p == "restart":
            level, msg = "WARN", "worker stopping, 4 jobs requeued"
        elif p in ("saturated", "failing"):
            level, msg = rng.choice([
                ("ERROR", "job failed: System.TimeoutException: operation timed out"),
                ("WARN", f"queue depth {rng.randrange(400, 5000)}"),
            ])
        else:
            level, msg = rng.choice([
                ("INFO", f"job {rng.randrange(10**5):05d} completed in "
                         f"{rng.randrange(10, 400)}ms"),
                ("DEBUG", f"queue depth {rng.randrange(0, 40)}"),
            ])
        out.append(f"{stamp} {level} {msg}")
    return out


# ---------------------------------------------------------------------------

def write(path: Path, lines: list[str], *, encoding: str = "utf-8",
          newline: str = "\r\n") -> int:
    text = newline.join(lines) + newline
    path.write_text(text, encoding=encoding, newline="")
    return len(text.encode(encoding))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"where to write (default: {DEFAULT_OUT})")
    ap.add_argument("--minutes", type=int, default=45,
                    help="how long a window the incident spans (default: 45)")
    ap.add_argument("--rate", type=int, default=25,
                    help="roughly how many entries a minute, per file (default: 25)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    span = timedelta(minutes=args.minutes)
    # Ending now, so the files look like something that just happened and the
    # yearless syslog is anchored against a sensible mtime.
    clock = Clock(datetime.now() - span, span, rng)
    n = max(10, args.minutes * args.rate)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, int, int, str]] = []

    def record(name: str, lines: list[str], **kw) -> None:
        size = write(out / name, lines, **kw)
        written.append((name, len(lines), size, kw.get("encoding", "utf-8")))

    record("app.log", app_log(clock, rng, n))
    record("soa.log", soa_log(clock, rng, int(n * 0.8)))
    record("database.log", db_log(clock, rng, int(n * 0.5)))
    record("syslog", syslog_lines(clock, rng, int(n * 0.4)))
    # UTF-16 with a BOM, LF-terminated, to exercise SPEC 3.3 end to end.
    record("worker.log", worker_lines(clock, rng, int(n * 0.7)),
           encoding="utf-16", newline="\n")

    # A rotated pair: the same stream, split at the point the incident starts,
    # so merging puts the two halves back in order around everything else.
    gw = gateway_lines(clock, rng, int(n * 1.4))
    record("gateway.1.log", [line for frac, line in gw if frac < SATURATED])
    record("gateway.log", [line for frac, line in gw if frac >= SATURATED])

    total = sum(size for _, _, size, _ in written)
    width = max(len(name) for name, *_ in written)
    print(f"\n{out}\n")
    for name, lines, size, enc in sorted(written):
        print(f"  {name:<{width}}  {lines:>6,} lines  {size / 1024:>8.1f} KiB  {enc}")
    print(f"\n  {len(written)} files, {total / 1024:.1f} KiB, "
          f"{args.minutes} minutes ending now\n")
    print("Drag the folder onto the LogWeave window, or:")
    print(f"  python -m logweave {out}")
    print("\nWhat to look for:")
    print("  - the incident only reads in order once the files are interleaved")
    print("  - gateway.1.log and gateway.log interleave as one stream")
    print("  - worker.log is UTF-16; it should look like every other file")
    print("  - syslog has no year in it at all; check its dates are right")
    print("  - database.log has 3000-character lines; scroll right (FR-36a)")
    print("  - app.log stack traces are one entry each, marked with an arrow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
