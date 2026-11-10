"""
Timestamp display formats -- FR-35.

Three properties carry nearly all of the risk, and each has a test here that
would fail loudly rather than subtly:

1. **The default rendering has not moved.** `civil.format_wall_us` is what an
   annotated copy cites and what a sidecar header records, and the shipped
   pattern must still produce exactly that string.
2. **Every mode is fixed width.** The view reserves the column once and
   freezes it against horizontal scroll, so a format that rendered narrow on
   some rows would make the message column move while scrolling.
3. **A relative offset is never quietly truncated.** The column is sized from
   the log's span; an offset bigger than that widens the column instead of
   dropping its top group, because a wrong interval in evidence is worse than
   a ragged column.

The pattern fields are cross-checked against `datetime.strftime` rather than
against hand-written expectations, so the subset means what a reader coming
from any other tool expects it to mean.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from logweave.index.civil import (
    US_PER_DAY,
    US_PER_SEC,
    days_from_civil_scalar,
    format_wall_us,
)
from logweave.session import timefmt as tf

BASE = 1_793_123_456_789_012
"""An arbitrary mid-2026 afternoon, microsecond precision, nothing round."""


def wall(y: int, m: int, d: int, h: int = 0, mi: int = 0, s: int = 0,
         us: int = 0) -> int:
    return (days_from_civil_scalar(y, m, d) * US_PER_DAY
            + ((h * 60 + mi) * 60 + s) * US_PER_SEC + us)


# ---------------------------------------------------------------------------
# the absolute modes
# ---------------------------------------------------------------------------


def test_the_default_pattern_is_the_canonical_rendering():
    """
    FR-35 adds formats; it does not move the one everything else cites.

    If this fails, every annotated copy and every sidecar header has changed
    shape at the same time as the view did.
    """
    f = tf.TimeFormatter()
    for i in range(500):
        ts = BASE + i * 7919
        assert f.render(ts) == format_wall_us(ts)


@pytest.mark.parametrize("pattern", [p for p, _ in tf.PRESETS])
def test_every_preset_is_fixed_width(pattern):
    f = tf.TimeFormatter(tf.TimeDisplay(pattern=pattern))
    width = len(f.sample)
    # Across days, months and hours, including the single-digit cases that a
    # format built with str() rather than a width spec would shorten.
    for ts in (wall(2026, 1, 1), wall(2026, 12, 31, 23, 59, 59, 999_999),
               wall(2026, 3, 9, 1, 2, 3, 4), BASE):
        assert len(f.render(ts)) == width, f.render(ts)


def test_the_fields_mean_what_strftime_means():
    pattern = "%a %b %d %Y %j %p %I %y %H:%M:%S.%f"
    f = tf.TimeFormatter(tf.TimeDisplay(pattern=pattern))
    rng = random.Random(7)
    for _ in range(400):
        y, m, d = rng.randint(1990, 2099), rng.randint(1, 12), rng.randint(1, 28)
        us = rng.randrange(US_PER_DAY)
        ts = days_from_civil_scalar(y, m, d) * US_PER_DAY + us
        want = (dt.datetime(y, m, d) + dt.timedelta(microseconds=us)).strftime(pattern)
        assert f.render(ts) == want


def test_milliseconds_truncate_rather_than_round():
    """A displayed timestamp must never read later than the one in the file."""
    f = tf.TimeFormatter(tf.TimeDisplay(pattern="%S.%L"))
    assert f.render(wall(2026, 3, 9, 0, 0, 7, 999_999)) == "07.999"


def test_a_literal_percent_and_a_brace_survive():
    """
    A pattern is rendered through `str.format`, which is an implementation
    detail the reader must not be able to trip over.
    """
    f = tf.TimeFormatter(tf.TimeDisplay(pattern="{%H}%% done"))
    assert f.render(wall(2026, 3, 9, 14)) == "{14}% done"


def test_an_unknown_field_is_refused_by_validation_not_by_rendering():
    """
    `validate_pattern` says no where a human can be told; `render` is on the
    paint path and degrades instead of raising sixty times a second.
    """
    assert "%Q" in tf.validate_pattern("%Y %Q")
    assert tf.validate_pattern("%Y-%m-%d") == ""
    assert tf.validate_pattern("no fields here")
    assert tf.validate_pattern("  ")
    assert tf.validate_pattern("%H:%M:%")      # a lone trailing %
    rendered = tf.TimeFormatter(tf.TimeDisplay(pattern="%H %Q")).render(BASE)
    assert rendered.endswith("%Q")


def test_the_date_cache_does_not_leak_between_days():
    """
    The per-day template is the one piece of state a render keeps, and a stale
    one would show every row of today under yesterday's date.
    """
    f = tf.TimeFormatter(tf.TimeDisplay(pattern="%Y-%m-%d %H:%M"))
    assert f.render(wall(2026, 3, 9, 23, 59)) == "2026-03-09 23:59"
    assert f.render(wall(2026, 3, 10, 0, 0)) == "2026-03-10 00:00"
    assert f.render(wall(2026, 3, 9, 12, 0)) == "2026-03-09 12:00"
    # Past the cache bound, so the clear path runs and still answers correctly.
    for i in range(600):
        f.render(wall(2026, 1, 1) + i * US_PER_DAY)
    assert f.render(wall(2026, 3, 9, 23, 59)) == "2026-03-09 23:59"


# ---------------------------------------------------------------------------
# the relative modes
# ---------------------------------------------------------------------------


def test_delta_is_from_the_row_above():
    d = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.DELTA),
                         span_us=600 * US_PER_SEC)
    assert d.render(BASE + 4321, BASE).strip() == "+00.004321"
    assert d.render(BASE, BASE + 4321).strip() == "-00.004321"
    assert d.render(BASE + 83_456_789, BASE).strip() == "+01:23.456789"


def test_the_first_row_of_a_delta_column_is_blank_not_zero():
    """A reader scanning for stalls must not read the top row as "no gap"."""
    d = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.DELTA),
                         span_us=600 * US_PER_SEC)
    first = d.render(BASE, None)
    assert first.strip() == ""
    assert len(first) == len(d.sample)


def test_elapsed_is_signed_from_the_zero_point():
    e = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.ELAPSED),
                         span_us=600 * US_PER_SEC, zero_us=BASE)
    assert e.render(BASE).strip() == "+00.000000"
    assert e.render(BASE + 5 * US_PER_SEC).strip() == "+05.000000"
    assert e.render(BASE - 5 * US_PER_SEC).strip() == "-05.000000"


def test_the_zero_point_sentinel_resolves_to_the_first_row():
    display = tf.TimeDisplay(mode=tf.TimeMode.ELAPSED)
    assert display.zero_us == tf.ZERO_FIRST_ROW
    f = tf.for_span(display, span_us=600 * US_PER_SEC, first_row_us=BASE)
    assert f.zero_us == BASE
    assert f.render(BASE).strip() == "+00.000000"


@pytest.mark.parametrize("span_us,expected_sample", [
    (5 * US_PER_SEC, "-59.999999"),
    (90 * US_PER_SEC, "-59:59.999999"),
    (2 * 3600 * US_PER_SEC, "-23:59:59.999999"),
    (9 * 86_400 * US_PER_SEC, "-9:23:59:59.999999"),
    (400 * 86_400 * US_PER_SEC, "-999:23:59:59.999999"),
])
def test_the_relative_column_is_sized_from_the_span(span_us, expected_sample):
    """
    A log covering one afternoon must not pay for a day group it cannot use:
    four characters of message on every row.
    """
    d = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.DELTA), span_us=span_us)
    assert d.sample == expected_sample


def test_relative_rows_are_all_the_sample_width():
    """
    Within the span the formatter was built for -- which is every offset a log
    can actually produce, since no interval inside a log exceeds its own span.
    """
    for span in (5 * US_PER_SEC, 90 * US_PER_SEC, 7200 * US_PER_SEC,
                 9 * 86_400 * US_PER_SEC):
        d = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.DELTA), span_us=span)
        for delta in {0, 1, 999_999, min(59_000_000, span),
                      min(3_599_000_000, span), span}:
            for sign in (1, -1):
                got = d.render(BASE + sign * delta, BASE)
                assert len(got) == len(d.sample), (span, delta, got)


def test_leading_groups_are_blanked_so_a_gap_reads_at_a_glance():
    """
    The reason delta mode exists: a stall has to be visible down the column,
    which means a four-millisecond gap must not be padded out to look like
    four hours.
    """
    d = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.DELTA),
                         span_us=2 * 3600 * US_PER_SEC)
    small = d.render(BASE + 4321, BASE)
    large = d.render(BASE + 3_700_000_000, BASE)
    assert small == "      +00.004321"
    assert large == "+01:01:40.000000"
    assert len(small) == len(large)


def test_an_offset_past_the_span_widens_rather_than_truncates():
    """
    83 seconds must never render as `23.456789`. The shape says the column is
    seconds-wide; the value says otherwise, and the value wins.
    """
    d = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.DELTA),
                         span_us=5 * US_PER_SEC)
    assert d.render(BASE + 83_456_789, BASE) == "+01:23.456789"


# ---------------------------------------------------------------------------
# what the rest of the application asks of a display
# ---------------------------------------------------------------------------


def test_a_relative_formatter_still_renders_clock_time_on_request():
    """
    The status bar explains `+1:04.882` in clock time, so every formatter can
    produce the absolute rendering whatever mode it is in.
    """
    d = tf.TimeFormatter(tf.TimeDisplay(mode=tf.TimeMode.DELTA))
    assert d.absolute(BASE) == format_wall_us(BASE)


def test_needs_previous_is_only_true_for_delta():
    """
    The view fetches the row above the viewport when this is set, and only
    then: on a filtered 40M-row merge that is a block replay nobody has asked
    for.
    """
    assert tf.TimeDisplay(mode=tf.TimeMode.DELTA).needs_previous
    assert not tf.TimeDisplay(mode=tf.TimeMode.ELAPSED).needs_previous
    assert not tf.TimeDisplay().needs_previous
    assert not tf.TimeDisplay().relative
