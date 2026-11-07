"""
Candidate kernels for the LogWeave index and merge engine  --  milestone M0.

WHY THIS FILE EXISTS, AND WHY IT IS IN tools/ RATHER THAN logweave/
-------------------------------------------------------------------
PRD risk R1 is that Python cannot reach the >=100 MB/s indexing throughput that
NFR-3 demands, and PRD D8 says no native code is written until a benchmark proves
it is needed. That decision has to be made *before* M1 commits to an architecture,
so these are reference implementations of the SPEC 4.4 / 6.2 designs, written to
be measured rather than shipped.

M1 replaces them with the real ``logweave.index.*`` and ``logweave.merge.*``
modules. These stay as the cross-check: the real implementation must agree with
them entry-for-entry on the generated corpus.

Everything here is deliberately pure Python + NumPy, with no Qt, no Cython and no
native extension, because that is the hypothesis under test.
"""

from __future__ import annotations

import re
from datetime import datetime

import numpy as np

EPOCH = datetime(1970, 1, 1)

# ---------------------------------------------------------------------------
# line splitting (SPEC 4.4)
# ---------------------------------------------------------------------------

def detect_lone_cr(buf: np.ndarray) -> bool:
    """
    True if the chunk contains a CR that is not part of a CRLF.

    SPEC 3.3 calls for the lone-CR path to be taken only when a file needs it,
    because supporting it costs a second scan. Detection runs once per file, on
    the first chunk.
    """
    cr = np.flatnonzero(buf == 0x0D)
    if cr.size == 0:
        return False
    at_end = cr == buf.size - 1
    nxt = buf[np.minimum(cr + 1, buf.size - 1)]
    return bool(np.any((nxt != 0x0A) | at_end))


def split_lines(buf: np.ndarray, lone_cr: bool | None = None) -> tuple[np.ndarray, np.ndarray]:
    """
    Split a chunk into complete lines.

    ``buf`` is a uint8 view of the chunk. Returns ``(starts, lens)`` where
    ``lens`` excludes the terminator and any preceding CR, so CRLF and LF files
    behave identically (FR-6).

    ``lone_cr`` selects the terminator rule: False for the LF-only fast path,
    True to also treat a CR not followed by LF as a terminator (classic Mac
    endings, and the mixed-ending files FR-6 requires), or None to detect it.
    The fast path is one comparison pass; the lone-CR path needs a second.

    An unterminated trailing line is deliberately excluded -- that is the
    partial-line rule from SPEC 7.3, and it is what stops a half-written line
    being indexed and then duplicated when the writer finishes it.
    """
    if lone_cr is None:
        lone_cr = detect_lone_cr(buf)

    term = np.flatnonzero(buf == 0x0A)
    if lone_cr:
        cr = np.flatnonzero(buf == 0x0D)
        if cr.size:
            at_end = cr == buf.size - 1
            nxt = buf[np.minimum(cr + 1, buf.size - 1)]
            lone = cr[(nxt != 0x0A) | at_end]
            if lone.size:
                term = np.sort(np.concatenate([term, lone]))

    n = term.size
    if n == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    starts = np.empty(n, dtype=np.int64)
    starts[0] = 0
    starts[1:] = term[:-1] + 1
    lens = term - starts
    # Strip a CR that belongs to a CRLF pair. Gated on the terminator actually
    # being an LF, so a lone-CR terminator is not shortened by mistake.
    is_lf = buf[term] == 0x0A
    has_cr = (buf[np.maximum(term - 1, 0)] == 0x0D) & is_lf
    return starts, lens - has_cr


# ---------------------------------------------------------------------------
# civil date -> day number (SPEC 2.2)
# ---------------------------------------------------------------------------

