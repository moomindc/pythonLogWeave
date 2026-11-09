"""
Jumping to the next thing worth reading -- FR-41, FR-42, FR-43.

Every assertion here is of the form "the row it landed on is the nearest row
that matches", checked against a brute-force walk over the merged rows. That is
the only honest test: the implementation deliberately does *not* walk the rows,
and a clever shortcut that is subtly wrong would otherwise pass.
"""

from __future__ import annotations

import pytest

from logweave.index.severity import Severity
from logweave.session import Session, nav

US = 1_000_000


def write_log(path, rows):
    with path.open("w", encoding="utf-8") as fh:
        for sec, level, text in rows:
            h, rem = 10 + sec // 3600, sec % 3600
            fh.write(f"2026-10-04 {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                     f".000000 {level} {text}\n")
    return path


def session_over(*paths) -> Session:
    s = Session(use_cache=False)
    s.add_files(list(paths))
    return s


@pytest.fixture
def two(tmp_path):
    """
    Two files on the same clock, so every row has a colliding neighbour.

    `a` carries the rare CRITICAL; `b` carries most of the warnings. Ties
    across files are what makes the frontier arithmetic matter.
    """
    a = write_log(tmp_path / "a.log", [
        (sec, "CRITICAL" if sec in (17, 93) else "INFO", f"a {sec}")
        for sec in range(100)])
    b = write_log(tmp_path / "b.log", [
        (sec, "WARN" if sec % 11 == 0 else "DEBUG", f"b {sec}")
        for sec in range(100)])
    return a, b


def brute(session, row, *, severities=None, source=None, back=False):
    """The answer by walking every merged row -- what the fast path must match."""
    total = session.total_rows
    span = range(row - 1, -1, -1) if back else range(row + 1, total)
    for candidate in span:
        got = session.rows(candidate, 1)[0]
        if severities is not None and got.severity not in severities:
            continue
        if source is not None and got.source != source:
            continue
        return candidate
    return None


# ---------------------------------------------------------------------------
# FR-42: by severity
# ---------------------------------------------------------------------------

def test_next_by_severity_matches_a_brute_force_walk(two):
    s = session_over(*two)
    want = nav.AT_LEAST[int(Severity.WARNING)]
    for row in (0, 1, 7, 33, 100, s.total_rows - 2, s.total_rows - 1):
        assert nav.next_row(s, row, severities=want) == \
            brute(s, row, severities=want), row
    s.close()


def test_previous_by_severity_matches_a_brute_force_walk(two):
    s = session_over(*two)
    want = nav.AT_LEAST[int(Severity.WARNING)]
    for row in (0, 1, 7, 33, 100, s.total_rows - 1):
        assert nav.next_row(s, row, severities=want, back=True) == \
            brute(s, row, severities=want, back=True), row
    s.close()


def test_next_always_moves_off_the_current_row(two):
    """
    Otherwise holding the key down finds the same entry forever -- and the
    frontier that makes "next" exclusive is the same one that makes
    "previous" exclusive, so both are checked.
    """
    s = session_over(*two)
    want = nav.AT_LEAST[int(Severity.CRITICAL)]
    first = nav.next_row(s, -1, severities=want)
    assert first is not None
    assert nav.next_row(s, first, severities=want) != first
    assert nav.next_row(s, first, severities=want, back=True) != first
    s.close()


def test_running_out_returns_none_rather_than_wrapping(two):
    """
    A find bar wraps because you are hunting a string. "Next error" does not:
    quietly going back to the top is how you read the same error twice and
    conclude there are two.
    """
    s = session_over(*two)
    want = nav.AT_LEAST[int(Severity.CRITICAL)]
    last = nav.next_row(s, s.total_rows, severities=want, back=True)
    assert last is not None
    assert nav.next_row(s, last, severities=want) is None
    assert nav.next_row(s, 0, severities=want, back=True) is None
    s.close()


def test_a_match_on_row_zero_is_findable_from_the_top(tmp_path):
    """
    Searching from before the first row has to be expressible, or the first
    error in a log that failed at startup is unreachable.
    """
    rows = [(sec, "ERROR" if sec == 0 else "INFO", f"line {sec}")
            for sec in range(20)]
    s = session_over(write_log(tmp_path / "startup.log", rows))
    assert nav.next_row(s, -1, severities=nav.AT_LEAST[int(Severity.ERROR)]) == 0
    assert nav.next_row(s, 0, severities=nav.AT_LEAST[int(Severity.ERROR)]) is None
    s.close()


def test_at_least_is_a_closed_upward_set():
    assert nav.AT_LEAST[int(Severity.WARNING)] == frozenset(
        {int(Severity.WARNING), int(Severity.ERROR), int(Severity.CRITICAL)})
    assert nav.AT_LEAST[int(Severity.NONE)] == frozenset(
        int(s) for s in Severity)


# ---------------------------------------------------------------------------
# FR-42: by source
# ---------------------------------------------------------------------------

def test_next_from_a_given_file_matches_a_brute_force_walk(two):
    s = session_over(*two)
    for row in (0, 1, 5, 50, s.total_rows - 1):
        for source in (0, 1):
            assert nav.next_row(s, row, source=source) == \
                brute(s, row, source=source), (row, source)
    s.close()


def test_source_and_severity_compose(two):
    s = session_over(*two)
    want = nav.AT_LEAST[int(Severity.WARNING)]
    got = nav.next_row(s, 0, severities=want, source=1)
    assert got == brute(s, 0, severities=want, source=1)
    assert s.rows(got, 1)[0].source == 1
    s.close()


# ---------------------------------------------------------------------------
# under a filter, and on an empty session
# ---------------------------------------------------------------------------

def test_navigation_respects_a_filter(two):
    """
    An entry the filter hid is not a row. Jumping to it would put the cursor on
    whatever row took its place, which is a different line.
    """
    from logweave.session import filters

    s = session_over(*two)
    s.set_filter(filters.apply(s, filters.Filter(sources=frozenset({0}))))
    want = nav.AT_LEAST[int(Severity.WARNING)]
    got = nav.next_row(s, 0, severities=want)
    assert got == brute(s, 0, severities=want)
    assert s.rows(got, 1)[0].source == 0, "source 1's warnings are filtered out"
    s.close()


def test_navigation_on_an_empty_session_returns_none():
    s = Session(use_cache=False)
    assert nav.next_row(s, 0, severities=nav.AT_LEAST[6]) is None
    assert nav.row_at_time(s, 1) == 0
    s.close()


def test_a_hidden_source_contributes_no_candidates(two):
    s = session_over(*two)
    s.set_enabled(1, False)
    assert nav.next_row(s, 0, source=1) is None
    s.close()


# ---------------------------------------------------------------------------
# FR-41 / FR-43: go to a time
# ---------------------------------------------------------------------------

def test_go_to_timestamp_lands_on_the_first_row_at_or_after_it(two):
    s = session_over(*two)
    rows = s.rows(0, s.total_rows)
    target = rows[40].ts
    got = nav.row_at_time(s, target)
    assert s.rows(got, 1)[0].ts == target
    assert got == 0 or s.rows(got - 1, 1)[0].ts < target
    # A time before everything and after everything, which a dialog can be
    # given and must not raise over.
    assert nav.row_at_time(s, 0) == 0
    assert nav.row_at_time(s, rows[-1].ts + US) == s.total_rows
    s.close()


# ---------------------------------------------------------------------------
# the window growth, which is the only thing about the implementation that is
# not obviously correct
# ---------------------------------------------------------------------------

def test_a_match_far_beyond_the_first_window_is_still_found(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(nav, "WINDOW", 4)
    rows = [(sec, "ERROR" if sec == 900 else "INFO", f"line {sec}")
            for sec in range(1000)]
    s = session_over(write_log(tmp_path / "far.log", rows))
    assert nav.next_row(s, 0, severities=nav.AT_LEAST[int(Severity.ERROR)]) == 900
    assert nav.next_row(s, 999, severities=nav.AT_LEAST[int(Severity.ERROR)],
                        back=True) == 900
    s.close()
