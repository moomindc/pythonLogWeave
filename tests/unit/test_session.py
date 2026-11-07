"""
The session layer -- SPEC 1 layer 4.

The load-bearing test here is `test_every_row_attribution_resolves_to_its_own_file`:
D9 promises that a row pasted into a ticket can be traced back, and the only way
to know that holds is to re-read each cited file at the cited line number and
compare. Everything else in this file is about not breaking that promise while
scrolling, hiding sources or applying skew.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import files_of

from logweave.index.civil import US_PER_SEC
from logweave.session import LOW_CONFIDENCE, OpenResult, Session


@pytest.fixture
def basic(corpus: Path) -> Path:
    return corpus / "basic"


@pytest.fixture
def session(basic: Path):
    s = Session()
    s.add_files(sorted(basic.glob("*.log")))
    yield s
    s.close()


def test_opening_the_basic_scenario_reports_every_file(session, basic, manifests):
    expected = files_of(manifests["basic"])
    assert len(session.sources) == len(expected)
    assert not session.is_empty

    by_alias = {s.alias: s for s in session.sources}
    for name, spec in expected.items():
        alias = Path(name).stem
        assert by_alias[alias].index.n == len(spec["entries"])


def test_total_rows_is_the_sum_of_every_source(session):
    assert session.total_rows == sum(s.index.n for s in session.sources)


def test_rows_come_back_in_timestamp_order(session):
    rows = session.rows(0, session.total_rows)
    stamps = [r.ts for r in rows]
    assert stamps == sorted(stamps)


def test_row_indices_are_absolute_not_relative_to_the_request(session):
    """A view painting rows 500-559 needs `row` to mean 500, not 0."""
    rows = session.rows(500, 60)
    assert [r.row for r in rows] == list(range(500, 560))


def test_asking_past_the_end_returns_what_exists_rather_than_raising(session):
    """A view scrolled to the bottom does this on every single frame."""
    total = session.total_rows
    rows = session.rows(total - 5, 60)
    assert len(rows) == 5
    assert session.rows(total, 60) == []
    assert session.rows(0, 0) == []


def test_every_row_attribution_resolves_to_its_own_file(session, basic):
    """
    D9 / FR-46a. The whole point of attribution: `app.log:7` must be line 7 of
    app.log, byte for byte.

    Checked for every row in the merge, not a sample, because an off-by-one that
    only appears after a multi-line entry or a chunk boundary is exactly the kind
    of bug this is here to catch.
    """
    file_lines = {
        s.alias: (basic / f"{s.alias}.log").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        for s in session.sources
    }

    for r in session.rows(0, session.total_rows):
        alias = session.sources[r.source].alias
        lines = file_lines[alias]
        assert 1 <= r.line_no <= len(lines), f"{alias}:{r.line_no} out of range"
        # A multi-line entry's text spans several lines; its first line is the
        # one the attribution names.
        assert r.text.splitlines()[0] == lines[r.line_no - 1]


def test_line_numbers_restart_per_file(session):
    """
    They are positions in the source file, not in the merge (SPEC 2.3).

    So every source must have a row claiming line 1, which a merge-relative
    numbering would make impossible for all but the first.
    """
    firsts = {
        s_i for s_i, _ in enumerate(session.sources)
    }
    seen = {
        r.source for r in session.rows(0, session.total_rows) if r.line_no == 1
    }
    assert seen == firsts


def test_text_has_no_trailing_terminator(session):
    """
    The stored range runs to the start of the next entry, so it carries its own
    newline and any blank line after it. Painting that would draw a phantom row.
    """
    for r in session.rows(0, 200):
        assert not r.text.endswith(("\n", "\r"))


def test_repeating_a_request_is_served_from_cache(session):
    first = session.rows(100, 60)
    again = session.rows(100, 60)
    assert [r.text for r in first] == [r.text for r in again]
    # Same strings, not merely equal ones: the LRU should have returned the
    # identical objects rather than re-decoding the bytes.
    assert all(a.text is b.text for a, b in zip(first, again, strict=True))


def test_the_text_cache_is_bounded(session):
    from logweave.session.session import TEXT_CACHE_ROWS

    session.rows(0, session.total_rows)
    assert len(session._text) <= TEXT_CACHE_ROWS


# ---------------------------------------------------------------------------
# skew and visibility
# ---------------------------------------------------------------------------

def test_skew_shifts_one_source_and_reorders_the_merge(session):
    """D2 / FR-18. Applying skew must not re-index anything."""
    before = [(r.source, r.ts) for r in session.rows(0, 40)]
    entries_before = [s.index.n for s in session.sources]

    session.set_skew(0, 3600 * US_PER_SEC)
    after = [(r.source, r.ts) for r in session.rows(0, 40)]

    assert before != after
    assert [s.index.n for s in session.sources] == entries_before
    assert session.total_rows == sum(entries_before)


def test_skew_of_zero_restores_the_original_order(session):
    original = [(r.source, r.ts) for r in session.rows(0, 80)]
    session.set_skew(1, 5 * US_PER_SEC)
    session.set_skew(1, 0)
    assert [(r.source, r.ts) for r in session.rows(0, 80)] == original


def test_hiding_a_source_removes_exactly_its_rows(session):
    """FR-9. Hiding is not closing: the index stays and the rows come back."""
    total = session.total_rows
    hidden = session.sources[0].index.n

    session.set_enabled(0, False)
    assert session.total_rows == total - hidden
    assert all(r.source != 0 for r in session.rows(0, session.total_rows))
    assert len(session.sources) == 3, "hiding must not close the file"

    session.set_enabled(0, True)
    assert session.total_rows == total


def test_hiding_is_recorded_on_the_meta_so_it_survives_a_rebuild(session, corpus):
    """
    Dropping another file in rebuilds the merge; a hidden source must stay hidden.

    This is the bug the `enabled` flag exists to prevent -- it used to be derived
    from `follow`, which meant every rebuild silently un-hid everything.
    """
    session.set_enabled(1, False)
    reduced = session.total_rows

    extra = sorted((corpus / "formats").glob("*.log"))[:1]
    added = session.add_files(extra)
    assert added.ok

    assert session.sources[1].meta.enabled is False
    assert session.total_rows == reduced + session.sources[-1].index.n


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def test_a_missing_file_fails_without_taking_the_batch_down(basic, tmp_path):
    s = Session()
    result = s.add_files([basic / "app.log", tmp_path / "nope.log"])
    assert len(result.opened) == 1
    assert len(result.failed) == 1
    assert result.ok
    assert not s.is_empty
    s.close()


def test_open_result_summary_mentions_each_category():
    r = OpenResult(
        opened=["a", "b"], failed=[("c", "boom")],
        no_format=["d"], low_confidence=[("e", 0.4)],
    )
    text = r.summary()
    assert "2 opened" in text
    assert "1 failed" in text
    assert "no recognised timestamp" in text
    assert "low confidence" in text


def test_an_empty_result_says_so_rather_than_being_blank():
    assert OpenResult().summary() == "nothing opened"
    assert not OpenResult().ok


def test_describe_reports_a_line_per_source_plus_the_merge(session):
    lines = session.describe()
    assert len(lines) == len(session.sources) + 1
    assert "merged" in lines[-1]
    for line, s in zip(lines, session.sources, strict=False):
        assert s.alias in line
        assert s.index.format_name in line


def test_low_confidence_threshold_is_what_the_ui_reports_against(session):
    assert 0.0 < LOW_CONFIDENCE < 1.0
    for s in session.sources:
        assert s.index.confidence > LOW_CONFIDENCE, (
            f"{s.alias} should detect confidently"
        )


# ---------------------------------------------------------------------------
# empty and closed
# ---------------------------------------------------------------------------

def test_an_empty_session_answers_everything_without_raising():
    """The view is constructed before any file is dropped on it."""
    s = Session()
    assert s.total_rows == 0
    assert s.is_empty
    assert s.rows(0, 60) == []
    assert s.time_span() == (0, 0)
    assert s.time_to_row(0) == 0
    assert s.row_to_time(0) == 0
    assert s.describe() == []
    s.close()


def test_close_releases_everything(session):
    session.close()
    assert session.is_empty
    assert session.sources == []
    assert session.rows(0, 10) == []


def test_time_span_covers_every_source(session):
    lo, hi = session.time_span()
    rows = session.rows(0, session.total_rows)
    assert lo == rows[0].ts
    assert hi == rows[-1].ts


def test_time_to_row_lands_on_the_first_row_at_or_after_the_time(session):
    rows = session.rows(0, session.total_rows)
    target = rows[len(rows) // 2].ts
    row = session.time_to_row(target)
    assert rows[row].ts >= target
    if row:
        assert rows[row - 1].ts < target