def days_from_civil(y, m, d):
    """
    Vectorised Howard Hinnant days-from-civil. Branch-free integer arithmetic,
    no datetime objects, proleptic Gregorian.

    Floor division is used throughout rather than the C formulation's truncating
    division, because ``//`` on a NumPy integer array floors and the two agree
    for every year this will ever see.
    """
    y = np.asarray(y, dtype=np.int64)
    m = np.asarray(m, dtype=np.int64)
    d = np.asarray(d, dtype=np.int64)
    yy = y - (m <= 2)
    era = yy // 400
    yoe = yy - era * 400
    mp = m + np.where(m > 2, -3, 9)
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def days_from_civil_scalar(y: int, m: int, d: int) -> int:
    yy = y - (1 if m <= 2 else 0)
    era = yy // 400
    yoe = yy - era * 400
    mp = m - 3 if m > 2 else m + 9
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


# ---------------------------------------------------------------------------
# timestamp extraction -- the vectorised fast path (SPEC 4.4)
# ---------------------------------------------------------------------------

ISO_W = 26  # len("YYYY-MM-DD HH:MM:SS.ffffff")
_ISO_SEPS = ((4, 0x2D), (7, 0x2D), (10, 0x20), (13, 0x3A), (16, 0x3A), (19, 0x2E))
_ISO_DIGITS = (0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18, 20, 21, 22, 23, 24, 25)


