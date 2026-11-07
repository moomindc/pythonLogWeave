"""
Rollover inference for incomplete timestamps -- SPEC 4.5.

Two catalogue formats do not write a complete timestamp. syslog omits the year;
strace omits the date entirely. The missing piece is inferred, and the direction
of inference is the whole point of these tests.

logmerger anchors from the file's creation time and infers **forwards**. That is
wrong for any file spanning New Year: entries written in December get dated with
January's year and land eleven months in the future, scattering them to the far
end of the merged view. `test_december_entries_are_not_dated_with_the_new_year`
is that bug, written as a test that must fail if LogWeave ever adopts the same
approach.

LogWeave anchors at the tail, from mtime, and walks backwards.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import files_of, index_of

from logweave.index.civil import US_PER_DAY, US_PER_SEC, days_from_civil_scalar
from logweave.index.indexer import infer_dates, infer_years


def wall(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return days_from_civil_scalar(y, mo, d) * US_PER_DAY + (h * 3600 + mi * 60 + s) * US_PER_SEC


def sod(h: int, mi: int = 0, s: int = 0) -> int:
    return (h * 3600 + mi * 60 + s) * US_PER_SEC


# ---------------------------------------------------------------------------
# the year, for a yearless format
# ---------------------------------------------------------------------------

def test_december_entries_are_not_dated_with_the_new_year():
    """
    The logmerger bug, stated as a requirement.

    A syslog spanning New Year, last written on 3 January 2026. Inferring
    forwards from the file's start would give every entry 2026, putting the
    December lines eleven months in the future. Anchored at the tail, the
    December lines correctly get 2025.
    """
    month = np.array([12, 12, 12, 1, 1, 1], dtype=np.int64)
    day = np.array([28, 30, 31, 1, 2, 3], dtype=np.int64)
    mtime = wall(2026, 1, 3, 9, 0, 0)

    years, rollovers = infer_years(month, day, mtime)

    assert rollovers == 1
    assert years.tolist() == [2025, 2025, 2025, 2026, 2026, 2026]


def test_a_file_entirely_within_one_year_gets_that_year():
    month = np.array([3, 4, 5], dtype=np.int64)
    day = np.array([1, 15, 30], dtype=np.int64)
    years, rollovers = infer_years(month, day, wall(2026, 5, 30, 12))
    assert rollovers == 0
    assert years.tolist() == [2026, 2026, 2026]


def test_a_tail_later_in_the_year_than_mtime_means_the_previous_year():
    """
    A file last written on 2 January whose final entry is dated 31 December
    cannot be from the current year: the last write is the upper bound.
    """
    month = np.array([12, 12], dtype=np.int64)
    day = np.array([30, 31], dtype=np.int64)
    years, _ = infer_years(month, day, wall(2026, 1, 2, 3))
    assert years.tolist() == [2025, 2025]


def test_two_year_boundaries_walk_back_twice():
    month = np.array([11, 1, 11, 1], dtype=np.int64)
    day = np.array([5, 5, 5, 5], dtype=np.int64)
    years, rollovers = infer_years(month, day, wall(2026, 1, 5, 6))
    assert rollovers == 2
    assert years.tolist() == [2024, 2025, 2025, 2026]


def test_small_backward_jitter_is_not_a_year_rollover():
    """
    The guard that keeps risk R4 shut.

    Two entries logged a little out of order must not read as New Year. Without
    a minimum jump, one transposed pair would shift a year's worth of entries
    and the result would look entirely plausible.
    """
    month = np.array([10, 10, 9, 10, 10], dtype=np.int64)
    day = np.array([4, 5, 30, 6, 7], dtype=np.int64)
    years, rollovers = infer_years(month, day, wall(2026, 10, 7, 12))
    assert rollovers == 0
    assert years.tolist() == [2026] * 5


def test_infer_years_handles_an_empty_file():
    years, rollovers = infer_years(
        np.empty(0, np.int64), np.empty(0, np.int64), wall(2026, 1, 1)
    )
    assert years.size == 0 and rollovers == 0


# ---------------------------------------------------------------------------
# the date, for a dateless format
# ---------------------------------------------------------------------------

def test_midnight_crossing_walks_the_date_back():
    """strace-style times crossing midnight, anchored at mtime."""
    sods = np.array([sod(23, 0), sod(23, 59), sod(0, 10), sod(1, 0)], dtype=np.int64)
    mtime = wall(2026, 6, 2, 1, 0)
    days, rollovers = infer_dates(sods, mtime)

    assert rollovers == 1
    first = days_from_civil_scalar(2026, 6, 1)
    assert days.tolist() == [first, first, first + 1, first + 1]


def test_a_tail_later_in_the_day_than_mtime_means_the_previous_day():
    sods = np.array([sod(20, 0), sod(22, 0)], dtype=np.int64)
    mtime = wall(2026, 6, 2, 3, 0)
    days, _ = infer_dates(sods, mtime)
    assert days.tolist() == [days_from_civil_scalar(2026, 6, 1)] * 2


def test_small_backward_jitter_is_not_a_midnight():
    """A one-second backward step is out-of-order logging, not a new day."""
    sods = np.array([sod(12, 0, 5), sod(12, 0, 4), sod(12, 0, 6)], dtype=np.int64)
    days, rollovers = infer_dates(sods, wall(2026, 6, 2, 12, 1))
    assert rollovers == 0
    assert len(set(days.tolist())) == 1


# ---------------------------------------------------------------------------
# end to end, against corpus ground truth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["newyear_syslog.log", "midnight_strace.log"])
def test_rollover_corpus_files_match_ground_truth(corpus, manifests, name):
    """
    The corpus files that exist solely to hold SPEC 4.5 honest.

    Their mtime is set by the generator to the last entry's timestamp, so the
    anchor is exactly what a real file would present.
    """
    want = files_of(manifests["edge"])[name]
    idx = index_of(corpus / "edge" / name)

    assert idx.n == len(want["entries"])
    expected = np.array([e["wall_us"] for e in want["entries"]], dtype=np.int64)
    got = idx.ts.astype(np.int64)
    assert np.array_equal(got, expected), (
        f"{name}: {int((got != expected).sum())} timestamps wrong; "
        f"first delta {int((got - expected)[np.flatnonzero(got != expected)[0]])} us"
    )
    assert idx.stats.year_rollovers + idx.stats.day_rollovers >= 1, (
        "this file is supposed to contain a rollover"
    )


# ---------------------------------------------------------------------------
# edge-case files whose correct answer is not "match the manifest"
# ---------------------------------------------------------------------------

def test_an_unterminated_last_line_is_not_indexed(corpus, manifests):
    """
    SPEC 7.3, and deliberately one entry short of ground truth.

    A line with no terminator may still be being written. Indexing it now would
    show a torn row and then duplicate it when the writer finishes -- the
    classic naive `tail -f` bug. So the generator's final entry is correctly
    absent, and this test pins that down so nobody "fixes" it later.
    """
    want = files_of(manifests["edge"])["partial_last_line.log"]
    idx = index_of(corpus / "edge" / "partial_last_line.log")
    assert idx.n == len(want["entries"]) - 1
    expected = np.array([e["wall_us"] for e in want["entries"][:-1]], dtype=np.int64)
    assert np.array_equal(idx.ts.astype(np.int64), expected)


def test_a_byte_order_mark_is_not_part_of_the_first_entry(corpus, manifests):
    """
    `LogSource.data_start` in action: the first entry begins after the mark, so
    the first row does not render a stray glyph.
    """
    want = files_of(manifests["edge"])["utf8_bom.log"]
    idx = index_of(corpus / "edge" / "utf8_bom.log")
    assert idx.n == len(want["entries"])
    assert int(idx.off[0]) == want["entries"][0]["byte_off"] == 3
