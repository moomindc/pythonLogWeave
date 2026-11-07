"""
Line splitting and entry grouping -- SPEC 3.3, 5.2, FR-6, FR-7.

The lone-CR case here is not hypothetical: it was a real bug in the M0
prototype, caught by the corpus on its first run. The splitter only looked for
LF, so a line ending in a bare CR was glued to its successor and
`mixed_newlines.log` parsed 20 entries where ground truth said 30.
"""

from __future__ import annotations

import numpy as np
import pytest

from logweave.index.lines import (
    detect_lone_cr,
    group_entries,
    last_complete_line_end,
    leading_orphans,
    split_lines,
)


def arr(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.uint8)


def split(data: bytes, lone_cr=None):
    starts, lens = split_lines(arr(data), lone_cr)
    return [data[s : s + n] for s, n in zip(starts.tolist(), lens.tolist(), strict=True)]


# ---------------------------------------------------------------------------
# terminators (FR-6)
# ---------------------------------------------------------------------------

def test_lf_and_crlf_produce_identical_lines():
    """
    FR-6. A log written on Windows and the same log written on Linux must index
    identically, so the CR is excluded from the line, not just from the
    terminator.
    """
    assert split(b"one\ntwo\nthree\n") == [b"one", b"two", b"three"]
    assert split(b"one\r\ntwo\r\nthree\r\n") == [b"one", b"two", b"three"]


def test_a_bare_cr_terminates_a_line_when_asked():
    """The M0 bug: without this, 'one' and 'two' become one line."""
    assert split(b"one\rtwo\rthree\r", lone_cr=True) == [b"one", b"two", b"three"]
    # With the rule off, the same bytes glue into a single line -- the bug
    # itself. Terminated with LF so there is a complete line to compare at all;
    # with no terminator anywhere, SPEC 7.3 correctly yields nothing.
    assert split(b"one\rtwo\rthree\n", lone_cr=False) == [b"one\rtwo\rthree"]
    assert split(b"one\rtwo\rthree\r", lone_cr=False) == []


def test_mixed_terminators_in_one_buffer():
    data = b"lf\ncrlf\r\ncr\rlast\n"
    assert split(data, lone_cr=True) == [b"lf", b"crlf", b"cr", b"last"]


def test_a_lone_cr_terminator_is_not_shortened_by_the_crlf_rule():
    """
    The CR strip is gated on the terminator actually being LF. Without that
    gate, a line ending `x\\r` loses its final character.
    """
    assert split(b"ax\rbx\r", lone_cr=True) == [b"ax", b"bx"]
    assert split(b"a\r\r", lone_cr=True) == [b"a", b""]


def test_detect_lone_cr():
    assert not detect_lone_cr(arr(b"a\nb\n"))
    assert not detect_lone_cr(arr(b"a\r\nb\r\n"))
    assert detect_lone_cr(arr(b"a\rb\n"))
    assert detect_lone_cr(arr(b"a\r")), "a CR at end of buffer is not part of a CRLF"
    assert not detect_lone_cr(arr(b""))


def test_autodetection_picks_the_right_rule():
    assert split(b"one\rtwo\r") == [b"one", b"two"]
    assert split(b"one\r\ntwo\r\n") == [b"one", b"two"]


# ---------------------------------------------------------------------------
# partial lines (SPEC 7.3)
# ---------------------------------------------------------------------------

def test_an_unterminated_trailing_line_is_excluded():
    """
    SPEC 7.3. The writer may be mid-line. Indexing it now would show a torn row
    and duplicate it on the next append.
    """
    assert split(b"one\ntwo\npartial") == [b"one", b"two"]


def test_a_buffer_with_no_terminator_yields_nothing():
    assert split(b"no terminator here") == []
    assert split(b"") == []


def test_an_empty_line_is_a_line():
    assert split(b"\n\n\n") == [b"", b"", b""]


# ---------------------------------------------------------------------------
# chunk alignment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("data", "lone_cr", "expected"),
    [
        (b"one\ntwo\n", False, 8),
        (b"one\ntwo\npart", False, 8),
        (b"one\ntwo", False, 4),
        (b"nothing", False, 0),
        (b"one\rtwo\r", True, 8),
        (b"one\rpart", True, 4),
    ],
)
def test_last_complete_line_end(data, lone_cr, expected):
    """Where the indexer cuts a chunk so no line is split across two."""
    assert last_complete_line_end(arr(data), lone_cr) == expected


# ---------------------------------------------------------------------------
# entries (FR-7)
# ---------------------------------------------------------------------------

def test_untimestamped_lines_join_the_preceding_entry():
    """FR-7: a Java stack trace is one entry, not forty."""
    valid = np.array([True, False, False, True, False, True])
    opens, spans = group_entries(valid)
    assert opens.tolist() == [0, 3, 5]
    assert spans.tolist() == [3, 2, 1]


def test_spans_cover_the_whole_chunk_after_the_first_entry():
    valid = np.array([False, False, True, False])
    opens, spans = group_entries(valid)
    assert opens.tolist() == [2]
    assert spans.tolist() == [2]
    assert leading_orphans(valid) == 2


def test_a_chunk_of_only_continuations_opens_no_entry():
    """These belong to whatever entry the previous chunk left open."""
    valid = np.zeros(5, dtype=bool)
    opens, spans = group_entries(valid)
    assert opens.size == 0 and spans.size == 0
    assert leading_orphans(valid) == 5


def test_entry_line_ordinals_are_the_open_positions():
    """
    `flatnonzero(valid)` *is* each entry's line ordinal, which is why `line_no`
    (D9) costs nothing beyond adding the chunk base.
    """
    valid = np.array([False, True, False, True])
    opens, _ = group_entries(valid)
    assert opens.tolist() == np.flatnonzero(valid).tolist()


def test_group_entries_on_an_empty_chunk():
    opens, spans = group_entries(np.zeros(0, dtype=bool))
    assert opens.size == 0 and spans.size == 0
    assert leading_orphans(np.zeros(0, dtype=bool)) == 0


# ---------------------------------------------------------------------------
# against the corpus
# ---------------------------------------------------------------------------

def test_mixed_newline_corpus_file_splits_to_ground_truth(corpus, manifests):
    """The regression test for the M0 bug, against the file that caught it."""
    from conftest import files_of

    want = files_of(manifests["edge"])["mixed_newlines.log"]
    data = (corpus / "edge" / "mixed_newlines.log").read_bytes()
    starts, lens = split_lines(arr(data))
    assert int(starts.size) == want["line_count"], (
        f"split {starts.size} lines, ground truth says {want['line_count']}"
    )
    # No line may retain a terminator byte.
    for s, n in zip(starts.tolist(), lens.tolist(), strict=True):
        assert b"\n" not in data[s : s + n]
        assert b"\r" not in data[s : s + n]