def extract_fixed_iso(buf: np.ndarray, starts: np.ndarray,
                      lens: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Parse ``YYYY-MM-DD HH:MM:SS.ffffff`` at offset 0 of every line, for every
    line at once.

    This is the mechanism NFR-3 rests on: one gather of the timestamp field into
    an (n, 26) byte array, then pure column arithmetic. No Python loop over
    lines, no regex, no strptime, no datetime.

    Returns ``(ts, valid)``. ``valid`` is False for a line that does not carry a
    well-formed timestamp -- which is exactly the continuation-line test from
    FR-15, obtained for free.
    """
    n = starts.size
    if n == 0:
        return np.zeros(0, np.int64), np.zeros(0, bool)

    # A line shorter than the field cannot hold it. Checking this explicitly
    # stops a short line from borrowing bytes out of its successor, and it also
    # removes the need to clip every gather: every line in `starts` is
    # terminated, so a line that clears ISO_W is guaranteed in range, while a
    # short line is redirected to offset 0, reads garbage, and is already
    # excluded here. Dropping the per-column clip was worth ~25% of the kernel.
    long_enough = lens >= ISO_W
    base = np.where(long_enough, starts, 0)
    idx = np.empty(n, dtype=np.int64)

    def col(j: int) -> np.ndarray:
        np.add(base, j, out=idx)
        return buf[idx]

    valid = long_enough.copy()
    for pos, ch in _ISO_SEPS:
        valid &= col(pos) == ch

    # One unsigned comparison per digit instead of two: for a byte below '0' the
    # subtraction wraps to >= 246, so the upper bound alone rejects it. The
    # subtracted value is then reused as the digit, saving 20 further passes.
    d: dict[int, np.ndarray] = {}
    for pos in _ISO_DIGITS:
        v = col(pos) - np.uint8(48)
        valid &= v <= np.uint8(9)
        d[pos] = v

    def pair(a: int, b: int) -> np.ndarray:
        """Two digits as one uint8 (<= 99), keeping the work in 1-byte lanes."""
        return d[a] * np.uint8(10) + d[b]

    year = pair(0, 1).astype(np.int32) * 100 + pair(2, 3)
    month = pair(5, 6).astype(np.int32)
    day = pair(8, 9).astype(np.int32)
    hh = pair(11, 12).astype(np.int32)
    mm = pair(14, 15).astype(np.int32)
    ss = pair(17, 18).astype(np.int32)
    us = (pair(20, 21).astype(np.int32) * 100 + pair(22, 23)) * 100 + pair(24, 25)

    valid &= (month >= 1) & (month <= 12) & (day >= 1) & (day <= 31)
    valid &= (hh <= 23) & (mm <= 59) & (ss <= 60)

    key = year * 10000 + month * 100 + day
    kv = key[valid]
    if kv.size == 0:
        return np.zeros(n, np.int64), valid

    sod = hh * 3600 + mm * 60 + ss                    # second of day, <= 86400

    # Constant-date fast path. A 64MB chunk of one log almost always covers a
    # single calendar day, so the day number is computed once as a scalar rather
    # than for every line -- days_from_civil was 75ms of a 511ms kernel. A chunk
    # that crosses midnight falls back to the vector form, which is still right,
    # just slower.
    if bool((kv == kv[0]).all()):
        first = int(np.argmax(valid))
        days = np.int64(days_from_civil_scalar(
            int(year[first]), int(month[first]), int(day[first])))
    else:
        days = days_from_civil(year, month, day)

    ts = (days * 86400 + sod) * 1_000_000 + us
    return ts, valid


def entries_from(ts: np.ndarray, valid: np.ndarray, starts: np.ndarray,
                 chunk_base_line: int = 0, chunk_base_off: int = 0):
    """
    Collapse lines into entries (SPEC 4.4). A timestamped line opens an entry;
    untimestamped lines that follow belong to it (FR-15).

    ``np.flatnonzero(valid)`` *is* the line ordinal of each entry within the
    chunk, so ``line_no`` (D9) costs nothing beyond the chunk-base add.
    """
    sel = np.flatnonzero(valid)
    line_no = (sel + chunk_base_line + 1).astype(np.uint32)   # 1-based
    offsets = (starts[sel] + chunk_base_off).astype(np.uint64)
    return line_no, offsets, ts[sel]


# ---------------------------------------------------------------------------
# timestamp extraction -- scalar baselines, for comparison only
# ---------------------------------------------------------------------------

_SCALAR_RE = re.compile(rb"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{6})")


def extract_scalar_regex(chunk: bytes, starts: np.ndarray):
    """Per-line compiled regex plus integer arithmetic. No strptime."""
    out: list[int] = []
    ok: list[bool] = []
    match = _SCALAR_RE.match
    for a in starts.tolist():
        m = match(chunk, a, a + ISO_W)
        if m is None:
            out.append(0)
            ok.append(False)
            continue
        y, mo, d, h, mi, s, us = (int(g) for g in m.groups())
        days = days_from_civil_scalar(y, mo, d)
        out.append((days * 86400 + h * 3600 + mi * 60 + s) * 1_000_000 + us)
        ok.append(True)
    return np.asarray(out, np.int64), np.asarray(ok, bool)


def extract_scalar_strptime(chunk: bytes, starts: np.ndarray):
    """What logmerger does today. Included to quantify what the fast path buys."""
    out: list[int] = []
    ok: list[bool] = []
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    for a in starts.tolist():
        raw = chunk[a:a + ISO_W]
        try:
            dt = datetime.strptime(raw.decode("ascii"), fmt)
        except (ValueError, UnicodeDecodeError):
            out.append(0)
            ok.append(False)
            continue
        td = dt - EPOCH
        out.append(td.days * 86_400_000_000 + td.seconds * 1_000_000 + td.microseconds)
        ok.append(True)
    return np.asarray(out, np.int64), np.asarray(ok, bool)


# ---------------------------------------------------------------------------
# severity scan (SPEC 10.3)
# ---------------------------------------------------------------------------

_SEV_PATTERNS = [
    (7, rb"FATAL|CRITICAL|SEVERE"),
    (6, rb"ERROR"),
    (5, rb"WARNING|WARN"),
    (4, rb"NOTICE"),
    (3, rb"INFO"),
    (2, rb"DEBUG"),
    (1, rb"TRACE"),
]
_SEV_BODY = rb"\b(?:" + b"|".join(b"(?P<s%d>%s)" % (lvl, pat)
                                 for lvl, pat in _SEV_PATTERNS) + rb")\b"
SEV_RE_INSENSITIVE = re.compile(_SEV_BODY, re.IGNORECASE)
SEV_RE_SENSITIVE = re.compile(_SEV_BODY)
_GROUP_SEV = {f"s{lvl}": lvl for lvl, _ in _SEV_PATTERNS}


def severity_scan(chunk: bytes, starts: np.ndarray, n_lines: int, *,
                  pattern: re.Pattern | None = None,
                  method: str = "lexsort") -> np.ndarray:
    """
    Classify every line's severity in one pass over the chunk bytes.

    The strips need severity for *every* row, not just the visible ones, so this
    cannot be deferred to paint time (SPEC 10.3). One C-level scan of the whole
    chunk, then ``searchsorted`` to map match offsets back to line indices, then
    a fold that keeps the highest severity per line.

    ``method`` selects the fold: ``"lexsort"`` or ``"maxat"``. ``np.maximum.at``
    is the obvious expression of it but is an unbuffered ufunc call and is
    usually far slower; the benchmark settles it.
    """
    rx = pattern or SEV_RE_INSENSITIVE
    hits = [(m.start(), _GROUP_SEV[m.lastgroup]) for m in rx.finditer(chunk)]
    sev = np.zeros(n_lines, np.uint8)
    if not hits or n_lines == 0:
        return sev

    arr = np.asarray(hits, dtype=np.int64)
    offs, lvls = arr[:, 0], arr[:, 1].astype(np.uint8)
    line_of = np.searchsorted(starts, offs, side="right") - 1
    np.clip(line_of, 0, n_lines - 1, out=line_of)

    if method == "maxat":
        np.maximum.at(sev, line_of, lvls)
    else:
        order = np.lexsort((lvls, line_of))
        ls, vs = line_of[order], lvls[order]
        last = np.empty(ls.size, bool)
        last[-1] = True
        last[:-1] = ls[1:] != ls[:-1]
        sev[ls[last]] = vs[last]
    return sev


# Highest severity first, so the first match on a line wins.
SEV_WORDS_BY_LEVEL: list[tuple[int, tuple[bytes, ...]]] = [
    (7, (b"CRITICAL", b"SEVERE", b"FATAL")),
    (6, (b"ERROR",)),
    (5, (b"WARNING", b"WARN")),
    (4, (b"NOTICE",)),
    (3, (b"INFO",)),
    (2, (b"DEBUG",)),
    (1, (b"TRACE",)),
]
_MAX_SEV_WORD = max(len(w) for _, ws in SEV_WORDS_BY_LEVEL for w in ws)


def detect_severity_offset(chunk: bytes, starts: np.ndarray, lens: np.ndarray,
                           sample: int = 400, agreement: float = 0.9) -> int | None:
    """
    Decide whether the level word sits at a fixed byte offset from line start.

    Once a fixed-width timestamp precedes it -- which SPEC 4.3 step 6 already
    detects for the timestamp itself -- the level is at a constant offset for
    the overwhelming majority of real formats. Detecting that unlocks
    ``severity_at_offset`` and removes the regex from the hot path entirely.

    Returns the offset, or None if the position is not predictable.
    """
    n = min(sample, starts.size)
    if n == 0:
        return None
    offsets: dict[int, int] = {}
    found = 0
    for i in range(n):
        a, ln = int(starts[i]), int(lens[i])
        m = SEV_RE_INSENSITIVE.search(chunk, a, a + ln)
        if m is None:
            continue
        found += 1
        off = m.start() - a
        offsets[off] = offsets.get(off, 0) + 1
    if not found:
        return None
    best_off, best_count = max(offsets.items(), key=lambda kv: kv[1])
    return best_off if best_count >= agreement * found else None


def severity_at_offset(buf: np.ndarray, starts: np.ndarray, lens: np.ndarray,
                       offset: int) -> np.ndarray:
    """
    Vectorised severity classification for a fixed level-word offset.

    This is the severity analogue of the SPEC 4.4 timestamp fast path, and it is
    what makes NFR-3 reachable: the M0 benchmark measured the general
    ``severity_scan`` at ~8 MB/s, which alone put total indexing at 7.4 MB/s
    against a 100 MB/s budget. A regex over every byte of every chunk was never
    going to work; gathering the handful of bytes where the word can start, and
    comparing them, is a few dozen passes over 1-byte arrays.

    ``severity_scan`` remains the fallback for formats whose level position is
    not predictable.
    """
    n = starts.size
    sev = np.zeros(n, dtype=np.uint8)
    if n == 0:
        return sev

    idx = np.empty(n, dtype=np.int64)
    limit = buf.size - 1
    cols: list[np.ndarray] = []
    for j in range(_MAX_SEV_WORD + 1):
        np.add(starts, offset + j, out=idx)
        np.clip(idx, 0, limit, out=idx)
        c = buf[idx]
        # fold to upper case in one pass
        cols.append(np.where((c >= 97) & (c <= 122), c - 32, c).astype(np.uint8))

    for level, words in SEV_WORDS_BY_LEVEL:
        for w in words:
            L = len(w)
            m = lens >= offset + L
            for j, ch in enumerate(w):
                m &= cols[j] == ch
            # Trailing word boundary: either the line ends here, or the next
            # byte is not [A-Z0-9_]. Without this, DEBUGGER would read as DEBUG.
            nxt = cols[L]
            not_word = ~(((nxt >= 65) & (nxt <= 90)) | ((nxt >= 48) & (nxt <= 57))
                         | (nxt == 95))
            m &= (lens == offset + L) | not_word
            np.copyto(sev, np.uint8(level), where=m & (sev == 0))
    return sev


# ---------------------------------------------------------------------------
# density histogram (SPEC 8.1)
# ---------------------------------------------------------------------------

def histogram(ts: np.ndarray, sev: np.ndarray, t0: int, bucket_us: int,
              nbuckets: int) -> np.ndarray:
    """(nbuckets, 8) counts. One structure feeds both overview strips (SPEC 8.2/8.3)."""
    b = (ts - t0) // bucket_us
    np.clip(b, 0, nbuckets - 1, out=b)
    flat = np.bincount(b * 8 + sev.astype(np.int64), minlength=nbuckets * 8)
    return flat[:nbuckets * 8].reshape(nbuckets, 8)


def build_cumulative(hist: np.ndarray) -> np.ndarray:
    """
    Prefix-sum the fine histogram once, so every later strip repaint is a diff.

    Keeping the cumsum out of the repaint is the whole point. The M0 benchmark
    measured a repaint that re-ran ``cumsum`` over a 1M-bucket histogram at
    8.3ms -- half a 60fps frame budget (NFR-5) spent re-deriving data that never
    changed. Rebuilt only when the histogram itself changes, i.e. on append.
    """
    return np.vstack([np.zeros((1, hist.shape[1]), dtype=np.int64),
                      np.cumsum(hist, axis=0, dtype=np.int64)])


def strip_aggregate(cum: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """
    Re-aggregate onto ``len(edges)-1`` strip pixels from a prepared cumulative.

    This is why SPEC 8 claims a repaint is cheap: two fancy-index reads and a
    subtraction, O(pixels), independent of how many log entries there are. Feeds
    both the timeline and the row-proportional minimap (SPEC 8.2/8.3).
    """
    e = np.clip(edges, 0, cum.shape[0] - 1)
    return cum[e[1:]] - cum[e[:-1]]


# ---------------------------------------------------------------------------
# checkpointed k-way merge (SPEC 6.2)
# ---------------------------------------------------------------------------

class MergePrototype:
    """
    Reference implementation of the checkpointed merge.

    The claim under test (SPEC 6.1/6.2) is that the merge order never needs to be
    materialised: storing the cursor state every ``CHECKPOINT`` rows is enough to
    resume it, costing ~1.7MB for 100M rows instead of 800MB.

    Per-file ``ts`` arrays must be non-decreasing. SPEC 4.6 handles the
    out-of-order case with a stored permutation; apply it before constructing.
    """

    CHECKPOINT = 4096

    def __init__(self, ts_arrays, skews=None):
        self.ts = [np.ascontiguousarray(a, dtype=np.int64) for a in ts_arrays]
        self.k = len(self.ts)
        self.skews = list(skews) if skews is not None else [0] * self.k
        self.total = int(sum(a.size for a in self.ts))
        self.cp_row: np.ndarray | None = None
        self.cp_ts: np.ndarray | None = None
        self.cp_cursors: np.ndarray | None = None

    # -- O(k log n), no replay at all. This is NFR-6.
    def time_to_row(self, t: int) -> int:
        return int(sum(int(np.searchsorted(a, t - s, side="left"))
                       for a, s in zip(self.ts, self.skews, strict=True)))

    def cursors_at_time(self, t: int) -> np.ndarray:
        return np.array([np.searchsorted(a, t - s, side="left")
                         for a, s in zip(self.ts, self.skews, strict=True)], dtype=np.int64)

    def _window(self, cur: np.ndarray, take: int):
        segs_ts, segs_f, segs_i = [], [], []
        bound = None
        for f in range(self.k):
            a = self.ts[f]
            lo = int(cur[f])
            hi = min(a.size, lo + take)
            if hi <= lo:
                continue
            segs_ts.append(a[lo:hi] + self.skews[f])
            segs_f.append(np.full(hi - lo, f, dtype=np.int64))
            segs_i.append(np.arange(lo, hi, dtype=np.int64))
            if hi < a.size:
                b = int(segs_ts[-1][-1])
                bound = b if bound is None else min(bound, b)
        ts_all = np.concatenate(segs_ts)
        f_all = np.concatenate(segs_f)
        i_all = np.concatenate(segs_i)
        # primary key is the LAST key given to lexsort
        order = np.lexsort((i_all, f_all, ts_all))
        return ts_all[order], f_all[order], i_all[order], bound

    def build_checkpoints(self, take: int = 200_000) -> None:
        """
        One pass, in windows. Within a window the order is a small lexsort; only
        rows provably ahead of every unread entry are committed, which is what
        makes the window boundary exact even with duplicate timestamps.
        """
        C = self.CHECKPOINT
        cur = np.zeros(self.k, dtype=np.int64)
        rows, tss, curs = [], [], []
        row = 0
        while row < self.total:
            ts_s, f_s, i_s, bound = self._window(cur, take)
            if bound is None:
                commit = ts_s.size
            else:
                commit = int(np.searchsorted(ts_s, bound, side="left"))
                if commit == 0:
                    take *= 2          # a run of identical timestamps; widen
                    continue
            pos = [np.flatnonzero(f_s[:commit] == f) for f in range(self.k)]
            first = (-row) % C          # offset of the first checkpoint in this window
            for j in range(first, commit, C):
                cnt = np.array([np.searchsorted(pos[f], j) for f in range(self.k)],
                               dtype=np.int64)
                rows.append(row + j)
                tss.append(int(ts_s[j]))
                curs.append(cur + cnt)
            consumed = np.array([pos[f].size for f in range(self.k)], dtype=np.int64)
            cur = cur + consumed
            row += commit

        self.cp_row = np.asarray(rows, dtype=np.int64)
        self.cp_ts = np.asarray(tss, dtype=np.int64)
        self.cp_cursors = (np.vstack(curs) if curs
                           else np.zeros((0, self.k), dtype=np.int64))

    def checkpoint_bytes(self) -> int:
        return int(self.cp_row.nbytes + self.cp_ts.nbytes + self.cp_cursors.nbytes)

    def block(self, start_row: int, count: int):
        """
        Merged rows ``[start_row, start_row+count)`` as ``(file_idx, entry_idx, ts)``
        arrays. Replays at most CHECKPOINT rows from the preceding checkpoint; the
        real engine caches whole blocks so a scroll amortises this to nothing.
        """
        if self.cp_row is None:
            raise RuntimeError("build_checkpoints() first")
        c = int(np.searchsorted(self.cp_row, start_row, side="right") - 1)
        cur = self.cp_cursors[c].copy()
        skip = start_row - int(self.cp_row[c])
        need = skip + count
        ts_s, f_s, i_s, _ = self._window(cur, need)
        sl = slice(skip, skip + count)
        return f_s[sl], i_s[sl], ts_s[sl]

    def row_at(self, i: int):
        f, e, t = self.block(i, 1)
        return int(f[0]), int(e[0]), int(t[0])

    def materialise(self):
        """Full merge order. Only for cross-checking the prototype on small inputs."""
        ts_s, f_s, i_s, _ = self._window(np.zeros(self.k, dtype=np.int64), self.total)
        return f_s, i_s, ts_s
