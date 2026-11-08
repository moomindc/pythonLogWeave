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
* **Dead periods and bursts.** The rate varies by over two orders of magnitude
  across the window and twice drops to exactly nothing, so the timeline strip
  (FR-33) has something to show and the minimap has something to differ from.
  See `ACTIVITY`.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
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


ACTIVITY: list[tuple[float, float, float]] = [
    # (from, to, relative rate).  Fractions of the window, in order, covering
    # it exactly once.
    (0.00, 0.08, 1.0),     # ordinary background traffic
    (0.08, 0.20, 0.0),     # DEAD: nothing is logged at all
    (0.20, 0.26, 6.0),     # a batch job wakes up and hammers for a few minutes
    (0.26, 0.33, 0.15),    # barely a trickle
    (0.33, 0.40, 0.0),     # DEAD again, and longer
    (0.40, SATURATED, 1.4),   # traffic builds as the incident starts
    (SATURATED, FAILING, 9.0),   # saturation: everything retries at once
    (FAILING, RESTART, 14.0),    # failing: the loudest the system ever gets
    (RESTART, 0.86, 0.05),       # the restart itself -- near silence
    (0.86, 0.94, 2.5),           # recovery chatter, backlog draining
    (0.94, 1.00, 1.0),           # back to background
]
"""
How busy the window is, minute by minute (and the reason it is not uniform).

Entries used to be spread evenly across the window, which made every test log
look the same at a glance: a flat wall of text, and -- once the overview strips
arrived at M5 -- a flat timeline with nothing to see. Real logs are nothing
like that. They have hours where nothing happens, a batch job that produces a
day's volume in ninety seconds, and an incident that logs more in two minutes
than the whole quiet morning.

So the rate varies by more than two orders of magnitude across the window, and
**two bands are exactly zero**. The dead bands are global, applied to every
file: a gap only reads as a gap in the merged view if nothing at all is in it,
and one source quietly heartbeating through would fill it in.

This is what makes the timeline strip (FR-33) worth looking at, and it is also
the shape that finds bugs -- an idle gap inside a single checkpoint block is
what caught the minimap's row-to-time interpolation at M5.
"""

GRID = 4096
"""
Sub-intervals the activity profile is sampled on.

Fine enough that a band boundary lands within a fraction of a second of where
`ACTIVITY` puts it, coarse enough that building the CDF is free.
"""


class Clock:
    """Hands out timestamps across the window, in order, with jitter."""

    def __init__(self, start: datetime, span: timedelta, rng: random.Random):
        self.start, self.span, self.rng = start, span, rng
        self._cdf = self._build_cdf()

    @staticmethod
    def _build_cdf() -> list[tuple[float, float]]:
        """
        `(cumulative weight, fraction)` pairs, for sampling by inverse CDF.

        A piecewise-constant rate sampled this way puts exactly no entries in a
        zero band -- which a rejection sampler would also do, but only
        eventually, and a shaped random walk would not do at all.
        """
        pairs, total = [], 0.0
        for i in range(GRID):
            frac = i / GRID
            rate = next((w for lo, hi, w in ACTIVITY if lo <= frac < hi), 1.0)
            total += rate
            pairs.append((total, frac))
        if total <= 0:
            return [(1.0, i / GRID) for i in range(GRID)]
        return [(c / total, f) for c, f in pairs]

    def at(self, frac: float, jitter_s: float = 0.0) -> datetime:
        t = self.start + self.span * max(0.0, min(1.0, frac))
        if jitter_s:
            t += timedelta(seconds=self.rng.uniform(-jitter_s, jitter_s))
        return t

    def _sample(self) -> float:
        """One fraction, drawn against the activity profile."""
        u = self.rng.random()
        lo, hi = 0, len(self._cdf) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cdf[mid][0] < u:
                lo = mid + 1
            else:
                hi = mid
        # Spread within the sub-interval, so a dense band is not visibly combed.
        return min(1.0, self._cdf[lo][1] + self.rng.random() / GRID)

    def series(self, n: int) -> list[tuple[float, datetime]]:
        """
        `n` timestamps in order, distributed by `ACTIVITY`.

        Jitter is applied *after* ordering and can therefore put two entries a
        few hundred milliseconds out of sequence. That is deliberate and is
        what a real multi-threaded writer does -- it is also the case SPEC 4.6's
        sort permutation exists for, so the test logs should contain it.
        """
        fracs = sorted(self._sample() for _ in range(n))
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


# ---------------------------------------------------------------------------
# --tail: a live writer, for exercising follow by hand (M6)
# ---------------------------------------------------------------------------

TAIL_FILES = ("app.log", "soa.log", "database.log", "syslog", "gateway.log")
"""
Which files `--tail` keeps writing to.

`worker.log` is left out on purpose: it is UTF-16, and appending to it correctly
means not writing a second BOM, which is a thing to test deliberately rather
than by accident in the middle of watching an incident. `gateway.1.log` is the
already-rotated half and is supposed to be finished.
"""


