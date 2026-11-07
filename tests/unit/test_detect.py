"""
Format detection and the fast-path/oracle agreement -- SPEC 4.2, 4.3.

The most valuable test here is `test_fast_path_agrees_with_the_oracle`: two
independently written parsers, one vectorised and one per-line regex, over every
corpus file. Agreement between them is the evidence that the clever one is
right, and disagreement is risk R4 caught before a user sees it.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import files_of, index_of

from logweave.index import extract as ex
from logweave.index.detect import (
    LEADING_SLACK,
    MIN_MATCH_RATE,
    Detection,
    detect,
    sample_indices,
)
from logweave.index.layouts import BY_NAME, CATALOGUE, Semantics
from logweave.index.lines import split_lines


def read(path):
    chunk = path.read_bytes()
    buf = np.frombuffer(chunk, dtype=np.uint8)
    starts, lens = split_lines(buf)
    return chunk, buf, starts, lens


def detect_file(path) -> tuple[Detection, tuple]:
    chunk, buf, starts, lens = read(path)
    return detect(chunk, buf, starts, lens), (chunk, buf, starts, lens)


# ---------------------------------------------------------------------------
# the catalogue itself
# ---------------------------------------------------------------------------

def test_specificity_breaks_prefix_ties_in_the_right_direction():
    """
    SPEC 4.2 point 2. A 10-digit epoch pattern matches a prefix of a 13-digit
    one, so whichever consumes more digits must rank higher. Resolving this by
    declaration order -- as logmerger does implicitly -- is a latent bug.
    """
    assert BY_NAME["epoch_float"].specificity > BY_NAME["epoch_ms"].specificity
    assert BY_NAME["epoch_ms"].specificity > BY_NAME["epoch_s"].specificity
    # A zone-bearing or fraction-bearing variant outranks its plainer twin.
    assert BY_NAME["iso_space_dot_z"].specificity > BY_NAME["iso_space_dot"].specificity
    assert BY_NAME["iso_space_dot"].specificity > BY_NAME["iso_space_nofrac"].specificity


def test_every_catalogue_pattern_compiles_and_names_its_groups():
    import re

    for fmt in CATALOGUE:
        rx = re.compile(fmt.pattern.encode("ascii"))
        names = set(rx.groupindex)
        if fmt.semantics in (Semantics.EPOCH_S, Semantics.EPOCH_MS):
            assert "epoch" in names, fmt.name
        else:
            assert {"H", "M", "S"} <= names, f"{fmt.name} must capture a time"
            has_month = "m" in names or "b" in names
            if not fmt.infer_date:
                assert has_month and "d" in names, f"{fmt.name} must capture a date"
            if not (fmt.infer_year or fmt.infer_date):
                assert "Y" in names, f"{fmt.name} must capture a year"


def test_catalogue_names_are_unique():
    assert len(BY_NAME) == len(CATALOGUE)


# ---------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------

def test_sampling_spreads_across_the_file_rather_than_taking_the_head():
    """
    SPEC 4.2 point 1. A log's first lines are often a banner or a header, so
    voting on them alone is how a confident wrong answer happens.
    """
    idx = sample_indices(100_000, limit=300)
    assert idx.size <= 300
    assert idx[0] == 0
    assert idx[-1] == 99_999
    assert idx.max() > 50_000, "the sample must reach the far end of the file"
    assert np.all(np.diff(idx) > 0)


def test_sampling_a_short_file_takes_every_line():
    assert sample_indices(7, limit=300).tolist() == list(range(7))


# ---------------------------------------------------------------------------
# detection over the corpus
# ---------------------------------------------------------------------------

def test_every_format_file_is_detected_on_the_vectorised_path(corpus, manifests):
    """
    All 15 formats must reach the fast path. Falling back to the scalar path is
    correct but ~20x slower, so a regression here is a performance regression
    that no timing test would attribute correctly.
    """
    scalar = []
    for name in sorted(files_of(manifests["formats"])):
        det, _ = detect_file(corpus / "formats" / name)
        assert det.ok, f"{name}: nothing detected"
        if not det.fast_path:
            scalar.append((name, det.notes))
    assert not scalar, f"these fell back to the scalar path: {scalar}"


def test_fast_path_agrees_with_the_oracle(corpus, manifests):
    """
    The R4 test. Every corpus file, both parsers, every row they both claim.

    `detect` already performs this check on its sample and refuses a layout that
    disagrees; this runs it over whole files, which is a strictly stronger
    statement than the sample can make.
    """
    disagreements = []
    for scenario in ("formats", "basic", "edge"):
        for name in sorted(files_of(manifests[scenario])):
            path = corpus / scenario / name
            det, (chunk, buf, starts, lens) = detect_file(path)
            if not det.ok or det.layout is None:
                continue

            fast = ex.extract(buf, starts, lens, det.layout)
            oracle = ex.extract_scalar(chunk, starts, lens, det.fmt, det.pattern)
            both = fast.valid & oracle.valid
            if not both.any():
                disagreements.append(f"{scenario}/{name}: no commonly matched rows")
                continue

            if det.layout.infer_year:
                for field in ("month", "day", "sod_us"):
                    a = getattr(fast.parts, field)[both]
                    b = getattr(oracle.parts, field)[both]
                    if not np.array_equal(a, b):
                        disagreements.append(f"{scenario}/{name}: {field} differs")
            elif det.layout.infer_date:
                a, b = fast.parts.sod_us[both], oracle.parts.sod_us[both]
                if not np.array_equal(a, b):
                    disagreements.append(f"{scenario}/{name}: sod_us differs")
            else:
                a, b = fast.ts[both], oracle.ts[both]
                if not np.array_equal(a, b):
                    i = int(np.flatnonzero(a != b)[0])
                    disagreements.append(
                        f"{scenario}/{name}: {int((a != b).sum())} timestamps differ, "
                        f"first {a[i]} vs {b[i]}"
                    )
    assert not disagreements, "\n".join(disagreements)


def test_a_file_with_no_timestamps_is_reported_not_guessed(corpus):
    """
    FR-19. The honest answer is "I do not know", which the UI turns into a
    prompt. Guessing here is how a whole file silently mis-orders.
    """
    det, _ = detect_file(corpus / "edge" / "no_timestamps.log")
    assert not det.ok
    assert det.confidence == 0.0
    assert det.notes and "no catalogue format matched" in det.notes[0]


def test_an_empty_file_is_reported_not_crashed(corpus):
    det, _ = detect_file(corpus / "edge" / "empty.log")
    assert not det.ok
    assert "empty source" in det.notes[0]


def test_confidence_tracks_match_rate_for_a_clean_file(corpus):
    """
    A near-tie only counts as ambiguity when the runner-up is at least as
    specific. `iso_space_nofrac` matches a prefix of every `iso_space_dot` line,
    so it always ties on rate while being the strictly weaker reading --
    penalising that would report low confidence on the commonest log format
    there is.
    """
    det, _ = detect_file(corpus / "edge" / "mixed_newlines.log")
    assert det.ok and det.fast_path
    assert det.match_rate == 1.0
    assert det.confidence >= 0.9


def test_a_leading_banner_does_not_prevent_detection(corpus):
    """The head of a file is often not representative; the sample must outvote it."""
    det, _ = detect_file(corpus / "edge" / "leading_banner.log")
    assert det.ok and det.fast_path
    assert det.match_rate >= MIN_MATCH_RATE


def test_variable_level_position_is_detected_as_unpredictable(corpus):
    """
    SPEC 4.3 step 7. This file puts the level after a variable-length logger
    name, so the fast severity path cannot apply and the regex fallback must be
    chosen -- knowingly, which is risk R11 rather than a wrong answer.
    """
    det, _ = detect_file(corpus / "edge" / "variable_sev_pos.log")
    assert det.ok
    assert det.severity_offset is None
    assert any("unpredictable" in n for n in det.notes)


def test_varying_json_key_order_falls_back_rather_than_guessing(corpus):
    """
    A JSONL file whose keys move cannot have a fixed timestamp offset. The
    scalar path is the correct answer, and it must still produce timestamps.

    Which of the two safety nets catches it is deliberately not asserted. If
    fewer than 90% of lines agree on an offset, synthesis refuses outright; if
    more than that agree, a layout is built and then thrown away because it
    loses rows the regex oracle finds. Both are correct, and which one fires
    depends on how the generator happened to shuffle the keys -- so asserting
    the mechanism would make this test fragile for no gain. What must hold is
    that no fixed layout survives and timestamps are still produced.
    """
    det, (chunk, buf, starts, lens) = detect_file(corpus / "edge" / "jsonl_varying.jsonl")
    assert det.ok
    assert det.layout is None, "a varying offset must not yield a fixed layout"
    assert any("scalar path" in n for n in det.notes), det.notes
    res = ex.extract_scalar(chunk, starts, lens, det.fmt, det.pattern)
    assert res.valid.sum() > 0


# ---------------------------------------------------------------------------
# guards against false positives
# ---------------------------------------------------------------------------

def test_an_implausible_epoch_is_rejected(tmp_path):
    """
    SPEC 4.3 step 4. An epoch pattern is just ten digits, so without a
    plausibility window a build number or a byte count becomes a timestamp.

    9000000000 would be the year 2255, outside the window, so these are ids.
    """
    p = tmp_path / "ids.log"
    p.write_bytes(b"".join(b"request 90000000%02d completed\n" % i for i in range(60)))
    det, _ = detect_file(p)
    assert not det.ok, f"detected {det.fmt.name if det.fmt else None} in a file of ids"


def test_a_ten_digit_id_inside_the_epoch_window_is_genuinely_ambiguous(tmp_path):
    """
    A known and irreducible limitation, recorded rather than papered over.

    An opaque ten-digit identifier that happens to fall between 1990 and 2100
    read as an epoch is *indistinguishable* from a timestamp: both are ten
    digits in the same range. No amount of pattern work fixes that, so detection
    takes it as an epoch.

    The mitigations are the two this test asserts: any format carrying an actual
    date outranks an epoch, so a line containing both is read correctly; and
    FR-19 reports what was detected so the user can override. Asserting the
    wrong-but-unavoidable answer here is deliberate -- if someone later makes
    this case return "no format", they will have broken real epoch logs.
    """
    p = tmp_path / "ids.log"
    p.write_bytes(b"".join(b"request 1000000%03d done\n" % i for i in range(60)))
    det, _ = detect_file(p)
    assert det.ok and det.fmt.name == "epoch_s"

    # But a real date on the line wins, because it is far more specific.
    q = tmp_path / "both.log"
    q.write_bytes(b"".join(
        b"2026-03-04 10:%02d:00.000000 INFO request 1000000%03d done\n" % (i % 60, i)
        for i in range(60)
    ))
    det2, _ = detect_file(q)
    assert det2.ok and det2.fmt.name.startswith("iso_"), det2.fmt.name


def test_a_plausible_epoch_is_accepted(tmp_path):
    """The counterpart: a real epoch in the same shape of line must be found."""
    p = tmp_path / "ok.log"
    p.write_bytes(b"".join(b"%d INFO tick %d\n" % (1791000000 + i, i) for i in range(60)))
    det, _ = detect_file(p)
    assert det.ok and det.fmt.name == "epoch_s"
    assert det.fast_path


def test_a_timestamp_far_into_the_line_is_not_accepted_for_an_anchored_format(tmp_path):
    """
    `LEADING_SLACK` exists because an unanchored pattern will otherwise match a
    number deep inside free text. A short prefix is fine; 200 bytes is not.
    """
    pad = b"x" * (LEADING_SLACK + 200)
    p = tmp_path / "deep.log"
    p.write_bytes(b"".join(pad + b" 1791000%03d\n" % i for i in range(60)))
    det, _ = detect_file(p)
    assert not det.ok or det.fmt.name != "epoch_s"


def test_detection_respects_a_restricted_catalogue(corpus):
    """
    Detection takes the catalogue as an argument so a user override (FR-19) is
    the same code path as the default, not a special case bolted on later.
    """
    path = corpus / "formats" / "ymd_hms_dot_f.log"
    chunk, buf, starts, lens = read(path)
    only = [BY_NAME["iso_space_nofrac"]]
    det = detect(chunk, buf, starts, lens, catalogue=only)
    assert det.ok and det.fmt.name == "iso_space_nofrac"


@pytest.mark.parametrize("name", ["cp1252.log", "invalid_utf8.log"])
def test_undecodable_bytes_do_not_stop_detection(corpus, manifests, name):
    """
    FR-5 and the 'try as best you can' decision. The indexer works on bytes, so
    a line whose message is not valid UTF-8 must still have its timestamp read.

    Asserted against ground truth rather than against a match-rate threshold:
    the rate depends on how many continuation lines the generator happened to
    emit, so a number here would be arbitrary. What matters is that every
    timestamp is right.
    """
    det, _ = detect_file(corpus / "edge" / name)
    assert det.ok and det.fast_path
    assert det.match_rate > MIN_MATCH_RATE

    idx = index_of(corpus / "edge" / name)
    expected = np.array(
        [e["wall_us"] for e in files_of(manifests["edge"])[name]["entries"]],
        dtype=np.int64,
    )
    assert np.array_equal(idx.ts.astype(np.int64), expected)
