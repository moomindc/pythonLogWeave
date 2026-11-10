"""
Bookmarks -- FR-37, and what a profile saves of them (FR-49).

The one property worth most of this file: **a bookmark does not drift.** It is
stored as the entry the reader marked, so hiding a source, applying a filter or
appending to a followed file leaves it on the same line of the same file --
whatever row number that line happens to have at the time.

The second: navigation only ever lands somewhere the reader can see. A
bookmark in a hidden or filtered-out source keeps its mark and loses its row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from logweave.index.severity import Severity
from logweave.session import Session
from logweave.session.bookmarks import Bookmarks
from logweave.session.filters import Filter, apply


@pytest.fixture
def session(corpus: Path):
    s = Session()
    s.add_files(sorted((corpus / "basic").glob("*.log")))
    yield s
    s.close()


def mark_row(marks: Bookmarks, session: Session, row: int) -> int:
    """Bookmark whatever row `row` is, and return the row back."""
    r = session.rows(row, 1)[0]
    marks.add(r.source, r.entry)
    return row


# ---------------------------------------------------------------------------
# the set
# ---------------------------------------------------------------------------

def test_toggling_marks_and_unmarks(session):
    marks = Bookmarks()
    r = session.rows(3, 1)[0]
    assert marks.toggle(r.source, r.entry) is True
    assert (r.source, r.entry) in marks
    assert len(marks) == 1
    assert marks.toggle(r.source, r.entry) is False
    assert len(marks) == 0


def test_a_note_rides_along(session):
    marks = Bookmarks()
    r = session.rows(3, 1)[0]
    marks.add(r.source, r.entry, "pool exhausted")
    assert [m.note for m in marks] == ["pool exhausted"]


# ---------------------------------------------------------------------------
# rows, which are derived rather than stored
# ---------------------------------------------------------------------------

def test_rows_come_back_in_merged_order(session):
    marks = Bookmarks()
    for row in (9, 2, 5):
        mark_row(marks, session, row)
    assert list(marks.rows(session)) == [2, 5, 9]


def test_a_bookmark_stays_on_its_line_when_a_source_is_hidden(session):
    """
    The whole reason a bookmark is `(source, entry)`: hiding another file
    renumbers every row after it.
    """
    marks = Bookmarks()
    row = 12
    marked = session.rows(row, 1)[0]
    marks.add(marked.source, marked.entry)

    other = next(i for i in range(len(session.sources)) if i != marked.source)
    session.set_enabled(other, False)

    rows = marks.rows(session)
    assert len(rows) == 1
    moved = session.rows(int(rows[0]), 1)[0]
    assert (moved.source, moved.entry) == (marked.source, marked.entry)
    assert moved.line_no == marked.line_no


def test_a_bookmark_in_a_hidden_source_keeps_its_mark_and_loses_its_row(session):
    marks = Bookmarks()
    r = session.rows(0, 1)[0]
    marks.add(r.source, r.entry)
    session.set_enabled(r.source, False)

    assert len(marks) == 1, "still remembered"
    assert marks.rows(session).size == 0, "but not somewhere the view can go"

    session.set_enabled(r.source, True)
    assert marks.rows(session).size == 1, "and it comes back"


def test_a_filter_hides_a_bookmark_without_losing_it(session):
    marks = Bookmarks()
    for row in range(0, 20, 4):
        mark_row(marks, session, row)
    before = len(marks.rows(session))

    filt = Filter(severities=frozenset({int(Severity.ERROR)}))
    session.set_filter(apply(session, filt))
    after = len(marks.rows(session))
    session.set_filter(None)

    assert after <= before
    assert len(marks) == 5, "none of them were forgotten"
    assert len(marks.rows(session)) == before


# ---------------------------------------------------------------------------
# navigation (FR-37)
# ---------------------------------------------------------------------------

def test_next_and_previous_step_strictly_and_do_not_wrap(session):
    marks = Bookmarks()
    for row in (3, 8, 15):
        mark_row(marks, session, row)

    assert marks.next_row(session, 0) == 3
    assert marks.next_row(session, 3) == 8, "strictly after, so a key repeats"
    assert marks.next_row(session, 15) == -1, "no wrap: say you are at the end"
    assert marks.next_row(session, 15, back=True) == 8
    assert marks.next_row(session, 3, back=True) == -1


def test_navigation_with_nothing_marked_says_so(session):
    assert Bookmarks().next_row(session, 0) == -1


# ---------------------------------------------------------------------------
# the strips (FR-37 on both overviews)
# ---------------------------------------------------------------------------

def test_the_layer_is_sorted_merged_time_with_skew_applied(session):
    marks = Bookmarks()
    r = session.rows(4, 1)[0]
    marks.add(r.source, r.entry)

    plain = marks.layer(session, "#ffffff")
    assert plain is not None and plain.hits == 1

    session.set_skew(r.source, 5_000_000)
    skewed = marks.layer(session, "#ffffff")
    assert int(skewed.times[0]) == int(plain.times[0]) + 5_000_000


def test_no_bookmarks_means_no_layer(session):
    assert Bookmarks().layer(session, "#ffffff") is None


# ---------------------------------------------------------------------------
# saving and restoring (FR-49)
# ---------------------------------------------------------------------------

def test_a_saved_bookmark_names_the_file_and_the_time(session):
    marks = Bookmarks()
    r = session.rows(6, 1)[0]
    marks.add(r.source, r.entry, "here")
    (alias, wall_us, line_no, note), = marks.to_pairs(session)

    assert alias == session.sources[r.source].alias
    assert wall_us == int(session.sources[r.source].index.ts[r.entry])
    assert line_no == r.line_no
    assert note == "here"


def test_a_saved_bookmark_does_not_move_when_the_skew_is_tuned(session):
    """
    A skew is a correction the reader adjusts while reading (FR-18). Saving the
    merged time would mean every bookmark moved a line the moment they did.
    """
    marks = Bookmarks()
    r = session.rows(6, 1)[0]
    marks.add(r.source, r.entry)
    before = marks.to_pairs(session)
    session.set_skew(r.source, -3_000_000)
    assert marks.to_pairs(session) == before


def test_restoring_lands_on_the_same_entry(session):
    """
    Including where several entries share a timestamp, which is why a saved
    bookmark carries the line number as well (SPEC 12.2 as corrected).
    """
    marks = Bookmarks()
    for row in (1, 7, 11):
        mark_row(marks, session, row)
    saved = marks.to_pairs(session)

    restored = Bookmarks()
    assert restored.restore(session, saved) == []
    assert list(restored.rows(session)) == list(marks.rows(session))


def test_restoring_a_bookmark_on_a_file_that_is_not_open_says_so(session):
    restored = Bookmarks()
    notes = restored.restore(session,
                             [("not-open.log", 1_793_000_000_000_000, 1, "")])
    assert len(restored) == 0
    assert notes and "not open" in notes[0]


def test_restoring_past_the_end_of_a_file_says_so(session):
    alias = session.sources[0].alias
    restored = Bookmarks()
    notes = restored.restore(session, [(alias, 4_000_000_000_000_000, 1, "")])
    assert len(restored) == 0
    assert notes and "at or after" in notes[0]


def test_a_bookmark_whose_entry_is_gone_lands_on_the_next_one(session):
    """
    A rotated file may no longer contain the line that was marked. Landing on
    the next entry keeps the reader near what they meant; vanishing silently
    would not.
    """
    ss = session.sources[0]
    between = int(ss.index.ts[3]) - 1
    restored = Bookmarks()
    notes = restored.restore(session, [(ss.alias, between, 0, "")])
    assert len(restored) == 1
    assert [m.entry for m in restored] == [3]
    assert notes and "next entry" in notes[0]


def test_a_bookmark_on_a_duplicated_timestamp_restores_to_its_own_line(tmp_path):
    """
    The defect this found: a service writing four lines in the same
    millisecond gives them one timestamp, so restoring by time alone landed on
    the first of the run -- a bookmark that moved, which is the failure
    `(source, entry)` exists to prevent, reintroduced by the file format.
    """
    path = tmp_path / "burst.log"
    path.write_text("".join(
        f"2026-10-04 10:00:00.000000 INFO line {i}\n" for i in range(6)
    ), encoding="utf-8")
    session = Session(use_cache=False)
    session.add_files([path])
    try:
        marks = Bookmarks()
        marks.add(0, 4, "the one I meant")
        saved = marks.to_pairs(session)
        assert saved[0][1] == int(session.sources[0].index.ts[0]), "all one time"

        restored = Bookmarks()
        assert restored.restore(session, saved) == []
        assert [m.entry for m in restored] == [4]
    finally:
        session.close()