def tail(out: Path, rng: random.Random, *, rate: float, rotate_every: float,
         truncate_every: float, seconds: float) -> int:
    """
    Keep appending to the generated files, as a running service would.

    This is the hands-on test for M6 that nothing else provides: the unit tests
    drive the poller by hand precisely so they do not depend on timing, which
    means nothing in the suite ever watches a file for real. Run the generator,
    open the folder in LogWeave, turn on View > Follow New Lines, and leave this
    running in another terminal.

    Three things are done on purpose, because all three are what rotation and
    truncation handling (FR-23) is written against and none of them can be
    arranged by hand at the right moment:

    * lines are appended in **bursts with pauses between**, so the "N new lines
      below" indicator has something to count and the timeline keeps growing
      unevenly rather than at a constant rate;
    * a file is occasionally **rotated** -- renamed aside and recreated -- which
      is what logrotate and most Windows services do;
    * a file is occasionally **truncated in place**, which is what logrotate's
      `copytruncate` and a restarted `logging.FileHandler(mode="w")` do, and is
      the case that destroys history.

    Writes with `"a"` and flushes per burst, so what LogWeave sees is what a
    real writer would produce: whole lines, and sometimes a line that is only
    half on disk when the poll lands.
    """
    started = datetime.now()
    deadline = started + timedelta(seconds=seconds)
    next_rotate = started + timedelta(seconds=rotate_every) if rotate_every else None
    next_truncate = (started + timedelta(seconds=truncate_every)
                     if truncate_every else None)
    appended = rotations = truncations = 0

    print(f"Appending to {len(TAIL_FILES)} files in {out}")
    print(f"  ~{rate:g} lines/second across them, for {seconds:g}s")
    if next_rotate:
        print(f"  rotating a file every {rotate_every:g}s")
    if next_truncate:
        print(f"  truncating a file in place every {truncate_every:g}s")
    print("  Ctrl+C to stop\n")

    try:
        while datetime.now() < deadline:
            burst = rng.choice([1, 1, 2, 3, 5, 9])
            name = rng.choice(TAIL_FILES)
            path = out / name
            now = datetime.now()
            lines = [
                f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} "
                f"{rng.choice(['INFO ', 'INFO ', 'INFO ', 'WARN ', 'ERROR'])}  "
                f"[tail-{appended + i:05d}] live append to {name}"
                for i in range(burst)
            ]
            try:
                with open(path, "a", encoding="utf-8", newline="") as handle:
                    handle.write("\r\n".join(lines) + "\r\n")
            except OSError as exc:
                print(f"  ! could not append to {name}: {exc}")
            appended += burst

            if next_rotate and now >= next_rotate:
                victim = out / rng.choice(TAIL_FILES)
                # A fresh name each time. Reusing one would be a rename *over*
                # an existing file, and if LogWeave is still holding that
                # earlier rotation open -- which it is, because a frozen
                # rotated source stays readable -- Windows refuses the replace
                # with ERROR_ACCESS_DENIED. That is a property of MoveFileEx
                # and not something a reader can permit; a rotating writer
                # numbers its archives for exactly this reason.
                rotations += 1
                aside = victim.with_suffix(f"{victim.suffix}.{rotations}")
                try:
                    victim.replace(aside)
                    victim.write_text("", encoding="utf-8")
                    print(f"  rotated {victim.name} -> {aside.name}")
                except OSError as exc:
                    rotations -= 1
                    print(f"  ! could not rotate {victim.name}: {exc}")
                next_rotate = now + timedelta(seconds=rotate_every)

            if next_truncate and now >= next_truncate:
                victim = out / rng.choice(TAIL_FILES)
                try:
                    # "w" is CREATE_ALWAYS on Windows, which is exactly what a
                    # restarting application does -- and what fails if anything
                    # is holding the file mapped (see FileSource.unmap).
                    with open(victim, "w", encoding="utf-8", newline="") as handle:
                        handle.write("")
                    truncations += 1
                    print(f"  truncated {victim.name} in place")
                except OSError as exc:
                    print(f"  ! could not truncate {victim.name}: {exc}")
                next_truncate = now + timedelta(seconds=truncate_every)

            time.sleep(max(0.01, burst / max(0.1, rate)))
    except KeyboardInterrupt:
        print("\n  stopped")

    print(f"\n  {appended:,} lines appended, {rotations} rotations, "
          f"{truncations} truncations")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"where to write (default: {DEFAULT_OUT})")
    ap.add_argument("--minutes", type=int, default=240,
                    help="how long a window the incident spans (default: 240). "
                         "Long enough that the quiet periods are quiet for "
                         "minutes rather than seconds")
    ap.add_argument("--rate", type=int, default=25,
                    help="average entries a minute, per file (default: 25). "
                         "The actual rate swings from zero to ~14x this; see "
                         "ACTIVITY in this file")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--tail", type=float, nargs="?", const=300.0, default=None,
                    metavar="SECONDS",
                    help="do not generate; keep appending to the files already "
                         "in --out for this long (default 300), so follow mode "
                         "can be watched working. Run it in a second terminal "
                         "with LogWeave open on the folder")
    ap.add_argument("--tail-rate", type=float, default=4.0,
                    help="lines a second while --tail is running (default: 4)")
    ap.add_argument("--rotate-every", type=float, default=60.0,
                    help="seconds between rotating one file aside while "
                         "--tail runs; 0 to never (default: 60)")
    ap.add_argument("--truncate-every", type=float, default=0.0,
                    help="seconds between truncating one file in place while "
                         "--tail runs; 0 to never (default: 0, because it "
                         "destroys the history it truncates -- FR-23)")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    if args.tail is not None:
        if not args.out.is_dir():
            print(f"{args.out} does not exist -- generate the files first, "
                  f"without --tail")
            return 1
        return tail(args.out, rng, rate=args.tail_rate,
                    rotate_every=args.rotate_every,
                    truncate_every=args.truncate_every, seconds=args.tail)
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
    print("  - the timeline strip has blank stretches; the minimap does not")
    print("  - the minimap is a colour map of severity; the timeline is volume")
    print("")
    print("To watch follow mode work (M6), leave this running next door:")
    print(f"  python tools/make_test_logs.py --out {out} --tail")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
