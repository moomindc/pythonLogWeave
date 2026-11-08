"""
The overview strips, as data -- SPEC 8.2, 8.3, 8.4, FR-32, FR-33, M5.

SPEC 15 asked for "a small set of screenshot comparisons" for the strips. These
are better than screenshots and they are here rather than in `tests/ui`,
because `session.overview` renders a strip into an RGBA buffer with NumPy and
the widget's whole job is to blit it. So the pixels can be asserted *exactly*,
headlessly, with no font database and no window -- and a wrong colour or a
missing error mark fails with a pixel coordinate rather than a similarity
score.

The cases worth holding onto:

* an idle gap is **blank** on the timeline and not on the minimap, which is the
  entire argument for D4 shipping both strips
* a single ERROR among thousands of INFO lines is **visible**, which is what
  `MIN_MARK_PX` exists for and the one thing a user scans an overview for
* every entry is counted **exactly once**, which the first version of
  `_aggregate` got wrong by four rows in seven thousand
"""

from __future__ import annotations

import numpy as np
import pytest

from logweave.index.histogram import CLASSES
from logweave.index.severity import Severity
from logweave.session import Session
from logweave.session.overview import (
    MIN_MARK_PX,
    StripTheme,
    overlay_marks,
    render_rgba,
    widths_for,
)

US = 1_000_000
HEIGHT = 40
WIDTH = 64


