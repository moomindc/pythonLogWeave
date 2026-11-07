"""
Severity classification -- SPEC 10.3, D11.

The fast path and the regex fallback must agree, because which one runs depends
on a detection heuristic the user never sees. If they disagree, the same log
file gets different colours depending on where its level word happens to sit.
"""

from __future__ import annotations

import numpy as np
import pytest

from logweave.index.lines import split_lines
from logweave.index.severity import (
    DEFAULT_RULESET,
    Severity,
    SeverityRuleset,
    classify,
    detect_severity_offset,
    severity_at_offset,
    severity_scan,
)


def prep(text: bytes):
    buf = np.frombuffer(text, dtype=np.uint8)
    starts, lens = split_lines(buf)
    return text, buf, starts, lens


def both_paths(text: bytes, offset: int):
    chunk, buf, starts, lens = prep(text)
    fast = severity_at_offset(buf, starts, lens, offset)
    slow = severity_scan(chunk, starts, int(starts.size))
    return fast, slow


# ---------------------------------------------------------------------------
# the eight classes
# ---------------------------------------------------------------------------

def test_there_are_exactly_eight_classes_fitting_three_bits():
    """
    D11. The count is fixed because `flags` bits 0-2 hold it (SPEC 2.3) and the
    palette has eight entries. Adding a ninth would silently corrupt the flags.
    """
    assert len(Severity) == 8
    assert max(int(s) for s in Severity) == 7
    assert Severity.NONE == 0


def test_classes_are_ordered_so_a_threshold_filter_is_one_comparison():
    assert Severity.CRITICAL > Severity.ERROR > Severity.WARNING > Severity.INFO
    assert Severity.INFO > Severity.DEBUG > Severity.TRACE > Severity.NONE


def test_a_ruleset_rejects_a_class_outside_the_eight():
    with pytest.raises(ValueError, match="0-7"):
        SeverityRuleset({9: ("NOPE",)})


def test_the_default_ruleset_covers_what_syslog_actually_writes():
    """
    D11 ships defaults that are useful out of the box. syslog writes `err`,
    `crit` and `emerg`, not `ERROR` and `CRITICAL`, and a ruleset that misses
    them colours a syslog entirely grey.
    """
    words = {w for ws in DEFAULT_RULESET.words.values() for w in ws}
    assert {"ERR", "CRIT", "EMERG", "ALERT"} <= words


# ---------------------------------------------------------------------------
# word boundaries and case
# ---------------------------------------------------------------------------

def test_a_level_word_must_stand_alone():
    """
    Without a trailing boundary check DEBUGGER reads as DEBUG and
    INFORMATIONAL reads as INFO, which miscolours whole files.
    """
    text = (b"2026-01-01 00:00:00.000000 DEBUGGER attached\n"
            b"2026-01-01 00:00:01.000000 INFORMATIONAL banner\n"
            b"2026-01-01 00:00:02.000000 WARNINGS are off\n"
            b"2026-01-01 00:00:03.000000 WARN real warning\n")
    fast, slow = both_paths(text, 27)
    assert fast.tolist() == [0, 0, 0, int(Severity.WARNING)]
    assert slow.tolist() == fast.tolist()


def test_longer_words_are_preferred_over_their_own_prefixes():
    """
    WARN is a prefix of WARNING. If the regex commits to WARN, the trailing
    boundary then rejects WARNING outright instead of backtracking, so the
    alternatives must be ordered longest-first.
    """
    text = (b"2026-01-01 00:00:00.000000 WARNING something\n"
            b"2026-01-01 00:00:01.000000 WARN something\n")
    fast, slow = both_paths(text, 27)
    assert fast.tolist() == [int(Severity.WARNING)] * 2
    assert slow.tolist() == fast.tolist()


