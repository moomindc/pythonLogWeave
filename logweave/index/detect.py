"""
Timestamp format detection -- SPEC 4.2, 4.3.

## What this does differently from logmerger

logmerger pioneered the idea of working the format out rather than asking for
it, and its pattern catalogue is adopted wholesale (see NOTICE). SPEC 4.2 lists
four things that have to change for the scale LogWeave targets:

1. **Detect from a sample, not the first line.** A log's first line is very
   often a banner, a header row, or a restart message with no timestamp at all.
   Here ~300 lines spread across the chunk vote.
2. **Rank by explicit specificity, not declaration order.** With overlapping
   patterns -- a 10-digit epoch is a prefix of a 13-digit one -- resolving ties
   by the order patterns happen to be written in is a latent ordering bug.
   `TimestampFormat.specificity` makes the intent reviewable.
3. **Report confidence.** FR-19: the user is told what was detected and how
   sure it is, and can override. Silence is the failure mode that hurts.
4. **Never `strptime` on the hot path.** Detection may be slow -- it runs once
   per file over a few hundred lines. Extraction may not.

## Why the oracle cross-check is here and not only in the tests

Risk R4 is a format that is *silently* wrong: it parses, it produces plausible
timestamps, and it mis-orders a whole file. A test suite catches that for
formats someone thought to write a test for. So detection itself proves the
synthesised fast-path layout against the regex path on the very sample it was
derived from, and refuses the layout on any disagreement (step 6 below). The
cost is a few hundred regex searches per file, once.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from . import extract as ex
from .layouts import CATALOGUE, Layout, Span, TimestampFormat
from .severity import DEFAULT_RULESET, SeverityRuleset, detect_severity_offset

SAMPLE_LINES = 300
"""SPEC 4.3 step 1. Enough for a stable vote, few enough to stay instant."""

MIN_MATCH_RATE = 0.25
"""FR-19 / SPEC 4.3 step 8: below this, declare no format rather than guess."""

LEADING_SLACK = 64
"""
How far into a line a `line_anchored` format's timestamp may start.