def write_log(path, rows):
    """`rows` is (second_offset, level, text); the base is a fixed wall clock."""
    with path.open("w", encoding="utf-8") as fh:
        for sec, level, text in rows:
            h, rem = 10 + sec // 3600, sec % 3600
            fh.write(f"2026-10-04 {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                     f".000000 {level} {text}\n")
    return path


@pytest.fixture
def gapped(tmp_path):
    """
    A log with two ten-minute bursts an hour apart and nothing in between.

    The shape the timeline exists for: on a row axis the gap is invisible,
    because there are no rows in it to be absent.
    """
    rows = [(s, "INFO", f"early {s}") for s in range(600)]
    rows += [(3600 + s, "INFO", f"late {s}") for s in range(600)]
    return write_log(tmp_path / "gapped.log", rows)


@pytest.fixture
def needle(tmp_path):
    """Three thousand INFO lines and exactly one ERROR, in the middle."""
    rows = [(s, "ERROR" if s == 1500 else "INFO", f"line {s}")
            for s in range(3000)]
    return write_log(tmp_path / "needle.log", rows)


def session_over(*paths) -> Session:
    s = Session(use_cache=False)
    s.add_files(list(paths))
    return s


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def test_nothing_to_draw_without_sources():
    s = Session()
    assert not s.overview.available
    strip = s.overview.timeline(HEIGHT)
    assert strip.counts.shape == (HEIGHT, CLASSES)
    assert not strip.counts.any()
    assert s.overview.minimap(HEIGHT).counts.shape == (HEIGHT, CLASSES)
    s.close()


def test_every_entry_is_counted_exactly_once(gapped, needle):
    """
    The bug the first `_aggregate` had: the final edge landed *inside* the
    bucket holding the last entry, so that bucket was excluded and the strip
    reported 6,996 of 7,000 rows. Four rows missing in seven thousand looks
    exactly like a correct strip.
    """
    s = session_over(gapped, needle)
    assert s.total_rows == 4200
    for strip in (s.overview.timeline(HEIGHT), s.overview.minimap(HEIGHT)):
        assert int(strip.counts.sum()) == s.total_rows
    s.close()


def test_the_timeline_shows_an_idle_gap_as_blank(gapped):
    """FR-33, and the reason the timeline is not just a second minimap."""
    s = session_over(gapped)
    strip = s.overview.timeline(HEIGHT)
    blank = strip.totals == 0
    assert blank.any(), "an hour of silence must leave blank pixels"
    # The gap is in the middle, not at either end: the first and last pixels
    # hold the two bursts.
    assert not blank[0] and not blank[-1]
    assert blank[HEIGHT // 2]
    s.close()


def test_the_minimap_has_no_blank_pixels_for_the_same_log(gapped):
    """
    SPEC 8.3's axis is rows, so an idle gap cannot show -- every pixel covers
    the same number of rows and every row has an entry in it. This is the
    *complement* of the test above, and together they are D4's whole argument:
    neither strip can answer the other's question.
    """
    s = session_over(gapped)
    strip = s.overview.minimap(HEIGHT)
    assert (strip.totals > 0).all()
    s.close()


def test_the_minimap_is_row_proportional(gapped, needle):
    """
    Every pixel covers the same share of the rows, to within rounding.

    This is what broke when `row_times` interpolated between checkpoints: the
    counts came back as ten blank pixels and a thousand rows piled into the
    last one, because the gap had been smeared over the rows around it.
    """
    s = session_over(gapped, needle)
    strip = s.overview.minimap(HEIGHT)
    per_pixel = s.total_rows / HEIGHT
    assert strip.totals.min() > per_pixel * 0.8
    assert strip.totals.max() < per_pixel * 1.2
    s.close()


def test_row_edges_are_evenly_spaced_and_cover_the_merge(gapped):
    s = session_over(gapped)
    strip = s.overview.minimap(HEIGHT)
    assert strip.rows is not None
    assert strip.rows[0] == 0
    assert strip.rows[-1] == s.total_rows
    steps = np.diff(strip.rows)
    assert steps.max() - steps.min() <= 1
    s.close()


def test_time_edges_span_the_merge(gapped):
    s = session_over(gapped)
    lo, hi = s.time_span()
    strip = s.overview.timeline(HEIGHT)
    assert strip.times[0] == lo
    assert strip.times[-1] == hi
    assert (np.diff(strip.times) > 0).all()
    s.close()


def test_a_disabled_source_leaves_the_overview(gapped, needle):
    """FR-9: switching a source off must take it out of the strips too."""
    s = session_over(gapped, needle)
    before = int(s.overview.timeline(HEIGHT).counts.sum())
    s.set_enabled(1, False)
    after = int(s.overview.timeline(HEIGHT).counts.sum())
    assert after < before
    assert after == s.total_rows
    s.set_enabled(1, True)
    assert int(s.overview.timeline(HEIGHT).counts.sum()) == before
    s.close()


def test_skew_moves_a_source_relative_to_the_others(gapped, needle):
    """
    D2: the skew shifts where a source lands without touching its index.

    Applied to the *query* rather than the data, so the proof is that the
    source's counts move to a different part of the strip while its index is
    byte-for-byte what it was.

    Two sources, and that is not incidental. Skewing the only source in a
    session shifts the merged time span by exactly as much, so the strip comes
    back identical -- which is the correct answer and no evidence at all. A
    skew is only observable against something that was not skewed.
    """
    s = session_over(gapped, needle)
    before = s.sources[1].index.ts.copy()
    plain = s.overview.timeline(HEIGHT).totals.copy()

    s.set_skew(1, 4 * 3600 * US)
    skewed = s.overview.timeline(HEIGHT).totals
    assert int(plain.sum()) == int(skewed.sum()), "no entry may be lost to a skew"
    assert not np.array_equal(plain, skewed)
    assert np.array_equal(s.sources[1].index.ts, before), \
        "a skew must not reach the index (D2)"
    s.close()


def test_the_aggregation_is_memoised_between_repaints(gapped):
    """
    The memo is what keeps a scroll from re-aggregating: what moves when the
    user scrolls is the viewport box, and the histogram underneath has not
    changed. Identity, because the widget uses `is` to decide whether its
    pixel buffer is stale.
    """
    s = session_over(gapped)
    first = s.overview.timeline(HEIGHT)
    assert s.overview.timeline(HEIGHT) is first
    assert s.overview.timeline(HEIGHT + 1) is not first
    s.set_skew(0, US)
    assert s.overview.timeline(HEIGHT) is not first
    s.close()


def test_top_severity_reports_the_worst_in_each_pixel(needle):
    s = session_over(needle)
    strip = s.overview.timeline(HEIGHT)
    top = strip.top_severity()
    assert set(top.tolist()) <= {-1, int(Severity.INFO), int(Severity.ERROR)}
    assert (top == int(Severity.ERROR)).sum() == 1
    s.close()


# ---------------------------------------------------------------------------
# seeking (FR-32, FR-33)
# ---------------------------------------------------------------------------

def test_a_click_maps_to_the_row_the_pixel_was_drawn_from(gapped, needle):
    s = session_over(gapped, needle)
    ov = s.overview
    strip = ov.minimap(HEIGHT)
    for p in (0, 1, HEIGHT // 2, HEIGHT - 1):
        assert ov.row_at_pixel(p, HEIGHT) == min(s.total_rows - 1,
                                                 int(strip.rows[p]))
    s.close()


def test_a_click_outside_the_strip_is_clamped_rather_than_wrapped(gapped):
    s = session_over(gapped)
    ov = s.overview
    assert ov.row_at_pixel(-50, HEIGHT) == 0
    assert ov.row_at_pixel(9999, HEIGHT) == ov.row_at_pixel(HEIGHT - 1, HEIGHT)
    assert ov.time_at_pixel(-5, HEIGHT) == s.time_span()[0]
    s.close()


def test_a_timeline_click_seeks_into_the_right_burst(gapped):
    """FR-33's promise: click a band and land in it, not near it."""
    s = session_over(gapped)
    ov = s.overview
    strip = ov.timeline(HEIGHT)
    late = int(np.flatnonzero(strip.totals > 0)[-1])
    row = s.time_to_row(ov.time_at_pixel(late, HEIGHT))
    assert row >= 600, "the late burst starts at row 600"
    s.close()


def test_pixel_and_row_round_trip_through_the_minimap(gapped, needle):
    s = session_over(gapped, needle)
    ov = s.overview
    for p in range(HEIGHT):
        row = ov.row_at_pixel(p, HEIGHT)
        assert abs(ov.pixel_of_row(row, HEIGHT) - p) <= 1
    s.close()


# ---------------------------------------------------------------------------
# the rendering (SPEC 15's screenshot comparisons, done exactly)
# ---------------------------------------------------------------------------

def test_an_empty_pixel_row_draws_nothing():
    counts = np.zeros((3, CLASSES), dtype=np.int64)
    counts[1, Severity.INFO] = 100
    assert not widths_for(counts, WIDTH)[0].any()
    assert not widths_for(counts, WIDTH)[2].any()


def test_the_busiest_row_fills_the_width():
    counts = np.zeros((2, CLASSES), dtype=np.int64)
    counts[0, Severity.INFO] = 10
    counts[1, Severity.INFO] = 100
    widths = widths_for(counts, WIDTH)
    assert widths[1].sum() == WIDTH
    assert 0 < widths[0].sum() < WIDTH


def test_a_row_with_any_entries_is_never_completely_invisible():
    """
    `BAR_MIN_PX`. A pixel holding one entry beside a pixel holding a million
    would round to zero width, and the strip would show a gap where there is
    data -- which is the one thing a gap must not mean.
    """
    counts = np.zeros((2, CLASSES), dtype=np.int64)
    counts[0, Severity.INFO] = 1
    counts[1, Severity.INFO] = 10_000_000
    widths = widths_for(counts, WIDTH)
    assert widths[0].sum() >= 1


def test_one_error_among_thousands_of_info_lines_is_visible():
    """
    `MIN_MARK_PX`, and the reason it exists (FR-11).

    By proportion the ERROR is 0.01% of the pixel -- a hundredth of a pixel of
    width. Finding it is the single thing a user is scanning the overview for,
    so it is guaranteed `MIN_MARK_PX` and the INFO beside it pays.
    """
    counts = np.zeros((1, CLASSES), dtype=np.int64)
    counts[0, Severity.INFO] = 10_000
    counts[0, Severity.ERROR] = 1
    widths = widths_for(counts, WIDTH)
    assert widths[0, Severity.ERROR] >= MIN_MARK_PX
    assert widths[0].sum() == WIDTH, "the width is reallocated, not added to"


def test_the_minimum_mark_is_funded_from_below_and_not_from_above():
    """
    A CRITICAL must never be narrowed to make room for an INFO. The rule runs
    from the highest class down and takes from the widest class *below* the one
    being widened, so severity order is preserved under pressure.
    """
    counts = np.zeros((1, CLASSES), dtype=np.int64)
    counts[0, Severity.INFO] = 10_000
    counts[0, Severity.WARNING] = 1
    counts[0, Severity.CRITICAL] = 1
    widths = widths_for(counts, WIDTH)[0]
    assert widths[Severity.CRITICAL] >= MIN_MARK_PX
    assert widths[Severity.WARNING] >= MIN_MARK_PX
    assert widths[Severity.INFO] == WIDTH - widths[Severity.CRITICAL] \
        - widths[Severity.WARNING]


def test_a_narrow_strip_cannot_be_made_wider_by_the_minimum_mark():
    """The guarantee is capped by the band, or an 8px strip would overflow."""
    counts = np.ones((1, CLASSES), dtype=np.int64)
    widths = widths_for(counts, 4)
    assert widths[0].sum() <= 4


def test_render_puts_the_palette_colour_where_the_class_is(needle):
    """
    The screenshot comparison SPEC 15 asked for, as an exact pixel assertion.

    A pixel row that is entirely INFO must come out in the palette's INFO
    colour -- which is how the M4 painter defect (the view drawing in the wrong
    font because it was never handed one) would have been caught by a test
    rather than by eye.
    """
    s = session_over(needle)
    strip = s.overview.timeline(HEIGHT)
    buf = render_rgba(strip, WIDTH)
    assert buf.shape == (HEIGHT, WIDTH, 4)
    assert (buf[..., 3] == 255).all(), "the strip is opaque"

    th = StripTheme()
    info = np.array(th.severity[int(Severity.INFO)], dtype=np.uint8)
    busiest = int(np.argmax(strip.totals))
    assert np.array_equal(buf[busiest, 0, 0:3], info)
    s.close()


def test_a_blank_pixel_row_is_pure_background(gapped):
    s = session_over(gapped)
    strip = s.overview.timeline(HEIGHT)
    buf = render_rgba(strip, WIDTH)
    blank = int(np.flatnonzero(strip.totals == 0)[0])
    unique = np.unique(buf[blank, :, 0:3].reshape(-1, 3), axis=0)
    assert unique.shape == (1, 3)
    assert np.array_equal(unique[0], np.array(StripTheme().background, np.uint8))
    s.close()


def test_the_error_mark_reaches_the_pixels(needle):
    """End to end: one ERROR in three thousand lines, visible in the buffer."""
    s = session_over(needle)
    strip = s.overview.timeline(HEIGHT)
    buf = render_rgba(strip, WIDTH)
    error = np.array(StripTheme().severity[int(Severity.ERROR)], dtype=np.uint8)
    hits = (buf[..., 0:3] == error).all(axis=2)
    assert hits.any(), "the single ERROR must survive to the pixels"
    assert hits.sum(axis=1).max() >= MIN_MARK_PX
    s.close()


def test_reverse_draws_the_band_from_the_other_edge(needle):
    s = session_over(needle)
    strip = s.overview.timeline(HEIGHT)
    normal = render_rgba(strip, WIDTH, reverse=False)
    flipped = render_rgba(strip, WIDTH, reverse=True)
    assert np.array_equal(normal, flipped[:, ::-1, :])
    s.close()


def test_rendering_a_zero_width_strip_does_not_raise(needle):
    s = session_over(needle)
    buf = render_rgba(s.overview.timeline(HEIGHT), 0)
    assert buf.shape[0] == HEIGHT
    s.close()


def test_overlay_marks_draw_over_whatever_is_beneath(needle):
    """
    SPEC 8.4. The data behind search hits (FR-44) and bookmarks (FR-37) arrives
    with M7; this is the drawing path, and a hit hidden under a dense band is a
    hit the user cannot find, so it is drawn over.
    """
    s = session_over(needle)
    buf = render_rgba(s.overview.timeline(HEIGHT), WIDTH)
    mark = (255, 0, 255)
    overlay_marks(buf, np.array([0, 5, HEIGHT - 1]), mark, span=3)
    for row in (0, 5, HEIGHT - 1):
        assert tuple(buf[row, 0, 0:3]) == mark
        assert tuple(buf[row, 2, 0:3]) == mark
    s.close()


def test_overlay_marks_clamp_rather_than_wrap(needle):
    s = session_over(needle)
    buf = render_rgba(s.overview.timeline(HEIGHT), WIDTH)
    before = buf.copy()
    overlay_marks(buf, np.array([-10, 10_000]), (255, 0, 255))
    assert not np.array_equal(buf[0], before[0])
    assert not np.array_equal(buf[-1], before[-1])
    assert np.array_equal(buf[HEIGHT // 2], before[HEIGHT // 2])
    s.close()


def test_overlay_marks_on_an_empty_list_changes_nothing(needle):
    s = session_over(needle)
    buf = render_rgba(s.overview.timeline(HEIGHT), WIDTH)
    before = buf.copy()
    overlay_marks(buf, np.empty(0, dtype=np.int64), (255, 0, 255))
    assert np.array_equal(buf, before)
    s.close()