def test_classification_is_case_insensitive_by_default():
    text = (b"2026-01-01 00:00:00.000000 error lowercase\n"
            b"2026-01-01 00:00:01.000000 Warn mixed case\n")
    fast, slow = both_paths(text, 27)
    assert fast.tolist() == [int(Severity.ERROR), int(Severity.WARNING)]
    assert slow.tolist() == fast.tolist()


def test_the_most_severe_word_on_a_line_wins():
    """
    An error message that quotes the word INFO is still an error. Classes are
    visited highest-first so position on the line does not decide.
    """
    text = b"2026-01-01 00:00:00.000000 ERROR could not parse INFO record\n"
    _, slow = both_paths(text, 27)
    assert slow.tolist() == [int(Severity.ERROR)]


def test_a_line_with_no_level_word_is_class_none():
    text = b"2026-01-01 00:00:00.000000 just a message\n"
    fast, slow = both_paths(text, 27)
    assert fast.tolist() == [0]
    assert slow.tolist() == [0]


def test_a_line_shorter_than_the_offset_is_safe():
    """A short line is a legitimate row, not an out-of-bounds read."""
    text = b"short\n2026-01-01 00:00:00.000000 ERROR real\n"
    chunk, buf, starts, lens = prep(text)
    fast = severity_at_offset(buf, starts, lens, 27)
    assert fast.tolist() == [0, int(Severity.ERROR)]


# ---------------------------------------------------------------------------
# offset detection
# ---------------------------------------------------------------------------

def test_a_fixed_level_position_is_found(corpus):
    chunk, buf, starts, lens = prep((corpus / "basic" / "app.log").read_bytes())
    offset = detect_severity_offset(chunk, starts, lens)
    assert offset is not None
    fast = severity_at_offset(buf, starts, lens, offset)
    slow = severity_scan(chunk, starts, int(starts.size))
    assert np.array_equal(fast, slow)


def test_a_variable_level_position_is_refused(corpus):
    """
    SPEC 4.3 step 7. Guessing an offset here would misclassify every line; the
    correct answer is None, which selects the slow-but-right path (risk R11).
    """
    chunk, buf, starts, lens = prep(
        (corpus / "edge" / "variable_sev_pos.log").read_bytes()
    )
    assert detect_severity_offset(chunk, starts, lens) is None


def test_a_coincidence_is_not_promoted_to_a_fixed_offset():
    """
    `min_found` exists so that three agreeing lines in a large sample cannot
    decide the whole file's classification path.
    """
    lines = [b"2026-01-01 00:00:00.000000 plain message\n"] * 200
    lines[5] = b"2026-01-01 00:00:00.000000 ERROR here\n"
    chunk, buf, starts, lens = prep(b"".join(lines))
    assert detect_severity_offset(chunk, starts, lens) is None


def test_classify_dispatches_to_the_right_path(corpus):
    """
    `classify` is the single call site the indexer uses, so both branches must
    produce the same answer for a file where the fast path is available.
    """
    chunk, buf, starts, lens = prep((corpus / "basic" / "app.log").read_bytes())
    offset = detect_severity_offset(chunk, starts, lens)
    assert np.array_equal(
        classify(chunk, buf, starts, lens, offset),
        classify(chunk, buf, starts, lens, None),
    )


def test_a_user_ruleset_extends_the_defaults(corpus):
    """
    D11: the words are fully user-extensible even though the classes are not.
    A house convention like `OHNO` must be classifiable without code changes.
    """
    words = {int(k): v for k, v in DEFAULT_RULESET.words.items()}
    words[int(Severity.CRITICAL)] = (*words[int(Severity.CRITICAL)], "OHNO")
    custom = SeverityRuleset(words)

    text = b"2026-01-01 00:00:00.000000 OHNO everything is on fire\n"
    chunk, buf, starts, lens = prep(text)
    assert severity_at_offset(buf, starts, lens, 27, ruleset=custom).tolist() == [
        int(Severity.CRITICAL)
    ]
    # The shipped ruleset does not know the word, which is the point of the test.
    assert severity_at_offset(buf, starts, lens, 27).tolist() == [0]
