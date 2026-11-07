"""
Calendar arithmetic and local-zone conversion -- SPEC 2.2, D1.

The engine does calendar maths with Hinnant's algorithm on integer arrays
instead of `datetime`, for speed. These tests check it against `datetime` --
the thing it replaced -- across enough dates that a leap-year or century-rule
mistake cannot hide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from logweave.index.civil import (
    MAX_PLAUSIBLE_YEAR,
    MIN_PLAUSIBLE_YEAR,
    US_PER_DAY,
    civil_from_days,
    days_from_civil,
    days_from_civil_scalar,
    epoch_us_to_wall_us,
    local_offset_us_at,
    plausible_date,
    plausible_time,
    plausible_wall_us,
    utc_to_local_wall_us,
    wall_us_to_datetime,
)

EPOCH = datetime(1970, 1, 1)


def all_dates(start: datetime, days: int):
    return [start + timedelta(days=i) for i in range(days)]


# ---------------------------------------------------------------------------
# days_from_civil against datetime
# ---------------------------------------------------------------------------

def test_days_from_civil_matches_datetime_over_forty_years():
    """
    Every day from 1990 to 2030: ~15,000 dates, which covers every leap year,
    the 2000 century exception, and every month-length rule.
    """
    dates = all_dates(datetime(1990, 1, 1), 40 * 366)
    y = np.array([d.year for d in dates], dtype=np.int64)
    m = np.array([d.month for d in dates], dtype=np.int64)
    dd = np.array([d.day for d in dates], dtype=np.int64)
    expected = np.array([(d - EPOCH).days for d in dates], dtype=np.int64)
    assert np.array_equal(days_from_civil(y, m, dd), expected)


def test_known_anchors():
    assert days_from_civil_scalar(1970, 1, 1) == 0
    assert days_from_civil_scalar(1969, 12, 31) == -1
    assert days_from_civil_scalar(2000, 3, 1) == (datetime(2000, 3, 1) - EPOCH).days
    assert days_from_civil_scalar(2026, 11, 7) == (datetime(2026, 11, 7) - EPOCH).days


def test_leap_day_and_the_century_rule():
    """
    1900 was not a leap year, 2000 was. A table-driven implementation gets this
    wrong; the algorithm has no table.
    """
    assert days_from_civil_scalar(2000, 2, 29) == (datetime(2000, 2, 29) - EPOCH).days
    assert days_from_civil_scalar(2024, 2, 29) == (datetime(2024, 2, 29) - EPOCH).days
    # 1900-02-29 does not exist; the algorithm maps it to 1900-03-01.
    assert days_from_civil_scalar(1900, 2, 29) == days_from_civil_scalar(1900, 3, 1)


def test_scalar_and_vector_agree():
    """
    The scalar twin exists only for the constant-date fast path in
    `combine_civil`, so the two must never diverge.
    """
    dates = all_dates(datetime(2024, 1, 1), 800)
    y = np.array([d.year for d in dates], dtype=np.int64)
    m = np.array([d.month for d in dates], dtype=np.int64)
    dd = np.array([d.day for d in dates], dtype=np.int64)
    vector = days_from_civil(y, m, dd)
    scalar = [days_from_civil_scalar(d.year, d.month, d.day) for d in dates]
    assert vector.tolist() == scalar


def test_civil_from_days_is_the_inverse():
    days = np.arange(
        days_from_civil_scalar(1990, 1, 1), days_from_civil_scalar(2060, 1, 1), 7,
        dtype=np.int64,
    )
    y, m, d = civil_from_days(days)
    assert np.array_equal(days_from_civil(y, m, d), days)


def test_wall_us_to_datetime_round_trips():
    for dt in (datetime(2026, 11, 7, 14, 22, 8, 113456),
               datetime(1990, 1, 1), datetime(2099, 12, 31, 23, 59, 59, 999999)):
        wall = (dt - EPOCH) // timedelta(microseconds=1)
        assert wall_us_to_datetime(wall) == dt


# ---------------------------------------------------------------------------
# plausibility
# ---------------------------------------------------------------------------

def test_plausible_date_rejects_out_of_range_fields():
    y = np.array([2026, 1980, 2200, 2026, 2026], dtype=np.int64)
    m = np.array([1, 1, 1, 13, 1], dtype=np.int64)
    d = np.array([1, 1, 1, 1, 32], dtype=np.int64)
    assert plausible_date(y, m, d).tolist() == [True, False, False, False, False]


def test_plausible_time_allows_a_leap_second():
    """Second 60 is a real value that appears in real logs."""
    h = np.array([0, 23, 24, 0, 0], dtype=np.int64)
    mi = np.array([0, 59, 0, 60, 0], dtype=np.int64)
    s = np.array([60, 59, 0, 0, 61], dtype=np.int64)
    assert plausible_time(h, mi, s).tolist() == [True, True, False, False, False]


def test_plausible_wall_us_brackets_the_window():
    lo = days_from_civil_scalar(MIN_PLAUSIBLE_YEAR, 1, 1) * US_PER_DAY
    hi = days_from_civil_scalar(MAX_PLAUSIBLE_YEAR, 1, 1) * US_PER_DAY
    ts = np.array([lo - 1, lo, hi - 1, hi], dtype=np.int64)
    assert plausible_wall_us(ts).tolist() == [False, True, True, False]


# ---------------------------------------------------------------------------
# local zone (D1)
# ---------------------------------------------------------------------------

def test_local_offset_matches_the_platform():
    instant = (datetime(2026, 7, 1, 12) - EPOCH) // timedelta(microseconds=1)
    secs = instant / 1e6
    expected = round(
        (datetime.fromtimestamp(secs)
         - datetime.fromtimestamp(secs, UTC).replace(tzinfo=None)).total_seconds() * 1e6
    )
    assert local_offset_us_at(instant) == expected


def test_utc_to_local_applies_the_offset():
    instant = (datetime(2026, 7, 1, 12) - EPOCH) // timedelta(microseconds=1)
    got = int(utc_to_local_wall_us(np.array([instant], dtype=np.int64))[0])
    assert got == instant + local_offset_us_at(instant)
    # And it must equal what datetime says the local wall clock read.
    expected = (datetime.fromtimestamp(instant / 1e6) - EPOCH) // timedelta(microseconds=1)
    assert got == expected


def test_utc_to_local_resolves_one_offset_per_distinct_day():
    """
    The optimisation that keeps this off the hot path: a chunk spanning two days
    costs two `datetime` calls, not one per row.
    """
    base = (datetime(2026, 3, 1, 12) - EPOCH) // timedelta(microseconds=1)
    ts = np.repeat([base, base + US_PER_DAY], 5000)
    got = utc_to_local_wall_us(ts)
    assert np.unique(got).size == 2
    assert got[0] == base + local_offset_us_at(base)


def test_utc_to_local_honours_the_valid_mask():
    """
    Invalid rows hold garbage read from offset 0, so sampling days from them
    would resolve zones for dates that are not in the file.
    """
    base = (datetime(2026, 3, 1, 12) - EPOCH) // timedelta(microseconds=1)
    ts = np.array([base, 0, base], dtype=np.int64)
    valid = np.array([True, False, True])
    got = utc_to_local_wall_us(ts, valid)
    assert got[0] == got[2] == base + local_offset_us_at(base)


def test_utc_to_local_handles_empty_and_all_invalid():
    assert utc_to_local_wall_us(np.empty(0, dtype=np.int64)).size == 0
    ts = np.array([1, 2, 3], dtype=np.int64)
    assert np.array_equal(utc_to_local_wall_us(ts, np.zeros(3, dtype=bool)), ts)


def test_epoch_us_to_wall_us_is_the_boundary_conversion():
    """
    The helper that exists because mixing the two shifted a whole file by the
    local UTC offset once already -- invisible most of the time, and wrong at a
    New Year boundary for a yearless format.
    """
    instant = (datetime(2026, 7, 1, 12) - EPOCH) // timedelta(microseconds=1)
    assert epoch_us_to_wall_us(instant) == instant + local_offset_us_at(instant)


@pytest.mark.parametrize("month", [1, 4, 7, 10])
def test_dst_transitions_do_not_break_ordering_away_from_the_hour(month):
    """
    D1 declares ordering undefined *within* a transition hour. Away from one,
    conversion must stay monotonic -- a non-monotonic conversion would reorder
    rows that were logged in order.
    """
    base = (datetime(2026, month, 15, 12) - EPOCH) // timedelta(microseconds=1)
    ts = base + np.arange(0, 40 * US_PER_DAY, US_PER_DAY // 4, dtype=np.int64)
    got = utc_to_local_wall_us(ts)
    assert np.all(np.diff(got) > 0)