Not zero, because a leading `[` or a short prefix is common and harmless. Not
unbounded, because an unanchored 10-digit epoch pattern will otherwise cheerfully
match a request id two hundred bytes into the message text.
"""

MIN_GROUPS_FOR_VALIDATION = 8
"""Lines that must match before a cross-check verdict is trusted."""

RATE_TOLERANCE = 0.02
"""How much match rate the fast path may give up before scalar is preferred."""

OFFSET_AGREEMENT = 0.9
"""Share of sampled matches that must agree on the timestamp offset."""

# Named groups that carry no value -- present to make the pattern specific, not
# to be read. Their byte positions are "don't care" for layout synthesis.
IGNORED_GROUPS = frozenset({"a", "pid"})

_GROUP_TO_FIELD = {
    "Y": "year", "m": "month", "d": "day",
    "H": "hour", "M": "minute", "S": "second",
    "f": "frac", "z": "tz", "epoch": "epoch",
}


@dataclass
class Candidate:
    fmt: TimestampFormat
    matched: int = 0
    sampled: int = 0

    @property
    def rate(self) -> float:
        return self.matched / self.sampled if self.sampled else 0.0

    @property
    def rank(self) -> tuple:
        # Match rate first: a format that actually fits the file beats a more
        # specific one that fits a quarter of it. Specificity only breaks ties,
        # which is where logmerger's declaration order used to decide.
        return (round(self.rate, 3), self.fmt.specificity)


@dataclass
class Detection:
    """What was worked out about a file, and how much to trust it (FR-19)."""

    fmt: TimestampFormat | None = None
    layout: Layout | None = None
    pattern: re.Pattern[bytes] | None = None
    match_rate: float = 0.0
    sampled: int = 0
    severity_offset: int | None = None
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    runners_up: list[tuple[str, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.fmt is not None

    @property
    def fast_path(self) -> bool:
        return self.layout is not None

    def describe(self) -> str:
        if not self.ok:
            return "no timestamp format detected"
        how = self.layout.describe() if self.layout else f"{self.fmt.name} (scalar)"
        return (f"{how}, {self.match_rate:.0%} of {self.sampled} sampled lines, "
                f"confidence {self.confidence:.0%}")


# ---------------------------------------------------------------------------
# step 1: sampling
# ---------------------------------------------------------------------------

def sample_indices(n: int, limit: int = SAMPLE_LINES) -> np.ndarray:
    """
    Line indices to vote with: evenly spread, not the first `limit`.

    Spreading matters for real files. A banner, a column header, or a block of
    stack trace at the head is not representative, and a file whose format
    changes partway through -- a service restarted under a new logging config --
    shows up as a split vote rather than a confident wrong answer.
    """
    if n <= limit:
        return np.arange(n, dtype=np.int64)
    return np.unique(np.linspace(0, n - 1, limit).astype(np.int64))


# ---------------------------------------------------------------------------
# steps 2-5: vote and rank
# ---------------------------------------------------------------------------

def _scan_candidate(
    chunk: bytes,
    starts: np.ndarray,
    lens: np.ndarray,
    idx: np.ndarray,
    fmt: TimestampFormat,
    pattern: re.Pattern[bytes],
) -> tuple[Candidate, list[re.Match[bytes]], list[int]]:
    """
    Count how many sampled lines this format explains, and keep the matches.

    A match only counts if `scalar_one` can turn it into a plausible timestamp
    (SPEC 4.3 step 4). Matching the *shape* of a date is not enough -- that is
    exactly how a build number becomes a timestamp.
    """
    cand = Candidate(fmt, sampled=int(idx.size))
    matches: list[re.Match[bytes]] = []
    rows: list[int] = []
    slack = LEADING_SLACK if fmt.line_anchored else len(chunk)

    for i in idx:
        a = int(starts[i])
        ln = int(lens[i])
        m = pattern.search(chunk, a, a + min(ln, slack + 64))
        if m is None or m.start() - a > slack:
            continue
        if not ex.scalar_one(m, fmt).ok:
            continue
        cand.matched += 1
        matches.append(m)
        rows.append(int(i))
    return cand, matches, rows


# ---------------------------------------------------------------------------
# step 6: layout synthesis
# ---------------------------------------------------------------------------

def synthesise_layout(
    chunk: bytes,
    starts: np.ndarray,
    lens: np.ndarray,
    fmt: TimestampFormat,
    matches: list[re.Match[bytes]],
    rows: list[int],
) -> tuple[Layout | None, str]:
    """
    Pin a format to the byte positions this file actually uses, or give up.

    The width of a fractional-seconds field is not knowable from the pattern --
    `\\d{1,9}` is whatever the writer chose -- so it is measured here. Any
    inconsistency across the sample means no fixed layout, and the file takes
    the scalar path: slower, but never wrong.

    Returns the layout (or None) and a note explaining the outcome.
    """
    if not matches:
        return None, "no matches to synthesise from"

    widths = {m.end() - m.start() for m in matches}
    if len(widths) != 1:
        return None, f"timestamp width varies ({sorted(widths)}), scalar path"
    width = widths.pop()

    # Every value-bearing group must sit at a constant position and width.
    spans: dict[str, tuple[int, int]] = {}
    for g in matches[0].groupdict():
        if g in IGNORED_GROUPS:
            continue
        seen = set()
        for m in matches:
            if m.group(g) is None:
                seen.add(None)
                break
            seen.add((m.start(g) - m.start(), m.end(g) - m.start(g)))
        if len(seen) != 1 or None in seen:
            return None, f"group {g!r} is not at a fixed position, scalar path"
        spans[g] = seen.pop()

    # Where the timestamp starts. A constant offset is the cheapest case; an
    # anchor byte is the fallback that keeps variable-prefix formats (Apache and
    # IIS access logs) on the vectorised path at all.
    offsets = [m.start() - int(starts[r]) for m, r in zip(matches, rows, strict=True)]
    modal_offset, modal_count = Counter(offsets).most_common(1)[0]
    anchor_byte, anchor_skip, ts_offset = -1, 0, 0
    if modal_count >= OFFSET_AGREEMENT * len(offsets):
        # The mode, not unanimity. A stray leading byte-order mark shifts the
        # first line of a file and nothing else; demoting a 2 GB log to the
        # scalar path over one line would be absurd. Tolerating the outlier is
        # safe because `validate_layout` then compares the fast path's match
        # rate against the oracle's and rejects a layout that actually loses
        # rows -- so on a small file the one bad line still forces scalar, and
        # on a large one it is correctly ignored.
        ts_offset = modal_offset
        where = f"fixed offset {ts_offset}"
        if modal_count != len(offsets):
            where += f" ({len(offsets) - modal_count} outlier lines)"
    elif fmt.anchor_byte:
        byte = ord(fmt.anchor_byte)
        skips = set()
        for m, r in zip(matches, rows, strict=True):
            a = int(starts[r])
            hit = chunk.find(bytes([byte]), a, a + int(lens[r]))
            if hit < 0:
                return None, f"anchor {fmt.anchor_byte!r} missing, scalar path"
            skips.add(m.start() - hit)
        if len(skips) != 1:
            return None, f"anchor-relative offset varies ({sorted(skips)}), scalar path"
        anchor_byte, anchor_skip = byte, skips.pop()
        where = f"anchored to {fmt.anchor_byte!r}+{anchor_skip}"
    else:
        return None, (f"timestamp offset varies ({len(set(offsets))} values, "
                      f"mode covers {modal_count / len(offsets):.0%}), scalar path")

    # Every byte inside the timestamp that no group claims is a separator. It is
    # only checked if it is the *same* byte in every sample -- which makes the
    # optional parts of the patterns (`\\s?`, `:?`) self-resolving: consistent in
    # this file means checkable, inconsistent means the width varied and we
    # already bailed out above.
    covered = np.zeros(width, dtype=bool)
    for pos, w in spans.values():
        covered[pos:pos + w] = True
    seps: list[tuple[int, int]] = []
    for pos in range(width):
        if covered[pos]:
            continue
        bytes_here = {m.string[m.start() + pos] for m in matches}
        if len(bytes_here) == 1:
            seps.append((pos, bytes_here.pop()))

    def span_of(group: str, space_padded: bool = False) -> Span | None:
        if group not in spans:
            return None
        pos, w = spans[group]
        return Span(pos, w, space_padded)

    kwargs: dict[str, object] = {}
    for group, fieldname in _GROUP_TO_FIELD.items():
        padded = group == "d" and fmt.space_padded_day
        s = span_of(group, padded)
        if s is not None:
            kwargs[fieldname] = s

    layout = Layout(
        fmt_name=fmt.name,
        semantics=fmt.semantics,
        ts_width=width,
        ts_offset=ts_offset,
        anchor_byte=anchor_byte,
        anchor_skip=anchor_skip,
        seps=tuple(seps),
        month_name=spans["b"][0] if "b" in spans else None,
        infer_year=fmt.infer_year,
        infer_date=fmt.infer_date,
        **kwargs,  # type: ignore[arg-type]
    )
    return layout, f"vectorised, {where}, {len(seps)} separators checked"


# ---------------------------------------------------------------------------
# step 6b: the oracle cross-check (risk R4)
# ---------------------------------------------------------------------------

def validate_layout(
    chunk: bytes,
    buf: np.ndarray,
    starts: np.ndarray,
    lens: np.ndarray,
    rows: np.ndarray,
    fmt: TimestampFormat,
    pattern: re.Pattern[bytes],
    layout: Layout,
) -> tuple[bool, str]:
    """
    Prove the synthesised layout against the regex path on the sample.

    Three things are checked, and the second is the one that matters:

    * the fast path must not lose meaningfully more rows than the oracle;
    * **where both paths produce a timestamp, the timestamps must be equal** --
      a disagreement here is risk R4 materialising, so the layout is thrown away
      rather than shipped;
    * the fast path must not accept a row the oracle rejects, which would mean
      it is reading something that is not a timestamp.

    Returns whether to trust the layout, and a note.
    """
    sub_starts = starts[rows]
    sub_lens = lens[rows]

    fast = ex.extract(buf, sub_starts, sub_lens, layout)
    oracle = ex.extract_scalar(chunk, sub_starts, sub_lens, fmt, pattern)

    if int(oracle.valid.sum()) < MIN_GROUPS_FOR_VALIDATION:
        return False, "too few oracle matches to validate the layout, scalar path"

    overclaim = int((fast.valid & ~oracle.valid).sum())
    if overclaim:
        return False, f"fast path claimed {overclaim} rows the oracle rejects, scalar path"

    if fast.match_rate < oracle.match_rate - RATE_TOLERANCE:
        return False, (f"fast path matched {fast.match_rate:.0%} vs oracle "
                       f"{oracle.match_rate:.0%}, scalar path")

    both = fast.valid & oracle.valid
    if not both.any():
        return False, "no rows matched by both paths, scalar path"

    if layout.infer_year:
        # ts is not yet meaningful; the components are what the indexer will use.
        assert fast.parts is not None and oracle.parts is not None
        for name in ("month", "day", "sod_us"):
            a = getattr(fast.parts, name)[both]
            b = getattr(oracle.parts, name)[both]
            if not np.array_equal(a, b):
                bad = int((a != b).sum())
                return False, f"fast/oracle disagree on {name} for {bad} rows, scalar path"
    else:
        a, b = fast.ts[both], oracle.ts[both]
        if not np.array_equal(a, b):
            bad = int((a != b).sum())
            first = int(np.flatnonzero(a != b)[0])
            return False, (f"fast/oracle disagree on {bad} timestamps "
                           f"(first: {a[first]} vs {b[first]}), scalar path")

    return True, f"layout validated against the oracle on {int(both.sum())} rows"


# ---------------------------------------------------------------------------
# the whole thing
# ---------------------------------------------------------------------------

def detect(
    chunk: bytes,
    buf: np.ndarray,
    starts: np.ndarray,
    lens: np.ndarray,
    *,
    sample: int = SAMPLE_LINES,
    ruleset: SeverityRuleset = DEFAULT_RULESET,
    catalogue: list[TimestampFormat] | None = None,
) -> Detection:
    """
    Work out how to read a file's timestamps, and how much to trust the answer.

    `chunk` and `buf` are the same bytes -- the first for `re`, the second as
    `uint8` for the kernels. Passing both avoids a copy per candidate format.
    """
    formats = catalogue if catalogue is not None else CATALOGUE
    det = Detection()
    if starts.size == 0:
        det.notes.append("empty source")
        return det

    idx = sample_indices(int(starts.size), sample)
    det.sampled = int(idx.size)

    scored: list[tuple[Candidate, list[re.Match[bytes]], list[int], re.Pattern[bytes]]] = []
    for fmt in formats:
        pattern = re.compile(fmt.pattern.encode("ascii"))
        cand, matches, rows = _scan_candidate(chunk, starts, lens, idx, fmt, pattern)
        if cand.matched:
            scored.append((cand, matches, rows, pattern))

    if not scored:
        det.notes.append(f"no catalogue format matched any of {det.sampled} sampled lines")
        return det

    scored.sort(key=lambda t: t[0].rank, reverse=True)
    det.runners_up = [(c.fmt.name, c.rate) for c, _, _, _ in scored[1:4]]

    cand, matches, rows, pattern = scored[0]
    det.match_rate = cand.rate

    # Step 8 / FR-19: a quarter of the lines is the floor. Below it, the honest
    # answer is "I do not know", which the UI turns into a prompt rather than a
    # silently mis-ordered file.
    if cand.rate < MIN_MATCH_RATE:
        det.notes.append(
            f"best candidate {cand.fmt.name} matched only {cand.rate:.0%} of "
            f"{det.sampled} sampled lines, below the {MIN_MATCH_RATE:.0%} floor"
        )
        return det

    det.fmt = cand.fmt
    det.pattern = pattern

    layout, note = synthesise_layout(chunk, starts, lens, cand.fmt, matches, rows)
    det.notes.append(note)
    if layout is not None:
        trusted, vnote = validate_layout(
            chunk, buf, starts, lens, np.asarray(rows, dtype=np.int64),
            cand.fmt, pattern, layout,
        )
        det.notes.append(vnote)
        if trusted:
            det.layout = layout

    # Step 7: the severity word's position, which decides whether severity
    # classification gets the 320 MB/s path or the 7.4 MB/s one (SPEC 10.3).
    det.severity_offset = detect_severity_offset(chunk, starts, lens, ruleset=ruleset)
    det.notes.append(
        f"severity at fixed offset {det.severity_offset}"
        if det.severity_offset is not None
        else "severity position unpredictable, regex fallback (risk R11)"
    )

    det.confidence = _confidence(det, scored)
    return det


def _confidence(det: Detection, scored: list) -> float:
    """
    A number for FR-19 to show the user. Deliberately simple and explainable.

    Match rate is the bulk of it. A validated fast-path layout adds a little,
    because agreement between two independently written parsers is real evidence.
    A close runner-up subtracts, because that is genuine ambiguity the user may
    want to resolve.

    A runner-up only counts as ambiguity if it is *at least as specific* as the
    winner. Most near-ties are not ambiguous at all: `iso_space_nofrac` matches a
    prefix of every `iso_space_dot` line, so it always ties on rate while being
    strictly the weaker reading. Penalising that would report low confidence on
    the most common log format there is.
    """
    score = det.match_rate * 0.8
    if det.layout is not None:
        score += 0.15
    if det.fmt is not None:
        for cand, *_ in scored[1:]:
            if cand.fmt.specificity < det.fmt.specificity:
                continue
            if det.match_rate - cand.rate < 0.05:
                score -= 0.15
                break
    return max(0.0, min(1.0, score))
