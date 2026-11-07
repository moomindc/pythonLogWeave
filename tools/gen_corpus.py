#!/usr/bin/env python3
"""
LogWeave corpus generator  --  milestone M0.

Generates log files together with their GROUND TRUTH, so that every later
milestone is checked against something independent rather than against itself.

For every entry the manifest records:

  * ``wall_us``   -- the exact naive-local microsecond value (SPEC 2.2, D1)
  * ``line_no``   -- the 1-based physical line the entry starts on (SPEC 2.3, D9)
  * ``byte_off``  -- the byte offset the entry starts at
  * ``n_lines``   -- physical lines the entry spans (multi-line entries, FR-15)
  * ``severity``  -- severity class 0-7 (SPEC 10.2)

and for a whole scenario, the exact expected merged order across its files,
tie-broken by ``(wall_us, file_order, entry_index)`` as FR-17 requires.

Ground truth is computed here with ``datetime`` arithmetic. The engine computes
the same values with vectorised integer arithmetic (Hinnant's algorithm). The two
being independent is the point: a bug in one will not hide a bug in the other.

Usage
-----
    python tools/gen_corpus.py --out tests/corpus
    python tools/gen_corpus.py --out tests/corpus --only formats,edge
    python tools/gen_corpus.py --out tests/corpus --perf 1mb,100mb
    python tools/gen_corpus.py --out D:/big --perf 1gb --only perf

Timezone caveat
---------------
Zone-aware formats (``ymd_t_hms_z``, ``apache_access``) and the epoch formats are
written relative to the generating machine's local zone, because D1 says a
zone-aware timestamp converts to local time and then drops its zone. Their
``wall_us`` truth is therefore only valid for a machine in the same zone with the
same DST rules. Regenerate the corpus rather than moving it between zones; CI
always generates in-job, so this never bites there. The manifest records
``local_tz_offset_s`` so a mismatch is detectable.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

EPOCH = datetime(1970, 1, 1)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAYNAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# SPEC 10.2 -- fixed, ordered, three bits.
SEV_NONE, SEV_TRACE, SEV_DEBUG, SEV_INFO, SEV_NOTICE, SEV_WARN, SEV_ERROR, SEV_FATAL = range(8)
SEV_NAME = {
    SEV_TRACE: "TRACE", SEV_DEBUG: "DEBUG", SEV_INFO: "INFO", SEV_NOTICE: "NOTICE",
    SEV_WARN: "WARN", SEV_ERROR: "ERROR", SEV_FATAL: "FATAL",
}

# Realistic-ish mix: mostly INFO, a tail of problems.
SEV_WEIGHTS = [
    (SEV_INFO, 62), (SEV_DEBUG, 18), (SEV_WARN, 9),
    (SEV_ERROR, 6), (SEV_TRACE, 3), (SEV_NOTICE, 1), (SEV_FATAL, 1),
]

# Messages are deliberately free of commas and quotes so that the same pool can
# be used for CSV and JSONL without needing to quote or escape.
MESSAGES = {
    SEV_TRACE: ["enter forward() depth=2", "exit forward() rc=0", "lock acquired on pool",
                "span started", "cache probe miss"],
    SEV_DEBUG: ["resolved upstream to 10.2.0.14:8443", "serialised 1842 bytes",
                "config reload no-op", "heartbeat ack in 4ms", "pool idle=12 busy=3"],
    SEV_INFO: ["request accepted", "pool resized 32 -> 64", "worker 7 started",
               "session established for user 4471", "flushed 240 records",
               "checkpoint written", "upstream healthy"],
    SEV_NOTICE: ["configuration changed by admin", "certificate expires in 21 days",
                 "entering maintenance window"],
    SEV_WARN: ["retry 1/3 scheduled", "pool utilisation above 85 percent",
               "slow query took 2841ms", "clock skew 412ms detected",
               "deprecated endpoint used"],
    SEV_ERROR: ["upstream timeout after 30s", "connection pool exhausted",
                "failed to parse response body", "write rejected by backend",
                "circuit breaker opened"],
    SEV_FATAL: ["unrecoverable state - shutting down", "heap exhausted"],
}

STACK = [
    "    at Gateway.forward(Gateway.java:88)",
    "    at Gateway.handle(Gateway.java:41)",
    "    at Dispatcher.run(Dispatcher.java:217)",
    "    at java.base/java.lang.Thread.run(Thread.java:840)",
    "  Caused by: java.net.SocketTimeoutException: Read timed out",
    "    ... 14 more",
]


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def wall_us(dt: datetime) -> int:
    """SPEC 2.2 naive-local wall_us. ``dt`` must be naive."""
    td = dt - EPOCH
    return td.days * 86_400_000_000 + td.seconds * 1_000_000 + td.microseconds


def truncate(dt: datetime, res_us: int) -> datetime:
    """Drop precision a format cannot carry, so truth matches what was written."""
    if res_us <= 1:
        return dt
    return dt.replace(microsecond=(dt.microsecond // res_us) * res_us)


def local_offset_seconds() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


# ---------------------------------------------------------------------------
# format renderers
# ---------------------------------------------------------------------------

def _msg(rng: random.Random, sev: int) -> str:
    return rng.choice(MESSAGES[sev if sev in MESSAGES else SEV_INFO])


def r_ymd_hms_comma_f(dt, sev, rng):
    return f"{dt:%Y-%m-%d %H:%M:%S},{dt.microsecond // 1000:03d} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_ymd_hms_dot_f(dt, sev, rng):
    return f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond:06d} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_ymd_hms(dt, sev, rng):
    return f"{dt:%Y-%m-%d %H:%M:%S} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_ymd_t_hms_dot_f(dt, sev, rng):
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_ymd_t_hms_z(dt, sev, rng):
    """Written as UTC with an explicit +0000; D1 says it converts back to local."""
    u = dt.astimezone().astimezone(UTC)
    return f"{u:%Y-%m-%dT%H:%M:%S}+0000 {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_syslog_bdhms(dt, sev, rng):
    """Yearless. The year must be inferred (SPEC 4.5)."""
    return (f"{MONTHS[dt.month - 1]} {dt.day:2d} {dt:%H:%M:%S} "
            f"gw01 gateway[{rng.randrange(1000, 9999)}]: {SEV_NAME[sev]} {_msg(rng, sev)}")


def r_strace_hms_dot(dt, sev, rng):
    """Dateless. The date must be inferred (SPEC 4.5)."""
    return f"{dt:%H:%M:%S}.{dt.microsecond:06d} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_epoch_s(dt, sev, rng):
    return f"{int(dt.timestamp())} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_epoch_ms(dt, sev, rng):
    return f"{round(dt.timestamp() * 1000)} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_epoch_float(dt, sev, rng):
    return f"{dt.timestamp():.3f} {SEV_NAME[sev]} {_msg(rng, sev)}"


def r_apache_error(dt, sev, rng):
    return (f"[{DAYNAMES[dt.weekday()]} {MONTHS[dt.month - 1]} {dt.day:02d} "
            f"{dt:%H:%M:%S}.{dt.microsecond:06d} {dt.year}] "
            f"[core:{SEV_NAME[sev].lower()}] [pid {rng.randrange(1000, 9999)}] {_msg(rng, sev)}")


def r_apache_access(dt, sev, rng):
    off = dt.astimezone().strftime("%z")
    code = rng.choice([200, 200, 200, 200, 304, 404, 500, 502])
    return (f"91.194.60.{rng.randrange(1, 254)} - - "
            f"[{dt.day:02d}/{MONTHS[dt.month - 1]}/{dt.year}:{dt:%H:%M:%S} {off}] "
            f'"GET /api/v2/order HTTP/1.1" {code} {rng.randrange(100, 99999)}')


def r_python_http(dt, sev, rng):
    return (f"::1 - - [{dt.day:02d}/{MONTHS[dt.month - 1]}/{dt.year} {dt:%H:%M:%S}] "
            f'"GET /log{rng.randrange(0, 9)}.txt HTTP/1.1" 200 -')


def r_csv(dt, sev, rng):
    return (f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond // 1000:03d},"
            f"{SEV_NAME[sev]},gateway,{_msg(rng, sev)}")


def r_jsonl(dt, sev, rng):
    rec = {
        "ts": f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}",
        "level": SEV_NAME[sev].lower(),
        "svc": "gateway",
        "msg": _msg(rng, sev),
        "cid": f"{rng.randrange(16 ** 8):08x}",
    }
    return json.dumps(rec, separators=(",", ":"))


@dataclass(frozen=True)
class Format:
    name: str
    render: object
    res_us: int
    has_severity: bool = True
    zone_aware: bool = False
    suffix: str = ".log"
    notes: str = ""


FORMATS: list[Format] = [
    Format("ymd_hms_dot_f", r_ymd_hms_dot_f, 1,
           notes="the fixed-position ISO form the vectorised fast path targets (SPEC 4.4)"),
    Format("ymd_hms_comma_f", r_ymd_hms_comma_f, 1000),
    Format("ymd_hms", r_ymd_hms, 1_000_000),
    Format("ymd_t_hms_dot_f", r_ymd_t_hms_dot_f, 1000),
    Format("ymd_t_hms_z", r_ymd_t_hms_z, 1_000_000, zone_aware=True,
           notes="written UTC +0000; D1 converts to local and drops the zone"),
    Format("syslog_bdhms", r_syslog_bdhms, 1_000_000,
           notes="yearless; year must be inferred (SPEC 4.5)"),
    Format("strace_hms_dot", r_strace_hms_dot, 1,
           notes="dateless; date must be inferred (SPEC 4.5)"),
    Format("epoch_s", r_epoch_s, 1_000_000, zone_aware=True),
    Format("epoch_ms", r_epoch_ms, 1000, zone_aware=True),
    Format("epoch_float", r_epoch_float, 1000, zone_aware=True),
    Format("apache_error", r_apache_error, 1,
           notes="severity word is lowercase inside [core:error]"),
    Format("apache_access", r_apache_access, 1_000_000, has_severity=False, zone_aware=True,
           notes="no severity word at all; severity must be NONE"),
    Format("python_http", r_python_http, 1_000_000, has_severity=False),
    Format("csv", r_csv, 1000, suffix=".csv"),
    Format("jsonl", r_jsonl, 1000, suffix=".jsonl",
           notes="D17: record shown verbatim; level is lowercase"),
]
FORMATS_BY_NAME = {f.name: f for f in FORMATS}


# ---------------------------------------------------------------------------
# truth-tracking writer
# ---------------------------------------------------------------------------

@dataclass
class EntryTruth:
    line_no: int
    n_lines: int
    byte_off: int
    wall_us: int
    severity: int


@dataclass
class FileTruth:
    name: str
    format: str
    encoding: str = "utf-8"
    newline: str = "\\n"
    size: int = 0
    entry_count: int = 0
    line_count: int = 0
    zone_aware: bool = False
    sample_every: int = 1
    notes: str = ""
    entries: list = field(default_factory=list)


class TruthFile:
    """Accumulates bytes while recording exact ground truth for each entry."""

    def __init__(self, name: str, fmt_name: str, *, encoding: str = "utf-8",
                 newline: bytes = b"\n", bom: bytes = b"", zone_aware: bool = False,
                 notes: str = ""):
        self.name = name
        self.fmt_name = fmt_name
        self.encoding = encoding
        self.newline = newline
        self.zone_aware = zone_aware
        self.notes = notes
        self.buf = bytearray(bom)
        self.entries: list[EntryTruth] = []
        self._next_line = 1  # 1-based (D9)

    def add_entry(self, lines: list[str], ts_us: int, severity: int) -> None:
        off = len(self.buf)
        for ln in lines:
            self.buf += ln.encode(self.encoding) + self.newline
        self.entries.append(EntryTruth(self._next_line, len(lines), off, ts_us, severity))
        self._next_line += len(lines)

    def add_untracked(self, lines: list[str]) -> None:
        """Lines that are deliberately not entries (banners, timestamp-less noise)."""
        for ln in lines:
            self.buf += ln.encode(self.encoding) + self.newline
        self._next_line += len(lines)

    def add_bytes(self, raw: bytes, n_lines: int) -> None:
        self.buf += raw
        self._next_line += n_lines

    def truth(self) -> FileTruth:
        return FileTruth(
            name=self.name, format=self.fmt_name, encoding=self.encoding,
            newline=self.newline.decode("latin-1").replace("\r", "\\r").replace("\n", "\\n")
            .replace("\x00", "\\0"),
            size=len(self.buf), entry_count=len(self.entries),
            line_count=self._next_line - 1, zone_aware=self.zone_aware,
            notes=self.notes, entries=[asdict(e) for e in self.entries],
        )

    def write(self, directory: Path) -> FileTruth:
        path = directory / self.name
        path.write_bytes(self.buf)
        # Set mtime to the last entry's timestamp, so the fixture is faithful to
        # a real log file. This is not cosmetic: SPEC 4.5 anchors the yearless
        # and dateless formats from mtime and walks rollovers backwards from it,
        # so a corpus whose files all claim to have been written today cannot
        # test that inference at all -- it would silently validate against the
        # wrong anchor. The naive wall_us is converted back through the local
        # zone because that is what the filesystem stores.
        if self.entries:
            last_us = max(e.wall_us for e in self.entries)
            stamp = datetime(1970, 1, 1) + timedelta(microseconds=last_us)
            path.touch()
            os.utime(path, (stamp.timestamp(), stamp.timestamp()))
        return self.truth()


def merged_order(files: list[FileTruth]) -> list[list[int]]:
    """Expected merged order, tie-broken (wall_us, file_order, entry_index) per FR-17."""
    keyed = [
        (e["wall_us"], fi, ei)
        for fi, f in enumerate(files)
        for ei, e in enumerate(f.entries)
    ]
    keyed.sort()
    return [[fi, ei] for _, fi, ei in keyed]


# ---------------------------------------------------------------------------
# entry stream helper
# ---------------------------------------------------------------------------

def sev_stream(rng: random.Random, n: int) -> list[int]:
    pop = [s for s, _ in SEV_WEIGHTS]
    wts = [w for _, w in SEV_WEIGHTS]
    return rng.choices(pop, weights=wts, k=n)


def emit_series(tf: TruthFile, fmt: Format, start: datetime, count: int,
                rng: random.Random, *, step_us_range=(200_000, 3_000_000),
                multiline_pct: int = 6) -> None:
    """Write ``count`` entries with increasing timestamps, some multi-line."""
    dt = start
    for sev in sev_stream(rng, count):
        dt = dt + timedelta(microseconds=rng.randrange(*step_us_range))
        t = truncate(dt, fmt.res_us)
        lines = [fmt.render(t, sev, rng)]
        if sev >= SEV_ERROR and rng.randrange(100) < 55:
            lines += STACK[: rng.randrange(3, len(STACK) + 1)]
        elif rng.randrange(100) < multiline_pct:
            lines += ["    continued detail line", "    and another"]
        tf.add_entry(lines, wall_us(t), sev if fmt.has_severity else SEV_NONE)


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def scenario_formats(out: Path, rng: random.Random) -> dict:
    """One small file per supported format. Exercises detection breadth."""
    out.mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 10, 4, 9, 0, 0)
    files = []
    for fmt in FORMATS:
        tf = TruthFile(f"{fmt.name}{fmt.suffix}", fmt.name,
                       zone_aware=fmt.zone_aware, notes=fmt.notes)
        emit_series(tf, fmt, base, 200, rng)
        files.append(tf.write(out))
    return {"files": files, "merged": None}


def scenario_basic(out: Path, rng: random.Random) -> dict:
    """Three files over one overlapping window -- the core merge case (S1)."""
    out.mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 10, 4, 14, 20, 0)
    plan = [
        ("app.log", "ymd_hms_dot_f", 400),
        ("SOA.log", "ymd_hms_comma_f", 350),
        ("iis.log", "apache_access", 500),
    ]
    files = []
    for name, fmt_name, count in plan:
        fmt = FORMATS_BY_NAME[fmt_name]
        tf = TruthFile(name, fmt_name, zone_aware=fmt.zone_aware)
        emit_series(tf, fmt, base, count, rng, step_us_range=(50_000, 900_000))
        files.append(tf.write(out))
    return {"files": files, "merged": merged_order(files)}


def scenario_edge(out: Path, rng: random.Random) -> dict:
    """The nasty ones. Every file here has bitten a log viewer somewhere."""
    out.mkdir(parents=True, exist_ok=True)
    fmt = FORMATS_BY_NAME["ymd_hms_dot_f"]
    base = datetime(2026, 10, 4, 12, 0, 0)
    files = []

    # Empty file.
    files.append(TruthFile("empty.log", "none").write(out))

    # No timestamps at all (FR-19).
    tf = TruthFile("no_timestamps.log", "none")
    tf.add_untracked([f"plain line {i} with no timestamp anywhere" for i in range(40)])
    files.append(tf.write(out))

    # A banner before the first real entry -- defeats first-line-only detection (SPEC 4.2).
    tf = TruthFile("leading_banner.log", "ymd_hms_dot_f",
                   notes="8 banner lines first; detection must sample, not read line 1")
    tf.add_untracked(["=" * 60, "  Acme Gateway 4.2.1", "  (c) 2026", "=" * 60, "", "", "", ""])
    emit_series(tf, fmt, base, 120, rng)
    files.append(tf.write(out))

    # Final line with no terminator (follow partial-line rule, SPEC 7.3).
    tf = TruthFile("partial_last_line.log", "ymd_hms_dot_f",
                   notes="last line has no newline terminator")
    emit_series(tf, fmt, base, 30, rng)
    t = truncate(base + timedelta(seconds=99), fmt.res_us)
    line = fmt.render(t, SEV_INFO, rng)
    tf.entries.append(EntryTruth(tf._next_line, 1, len(tf.buf), wall_us(t), SEV_INFO))
    tf.buf += line.encode("utf-8")  # deliberately no newline
    files.append(tf.write(out))

    # One 1MB line (NFR-16).
    tf = TruthFile("huge_line.log", "ymd_hms_dot_f", notes="contains a single 1MB line")
    emit_series(tf, fmt, base, 5, rng)
    t = truncate(base + timedelta(seconds=30), fmt.res_us)
    big = f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond:06d} ERROR " + ("x" * (1 << 20))
    tf.add_entry([big], wall_us(t), SEV_ERROR)
    emit_series(tf, fmt, base + timedelta(seconds=60), 5, rng)
    files.append(tf.write(out))

    # Out-of-order, unique timestamps (FR-16).
    tf = TruthFile("ooo_unique.log", "ymd_hms_dot_f", notes="timestamps are not monotonic")
    stamps = [truncate(base + timedelta(seconds=i), fmt.res_us) for i in range(120)]
    shuffled = stamps[:]
    for i in range(0, 120, 12):          # shuffle inside 12-entry windows
        window = shuffled[i:i + 12]
        rng.shuffle(window)
        shuffled[i:i + 12] = window
    for sev, t in zip(sev_stream(rng, len(shuffled)), shuffled, strict=True):
        tf.add_entry([fmt.render(t, sev, rng)], wall_us(t), sev)
    files.append(tf.write(out))

    # Out-of-order with duplicate timestamps (FR-17 stable tie-break).
    tf = TruthFile("ooo_duplicate.log", "ymd_hms_dot_f",
                   notes="many entries share a timestamp; order must stay stable")
    dups = []
    for i in range(40):
        t = truncate(base + timedelta(seconds=i // 4), fmt.res_us)
        dups.append(t)
    rng.shuffle(dups)
    for sev, t in zip(sev_stream(rng, len(dups)), dups, strict=True):
        tf.add_entry([fmt.render(t, sev, rng)], wall_us(t), sev)
    files.append(tf.write(out))

    # New Year rollover, yearless syslog (SPEC 4.5).
    sfmt = FORMATS_BY_NAME["syslog_bdhms"]
    tf = TruthFile("newyear_syslog.log", "syslog_bdhms",
                   notes="spans 31 Dec 2025 -> 1 Jan 2026; year is not in the format")
    ny = datetime(2025, 12, 31, 23, 58, 0)
    for i, sev in enumerate(sev_stream(rng, 60)):
        t = truncate(ny + timedelta(seconds=8 * i), sfmt.res_us)
        tf.add_entry([sfmt.render(t, sev, rng)], wall_us(t), sev)
    files.append(tf.write(out))

    # Midnight rollover, dateless strace (SPEC 4.5).
    xfmt = FORMATS_BY_NAME["strace_hms_dot"]
    tf = TruthFile("midnight_strace.log", "strace_hms_dot",
                   notes="spans 23:59 -> 00:01; no date in the format")
    mn = datetime(2026, 10, 4, 23, 59, 0)
    for i, sev in enumerate(sev_stream(rng, 60)):
        t = truncate(mn + timedelta(seconds=2 * i), xfmt.res_us)
        tf.add_entry([xfmt.render(t, sev, rng)], wall_us(t), sev)
    files.append(tf.write(out))

    # Mixed line endings (FR-6).
    tf = TruthFile("mixed_newlines.log", "ymd_hms_dot_f", newline=b"\n",
                   notes="CRLF, LF and a lone CR all present")
    for i, sev in enumerate(sev_stream(rng, 30)):
        t = truncate(base + timedelta(seconds=i), fmt.res_us)
        term = [b"\r\n", b"\n", b"\r"][i % 3]
        off = len(tf.buf)
        tf.entries.append(EntryTruth(tf._next_line, 1, off, wall_us(t), sev))
        tf.buf += fmt.render(t, sev, rng).encode("utf-8") + term
        tf._next_line += 1
    files.append(tf.write(out))

    # Encodings (FR-5).
    tf = TruthFile("utf8_bom.log", "ymd_hms_dot_f", bom=b"\xef\xbb\xbf",
                   notes="UTF-8 with BOM; offsets include the 3 BOM bytes")
    emit_series(tf, fmt, base, 40, rng)
    files.append(tf.write(out))

    tf = TruthFile("utf16le.log", "ymd_hms_dot_f", encoding="utf-16-le",
                   newline=b"\n\x00", bom=b"\xff\xfe",
                   notes="UTF-16LE; 2-byte terminators, offsets are byte offsets")
    emit_series(tf, fmt, base, 40, rng)
    files.append(tf.write(out))

    tf = TruthFile("cp1252.log", "ymd_hms_dot_f", encoding="cp1252", newline=b"\r\n",
                   notes="legacy codepage with CRLF")
    for i, sev in enumerate(sev_stream(rng, 40)):
        t = truncate(base + timedelta(seconds=i), fmt.res_us)
        txt = fmt.render(t, sev, rng) + "  (caf\u00e9 \u00ab\u00bb \u2014 na\u00efve)"
        tf.add_entry([txt], wall_us(t), sev)
    files.append(tf.write(out))

    tf = TruthFile("invalid_utf8.log", "ymd_hms_dot_f",
                   notes="valid UTF-8 except one line with a lone 0x80 byte; must not crash")
    emit_series(tf, fmt, base, 20, rng)
    t = truncate(base + timedelta(seconds=50), fmt.res_us)
    tf.entries.append(EntryTruth(tf._next_line, 1, len(tf.buf), wall_us(t), SEV_WARN))
    tf.buf += (f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond:06d} WARN bad byte -> ".encode()
               + b"\x80\xfe" + b" <- there\n")
    tf._next_line += 1
    emit_series(tf, fmt, base + timedelta(seconds=60), 20, rng)
    files.append(tf.write(out))

    # Severity at a variable byte offset. Every other format here has a
    # fixed-width prefix, which lets the fixed-offset severity fast path win;
    # this one defeats it, so the regex fallback stays exercised.
    tf = TruthFile("variable_sev_pos.log", "ymd_hms_dot_f",
                   notes="level follows a variable-length logger name, so its "
                         "byte offset varies; forces the severity regex fallback")
    loggers = ["com.acme.Gw", "com.acme.pool.ConnectionManager", "db",
               "io.netty.Channel", "Main"]
    for i, sev in enumerate(sev_stream(rng, 80)):
        t = truncate(base + timedelta(seconds=i), fmt.res_us)
        logger = loggers[i % len(loggers)]
        txt = (f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond:06d} [{logger}] "
               f"{SEV_NAME[sev]} {_msg(rng, sev)}")
        tf.add_entry([txt], wall_us(t), sev)
    files.append(tf.write(out))

    # CSV with a quoted embedded newline (SPEC 3.2).
    tf = TruthFile("csv_quoted.csv", "csv",
                   notes="one record's message field contains a quoted newline")
    tf.add_untracked(["timestamp,level,service,message"])
    cfmt = FORMATS_BY_NAME["csv"]
    for i, sev in enumerate(sev_stream(rng, 25)):
        t = truncate(base + timedelta(seconds=i), cfmt.res_us)
        tf.add_entry([cfmt.render(t, sev, rng)], wall_us(t), sev)
    t = truncate(base + timedelta(seconds=90), cfmt.res_us)
    tf.add_entry([f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond // 1000:03d},ERROR,gateway,"
                  f'"first half', 'second half"'], wall_us(t), SEV_ERROR)
    files.append(tf.write(out))

    # JSONL with varying key order and missing keys.
    tf = TruthFile("jsonl_varying.jsonl", "jsonl",
                   notes="key order varies per record; some records omit optional keys")
    jfmt = FORMATS_BY_NAME["jsonl"]
    for i, sev in enumerate(sev_stream(rng, 60)):
        t = truncate(base + timedelta(seconds=i), jfmt.res_us)
        rec = {"ts": f"{t:%Y-%m-%dT%H:%M:%S}.{t.microsecond // 1000:03d}",
               "level": SEV_NAME[sev].lower(), "msg": _msg(rng, sev)}
        if i % 3:
            rec["svc"] = "gateway"
        if i % 4:
            rec["cid"] = f"{rng.randrange(16 ** 8):08x}"
        items = list(rec.items())
        if i % 5 == 0:
            rng.shuffle(items)                  # ts no longer the first key
        tf.add_entry([json.dumps(dict(items), separators=(",", ":"))], wall_us(t), sev)
    files.append(tf.write(out))

    return {"files": files, "merged": None}


# ---------------------------------------------------------------------------
# performance tiers
# ---------------------------------------------------------------------------

SIZE_UNITS = {"kb": 1 << 10, "mb": 1 << 20, "gb": 1 << 30}


def parse_size(text: str) -> int:
    t = text.strip().lower()
    for unit, mult in SIZE_UNITS.items():
        if t.endswith(unit):
            return int(float(t[: -len(unit)]) * mult)
    return int(t)


def gen_perf_file(path: Path, target_bytes: int, rng: random.Random,
                  *, sample_every: int = 50_000, span_hours: int = 24) -> FileTruth:
    """
    A single large file in ``ymd_hms_dot_f`` -- the fixed-position format the
    vectorised fast path targets, so the benchmark measures the intended path.

    Truth is sampled, not exhaustive: a 1GB file has ~9M entries and a full
    manifest would be larger than the log. Every ``sample_every``-th entry is
    recorded, which is enough to validate counts, offsets and ordering.
    """
    sevs = sev_stream(rng, 4096)
    msgs = [_msg(rng, s) for s in sevs]
    est_line = 90
    est_n = max(1, target_bytes // est_line)
    step_us = max(1, (span_hours * 3_600_000_000) // est_n)

    t0 = datetime(2026, 10, 4, 0, 0, 0)
    base_us = wall_us(t0)

    entries: list[EntryTruth] = []
    written = 0
    line_no = 1
    idx = 0
    buf = bytearray()
    CHUNK = 1 << 22  # 4MB

    with path.open("wb") as fh:
        while written < target_bytes:
            k = idx & 4095
            sev = sevs[k]
            us_total = base_us + idx * step_us
            # Direct integer formatting; strftime is far too slow at this volume.
            secs, us = divmod(us_total, 1_000_000)
            dt = EPOCH + timedelta(seconds=secs)
            line = (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} "
                    f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{us:06d} "
                    f"{SEV_NAME[sev]} [{idx:09d}] {msgs[k]}")
            raw = line.encode("ascii") + b"\n"

            if idx % sample_every == 0:
                entries.append(EntryTruth(line_no, 1, written, us_total, sev))

            buf += raw
            written += len(raw)
            line_no += 1
            idx += 1

            if len(buf) >= CHUNK:
                fh.write(buf)
                buf.clear()
        if buf:
            fh.write(buf)

    return FileTruth(
        name=path.name, format="ymd_hms_dot_f", size=written, entry_count=idx,
        line_count=idx, sample_every=sample_every,
        notes=f"perf tier; truth sampled every {sample_every} entries; "
              f"step_us={step_us}; all entries single-line",
        entries=[asdict(e) for e in entries],
    )


def scenario_perf(out: Path, rng: random.Random, target_bytes: int,
                  multi: bool) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    files = [gen_perf_file(out / "single.log", target_bytes, rng)]
    if multi:
        sub = out / "multi"
        sub.mkdir(exist_ok=True)
        each = max(1 << 10, target_bytes // 10)
        for i in range(10):
            files.append(gen_perf_file(sub / f"part{i:02d}.log", each, rng))
            files[-1].name = f"multi/part{i:02d}.log"
    return {"files": files, "merged": None}


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

SCENARIOS = {
    "basic": scenario_basic,
    "formats": scenario_formats,
    "edge": scenario_edge,
}


def write_manifest(directory: Path, scenario: str, seed: int, result: dict) -> Path:
    manifest = {
        "scenario": scenario,
        "generator_version": 1,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": seed,
        "local_tz_offset_s": local_offset_seconds(),
        "severity_names": {str(k): v for k, v in SEV_NAME.items()},
        "files": [asdict(f) if not isinstance(f, dict) else f for f in result["files"]],
        "merged": result["merged"],
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the LogWeave test corpus with ground truth.")
    ap.add_argument("--out", required=True, type=Path, help="output directory")
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--only", default="", help="comma list of scenarios (basic,formats,edge,perf)")
    ap.add_argument("--perf", default="", help="comma list of perf tiers, e.g. 1mb,100mb,1gb")
    ap.add_argument("--perf-multi", action="store_true",
                    help="also generate the 10-file set per tier (NFR-2)")
    args = ap.parse_args(argv)

    wanted = [s.strip() for s in args.only.split(",") if s.strip()] or list(SCENARIOS)
    tiers = [t.strip() for t in args.perf.split(",") if t.strip()]

    args.out.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    t_start = time.perf_counter()

    for name in wanted:
        if name == "perf":
            continue
        if name not in SCENARIOS:
            print(f"unknown scenario: {name}", file=sys.stderr)
            return 2
        # crc32, not hash(): str hashing is salted per process by
        # PYTHONHASHSEED, so --seed did not actually reproduce a corpus -- two
        # runs with identical arguments differed. Ground truth was always
        # self-consistent, but a flaky test could not be re-run to diagnose.
        # (The old expression also mis-bound: & binds tighter than ^.)
        rng = random.Random(args.seed ^ zlib.crc32(name.encode()))
        directory = args.out / name
        t0 = time.perf_counter()
        result = SCENARIOS[name](directory, rng)
        write_manifest(directory, name, args.seed, result)
        size = sum(f.size for f in result["files"])
        total_bytes += size
        print(f"  {name:10s} {len(result['files']):3d} files  {size / 1e6:8.2f} MB  "
              f"{time.perf_counter() - t0:6.2f}s")

    for tier in tiers:
        target = parse_size(tier)
        rng = random.Random(args.seed ^ target)
        directory = args.out / f"perf-{tier.lower()}"
        t0 = time.perf_counter()
        result = scenario_perf(directory, rng, target, args.perf_multi)
        write_manifest(directory, f"perf-{tier.lower()}", args.seed, result)
        size = sum(f.size for f in result["files"])
        total_bytes += size
        print(f"  perf-{tier.lower():6s} {len(result['files']):3d} files  {size / 1e6:8.2f} MB  "
              f"{time.perf_counter() - t0:6.2f}s")

    print(f"\n{total_bytes / 1e6:.2f} MB generated in "
          f"{time.perf_counter() - t_start:.2f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
