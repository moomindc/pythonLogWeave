"""
Severity classification -- SPEC 10.3, D11, FR-11.

Every row needs a severity class, not just the visible ones: the minimap and the
timeline colour the whole file, so this cannot be deferred to paint time
(SPEC 10.3). That makes it part of the indexing budget, and at M0 it was the
single thing standing between the pipeline and NFR-3 -- a regex over every byte
of every chunk measured ~8 MB/s and dragged total indexing to 7.4 MB/s against a
100 MB/s target.

Two paths, therefore:

* `severity_at_offset` -- the fast path, used when the level word sits at a
  constant byte offset, which is true of nearly every real log format once a
  fixed-width timestamp precedes it. Measured 320 MB/s at M0.
* `severity_scan` -- the general fallback, for formats where the level's
  position is not predictable (a variable-length logger name before it).
  Measured 7.4 MB/s, which is risk R11 in the PRD; it is correct but slow, and
  SPEC 14.2 item 1 is the work to vectorise it.

D11: the eight classes are fixed, because `flags` bits 0-2 hold them (SPEC 2.3)
and the palette has eight entries. The *words* mapped onto them are a ruleset
and are fully user-extensible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

import numpy as np


class Severity(IntEnum):
    """
    The eight fixed classes (D11). Ordered, so `max` is meaningful and a
    "at least WARNING" filter is a single comparison.
    """

    NONE = 0
    TRACE = 1
    DEBUG = 2
    INFO = 3
    NOTICE = 4
    WARNING = 5
    ERROR = 6
    CRITICAL = 7


DEFAULT_WORDS: dict[int, tuple[str, ...]] = {
    Severity.CRITICAL: ("FATAL", "CRITICAL", "CRIT", "SEVERE", "EMERG", "ALERT", "PANIC"),
    Severity.ERROR: ("ERROR", "ERR", "FAIL", "FAILED", "FAILURE", "EXCEPTION"),
    Severity.WARNING: ("WARNING", "WARN"),
    Severity.NOTICE: ("NOTICE",),
    Severity.INFO: ("INFO", "INFORMATION", "INF"),
    Severity.DEBUG: ("DEBUG", "DBG", "VERBOSE"),
    Severity.TRACE: ("TRACE", "TRC", "FINEST"),
}
"""
The shipped default ruleset (D11). Deliberately wider than M0's prototype set:
`ERR`, `CRIT` and `EMERG` are what syslog actually writes, and `EXCEPTION` and
`FAIL` are what application logs actually write.
"""


@dataclass
class SeverityRuleset:
    """
    A set of words mapped onto the eight classes, plus the matchers built from
    it.

    Constructed once per session and shared; building the regex is not free, and
    the word table is consulted by the fast path for every chunk.
    """

    words: dict[int, tuple[str, ...]]
    case_sensitive: bool = False

    def fingerprint(self) -> str:
        """
        A digest of the words, for the sidecar header (SPEC 5.4).

        `flags` holds the severity class of every entry, computed with *these*
        words at index time. Change the words and a cached index silently
        disagrees with the rules now in force -- a row the user has told the
        application to call an error still reads as INFO, and no size, mtime or
        head check can see it, because nothing about the file changed. So the
        digest travels with the index and a mismatch invalidates it.

        Case sensitivity is in the digest too: the same words matched case
        sensitively produce different flags.
        """
        import hashlib

        parts = [f"{int(level)}:{','.join(sorted(w.upper() for w in words))}"
                 for level, words in sorted(self.words.items())]
        parts.append(f"case={int(self.case_sensitive)}")
        return hashlib.blake2b(";".join(parts).encode("utf-8"),
                               digest_size=8).hexdigest()

    def __post_init__(self) -> None:
        unknown = set(self.words) - set(Severity)
        if unknown:
            raise ValueError(f"severity classes must be 0-7 (D11); got {sorted(unknown)}")

        # Highest class first, so that on a line reading "ERROR: INFO message"
        # the more severe word wins regardless of position.
        self._by_level: list[tuple[int, tuple[bytes, ...]]] = [
            (int(lvl), tuple(w.upper().encode("ascii") for w in self.words[lvl]))
            for lvl in sorted(self.words, reverse=True)
            if lvl != Severity.NONE and self.words[lvl]
        ]
        self.max_word = max(
            (len(w) for _, ws in self._by_level for w in ws), default=0
        )

        # A longer alternative must precede a shorter one that prefixes it, or
        # the regex engine commits to "WARN" and the trailing \b then rejects
        # "WARNING" outright rather than backtracking into it.
        parts = []
        for lvl, ws in self._by_level:
            alts = b"|".join(sorted(ws, key=len, reverse=True))
            parts.append(b"(?P<s%d>%s)" % (lvl, alts))
        body = rb"\b(?:" + b"|".join(parts) + rb")\b"
        flags = 0 if self.case_sensitive else re.IGNORECASE
        self.pattern = re.compile(body, flags)
        self._group_level = {f"s{lvl}": lvl for lvl, _ in self._by_level}

    @property
    def by_level(self) -> list[tuple[int, tuple[bytes, ...]]]:
        """Words as upper-cased bytes, highest class first."""
        return self._by_level

    def level_of_group(self, name: str) -> int:
        return self._group_level[name]


DEFAULT_RULESET = SeverityRuleset(DEFAULT_WORDS)


# ---------------------------------------------------------------------------
# position detection
# ---------------------------------------------------------------------------

def detect_severity_offset(
    chunk: bytes,
    starts: np.ndarray,
    lens: np.ndarray,
    *,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
    sample: int = 400,
    agreement: float = 0.9,
    min_found: int = 20,
) -> int | None:
    """
    Decide whether the level word sits at a fixed byte offset from line start.

    SPEC 4.3 step 7, added at M0 once the regex path proved too slow to ship.
    The modal offset must account for at least `agreement` of the lines that
    have a level word at all -- lines with no level word are not evidence either
    way, since a continuation line or a banner legitimately has none.

    `min_found` guards against promoting a coincidence: three matching lines in a
    400-line sample agreeing on an offset says nothing.

    Returns the offset, or None if the position is not predictable.
    """
    n = min(sample, int(starts.size))
    if n == 0:
        return None

    offsets: dict[int, int] = {}
    found = 0
    for i in range(n):
        a, ln = int(starts[i]), int(lens[i])
        m = ruleset.pattern.search(chunk, a, a + ln)
        if m is None:
            continue
        found += 1
        off = m.start() - a
        offsets[off] = offsets.get(off, 0) + 1

    if found < min_found:
        return None
    best_off, best_count = max(offsets.items(), key=lambda kv: kv[1])
    return best_off if best_count >= agreement * found else None


# ---------------------------------------------------------------------------
# the fast path
# ---------------------------------------------------------------------------

def severity_at_offset(
    buf: np.ndarray,
    starts: np.ndarray,
    lens: np.ndarray,
    offset: int,
    *,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
) -> np.ndarray:
    """
    Vectorised severity classification for a fixed level-word offset.

    The severity analogue of the SPEC 4.4 timestamp fast path, and what makes
    NFR-3 reachable. Instead of scanning every byte, it gathers only the handful
    of columns where a level word can begin -- `max_word + 1` of them -- and
    compares each candidate word against them. The work is a few dozen passes
    over 1-byte arrays rather than a regex over the chunk.
    """
    n = int(starts.size)
    sev = np.zeros(n, dtype=np.uint8)
    if n == 0 or ruleset.max_word == 0:
        return sev

    idx = np.empty(n, dtype=np.int64)
    limit = buf.size - 1
    cols: dict[int, np.ndarray] = {}

    def col(j: int) -> np.ndarray:
        """
        Column `j` past the level offset, case-folded, gathered on first use.

        Lazily, because a ruleset's longest word sets how many columns *could*
        be needed and a real file needs far fewer: a log writing only INFO and
        ERROR never looks past column 5, so eagerly gathering all twelve
        columns of an eleven-letter word like INFORMATION is most of the work
        thrown away.
        """
        got = cols.get(j)
        if got is None:
            np.add(starts, offset + j, out=idx)
            # Unlike the timestamp kernel there is no single length guard to
            # lean on, because a short line is still a legitimate row that
            # simply has no level word; clipping keeps the gather in bounds and
            # the per-word length test below discards whatever it read.
            np.clip(idx, 0, limit, out=idx)
            c = buf[idx]
            got = np.where((c >= 97) & (c <= 122), c - 32, c).astype(np.uint8)
            cols[j] = got
        return got

    first = col(0)
    for level, words in ruleset.by_level:
        for w in words:
            length = len(w)
            # Reject on the first letter before doing any other work. Most words
            # in a ruleset do not occur in a given file at all -- a default
            # ruleset carries two dozen, a log typically writes three or four --
            # and this turns each absent word from one pass per letter into a
            # single pass, which is what keeps a generous default ruleset (D11)
            # from costing throughput.
            head = first == w[0]
            if not head.any():
                continue
            m = head & (lens >= offset + length)
            for j, ch in enumerate(w[1:], start=1):
                m &= col(j) == ch
            # Trailing word boundary: either the line ends here, or the next
            # byte is not word-forming. Without this, DEBUGGER reads as DEBUG
            # and INFORMATIONAL reads as INFO.
            nxt = col(length)
            not_word = ~(
                ((nxt >= 65) & (nxt <= 90))
                | ((nxt >= 48) & (nxt <= 57))
                | (nxt == 95)
            )
            m &= (lens == offset + length) | not_word
            # Classes are visited highest first, so the first class to claim a
            # line keeps it.
            np.copyto(sev, np.uint8(level), where=m & (sev == 0))
    return sev


# ---------------------------------------------------------------------------
# the general fallback (risk R11)
# ---------------------------------------------------------------------------

def severity_scan(
    chunk: bytes,
    starts: np.ndarray,
    n_lines: int,
    *,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
) -> np.ndarray:
    """
    Classify every line by scanning the chunk, for formats with no fixed offset.

    One C-level `finditer` over the whole chunk, `searchsorted` to map match
    offsets back to line indices, then a sort-based fold keeping the highest
    class per line. `np.maximum.at` expresses that fold directly but is an
    unbuffered ufunc call and benchmarked far slower, so the lexsort stands.

    This is risk R11: correct, but ~7.4 MB/s single-core, below NFR-3.
    """
    hits = [(m.start(), ruleset.level_of_group(m.lastgroup))
            for m in ruleset.pattern.finditer(chunk)]
    sev = np.zeros(n_lines, dtype=np.uint8)
    if not hits or n_lines == 0:
        return sev

    arr = np.asarray(hits, dtype=np.int64)
    offs, lvls = arr[:, 0], arr[:, 1].astype(np.uint8)
    line_of = np.searchsorted(starts, offs, side="right") - 1
    np.clip(line_of, 0, n_lines - 1, out=line_of)

    order = np.lexsort((lvls, line_of))
    ls, vs = line_of[order], lvls[order]
    last = np.empty(ls.size, dtype=bool)
    last[-1] = True
    last[:-1] = ls[1:] != ls[:-1]
    sev[ls[last]] = vs[last]
    return sev


def classify(
    chunk: bytes,
    buf: np.ndarray,
    starts: np.ndarray,
    lens: np.ndarray,
    offset: int | None,
    *,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
) -> np.ndarray:
    """Take the fast path when an offset was detected, the scan when not."""
    if offset is None:
        return severity_scan(chunk, starts, int(starts.size), ruleset=ruleset)
    return severity_at_offset(buf, starts, lens, offset, ruleset=ruleset)
