"""
The candidate kernels, checked against the corpus ground truth.

These are the tests that matter most in M0: they decide whether the SPEC 4.4
vectorised extraction and the SPEC 6.2 checkpointed merge are actually correct,
not merely fast. M1's real implementation must keep passing them.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
from _prototypes import (
    MergePrototype,
    build_cumulative,
    days_from_civil,
    days_from_civil_scalar,
    detect_lone_cr,
    detect_severity_offset,
    entries_from,
    extract_fixed_iso,
    extract_scalar_regex,
    extract_scalar_strptime,
    histogram,
    severity_at_offset,
    severity_scan,
    split_lines,
    strip_aggregate,
)
from conftest import files_of

ISO_FILES = [
    ("formats", "ymd_hms_dot_f.log"),
    ("basic", "app.log"),
    ("edge", "leading_banner.log"),
    ("edge", "ooo_unique.log"),
    ("edge", "ooo_duplicate.log"),
    ("edge", "mixed_newlines.log"),
    ("edge", "huge_line.log"),
]


# ---------------------------------------------------------------------------
# days_from_civil (SPEC 2.2)
# ---------------------------------------------------------------------------

def test_days_from_civil_matches_date_toordinal():
    """Cross-check Hinnant's algorithm against the stdlib over ~55 years."""
    base = dt.date(1970, 1, 1).toordinal()
    days = [dt.date(1970, 1, 1) + dt.timedelta(days=i) for i in range(0, 20000, 7)]
    y = np.array([d.year for d in days])
    m = np.array([d.month for d in days])
    d_ = np.array([d.day for d in days])
    expected = np.array([d.toordinal() - base for d in days], dtype=np.int64)
    np.testing.assert_array_equal(days_from_civil(y, m, d_), expected)


def test_days_from_civil_epoch_and_leap_years():
    assert int(days_from_civil(np.array([1970]), np.array([1]), np.array([1]))[0]) == 0
    for y, m, d in [(2000, 2, 29), (2024, 2, 29), (1900, 3, 1), (2026, 12, 31)]:
        expected = dt.date(y, m, d).toordinal() - dt.date(1970, 1, 1).toordinal()
        assert int(days_from_civil(np.array([y]), np.array([m]), np.array([d]))[0]) == expected
        assert days_from_civil_scalar(y, m, d) == expected


# ---------------------------------------------------------------------------
# split_lines (SPEC 4.4 / 7.3)
# ---------------------------------------------------------------------------

def _split(raw: bytes):
    return split_lines(np.frombuffer(raw, np.uint8))


def test_split_lines_lf():
    starts, lens = _split(b"abc\ndefg\nhi\n")
    np.testing.assert_array_equal(starts, [0, 4, 9])
    np.testing.assert_array_equal(lens, [3, 4, 2])


def test_split_lines_crlf_excludes_cr():
    starts, lens = _split(b"abc\r\ndefg\r\n")
    np.testing.assert_array_equal(starts, [0, 5])
    np.testing.assert_array_equal(lens, [3, 4])


def test_split_lines_drops_unterminated_tail():
    """SPEC 7.3: a half-written line must not become an entry."""
    starts, lens = _split(b"abc\ndef")
    np.testing.assert_array_equal(starts, [0])
    np.testing.assert_array_equal(lens, [3])


def test_split_lines_empty_and_blank():
    starts, lens = _split(b"")
    assert starts.size == 0 and lens.size == 0
    starts, lens = _split(b"\n\n")
    np.testing.assert_array_equal(starts, [0, 1])
    np.testing.assert_array_equal(lens, [0, 0])


def test_split_lines_lone_cr():
    """FR-6: classic Mac endings. Needs the secondary scan (SPEC 3.3)."""
    starts, lens = _split(b"abc\rdefg\r")
    np.testing.assert_array_equal(starts, [0, 4])
    np.testing.assert_array_equal(lens, [3, 4])


def test_split_lines_all_three_terminators_mixed():
    starts, lens = _split(b"aa\r\nbbb\nc\rdddd\r\n")
    np.testing.assert_array_equal(starts, [0, 4, 8, 10])
    np.testing.assert_array_equal(lens, [2, 3, 1, 4])


def test_lone_cr_detection():
    assert detect_lone_cr(np.frombuffer(b"a\rb\n", np.uint8)) is True
    assert detect_lone_cr(np.frombuffer(b"a\r\nb\r\n", np.uint8)) is False
    assert detect_lone_cr(np.frombuffer(b"a\nb\n", np.uint8)) is False
    assert detect_lone_cr(np.frombuffer(b"", np.uint8)) is False
    assert detect_lone_cr(np.frombuffer(b"trailing\r", np.uint8)) is True


def test_lf_fast_path_ignores_lone_cr_when_told_to():
    """The opt-out exists so the benchmark measures the single-pass fast path."""
    buf = np.frombuffer(b"abc\rdefg\n", np.uint8)
    starts, lens = split_lines(buf, lone_cr=False)
    np.testing.assert_array_equal(starts, [0])
    np.testing.assert_array_equal(lens, [8])


# ---------------------------------------------------------------------------
# vectorised extraction (SPEC 4.4) -- against ground truth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario,name", ISO_FILES)
def test_vectorised_extraction_matches_ground_truth(corpus, manifests, scenario, name):
    """
    The central correctness claim of M0: timestamps, line numbers and byte
    offsets produced by pure column arithmetic equal what the generator recorded.
    """
    truth = files_of(manifests[scenario])[name]["entries"]
    raw = (corpus / scenario / name).read_bytes()
    buf = np.frombuffer(raw, np.uint8)

    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    line_no, offs, ets = entries_from(ts, valid, starts)

    assert ets.size == len(truth), f"{name}: {ets.size} entries vs {len(truth)}"
    np.testing.assert_array_equal(ets, [e["wall_us"] for e in truth])
    np.testing.assert_array_equal(line_no, [e["line_no"] for e in truth])
    np.testing.assert_array_equal(offs, [e["byte_off"] for e in truth])


def test_continuation_lines_are_not_entries(corpus, manifests):
    """FR-15: a stack trace is one entry, not N."""
    truth = files_of(manifests["basic"])["app.log"]
    raw = (corpus / "basic" / "app.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)

    assert int(valid.sum()) == truth["entry_count"]
    assert starts.size == truth["line_count"]
    assert starts.size > truth["entry_count"], "corpus has no multi-line entries"
    # every line that is not an entry start must be inside some entry's span
    assert int((~valid).sum()) == truth["line_count"] - truth["entry_count"]


def test_partial_last_line_is_excluded(corpus, manifests):
    """The unterminated final line is held back, one fewer entry than truth."""
    truth = files_of(manifests["edge"])["partial_last_line.log"]
    raw = (corpus / "edge" / "partial_last_line.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    assert int(valid.sum()) == truth["entry_count"] - 1


def test_leading_banner_does_not_defeat_extraction(corpus, manifests):
    """SPEC 4.2: detection must not depend on line 1 being a log line."""
    truth = files_of(manifests["edge"])["leading_banner.log"]
    raw = (corpus / "edge" / "leading_banner.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    line_no, _, _ = entries_from(ts, valid, starts)
    assert int(valid.sum()) == truth["entry_count"]
    assert int(line_no[0]) == truth["entries"][0]["line_no"] > 1


def test_no_timestamp_file_yields_no_entries(corpus):
    raw = (corpus / "edge" / "no_timestamps.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    _, valid = extract_fixed_iso(buf, starts, lens)
    assert int(valid.sum()) == 0


def test_empty_file_is_handled(corpus):
    raw = (corpus / "edge" / "empty.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    assert starts.size == 0 and ts.size == 0 and valid.size == 0


def test_short_line_cannot_borrow_bytes_from_its_successor():
    """The lens >= ISO_W guard; without it a short line reads into the next."""
    raw = b"x\n2026-10-04 14:22:08.113456 INFO ok\n"
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    _, valid = extract_fixed_iso(buf, starts, lens)
    np.testing.assert_array_equal(valid, [False, True])


def _expected_us(y, mo, d, h, mi, s, us):
    delta = dt.datetime(y, mo, d, h, mi, s, us) - dt.datetime(1970, 1, 1)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def test_extraction_across_midnight_takes_the_vector_date_path():
    """
    The constant-date fast path computes days_from_civil once as a scalar. A
    chunk spanning two calendar days must fall back and still be exact.
    """
    raw = (b"2026-10-04 23:59:59.999999 INFO before\n"
           b"2026-10-05 00:00:00.000001 INFO after\n"
           b"2026-10-05 00:00:01.000000 INFO later\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    assert valid.all()
    np.testing.assert_array_equal(ts, [
        _expected_us(2026, 10, 4, 23, 59, 59, 999999),
        _expected_us(2026, 10, 5, 0, 0, 0, 1),
        _expected_us(2026, 10, 5, 0, 0, 1, 0),
    ])


def test_extraction_across_new_year():
    raw = (b"2025-12-31 23:59:59.500000 INFO out with the old\n"
           b"2026-01-01 00:00:00.500000 INFO in with the new\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    assert valid.all()
    np.testing.assert_array_equal(ts, [
        _expected_us(2025, 12, 31, 23, 59, 59, 500000),
        _expected_us(2026, 1, 1, 0, 0, 0, 500000),
    ])


def test_constant_date_fast_path_ignores_invalid_lines(corpus):
    """
    The fast path tests date constancy over *valid* lines only. Continuation
    lines hold arbitrary bytes, so including them would both lose the fast path
    and risk reading a date out of a stack trace.
    """
    raw = (b"2026-10-04 10:00:00.000000 ERROR boom\n"
           b"    at Gateway.forward(Gateway.java:88)\n"
           b"9999-99-99 not even close to a date here\n"
           b"2026-10-04 10:00:01.000000 INFO recovered\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    np.testing.assert_array_equal(valid, [True, False, False, True])
    np.testing.assert_array_equal(ts[valid], [
        _expected_us(2026, 10, 4, 10, 0, 0, 0),
        _expected_us(2026, 10, 4, 10, 0, 1, 0),
    ])


def test_microsecond_digits_are_exact():
    """Each of the six fractional digits must land in the right decimal place."""
    raw = (b"2026-10-04 00:00:00.000001 INFO a\n"
           b"2026-10-04 00:00:00.000010 INFO b\n"
           b"2026-10-04 00:00:00.100000 INFO c\n"
           b"2026-10-04 00:00:00.123456 INFO d\n"
           b"2026-10-04 00:00:00.999999 INFO e\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts, valid = extract_fixed_iso(buf, starts, lens)
    assert valid.all()
    day0 = _expected_us(2026, 10, 4, 0, 0, 0, 0)
    np.testing.assert_array_equal(ts - day0, [1, 10, 100000, 123456, 999999])


def test_rejects_impossible_field_values():
    raw = (b"2026-13-04 14:22:08.113456 INFO bad month\n"
           b"2026-10-32 14:22:08.113456 INFO bad day\n"
           b"2026-10-04 25:22:08.113456 INFO bad hour\n"
           b"2026-10-04 14:61:08.113456 INFO bad minute\n"
           b"2026-10-04 14:22:08.113456 INFO fine\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    _, valid = extract_fixed_iso(buf, starts, lens)
    np.testing.assert_array_equal(valid, [False, False, False, False, True])


@pytest.mark.parametrize("scenario,name", [("formats", "ymd_hms_dot_f.log")])
def test_scalar_paths_agree_with_vectorised(corpus, scenario, name):
    """
    The scalar baselines exist to be beaten, but they must agree first --
    otherwise a throughput comparison is meaningless.
    """
    raw = (corpus / scenario / name).read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    ts_v, ok_v = extract_fixed_iso(buf, starts, lens)
    ts_r, ok_r = extract_scalar_regex(raw, starts)
    ts_s, ok_s = extract_scalar_strptime(raw, starts)
    np.testing.assert_array_equal(ok_v, ok_r)
    np.testing.assert_array_equal(ok_v, ok_s)
    np.testing.assert_array_equal(ts_v[ok_v], ts_r[ok_v])
    np.testing.assert_array_equal(ts_v[ok_v], ts_s[ok_v])


# ---------------------------------------------------------------------------
# severity (SPEC 10.3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario,name", [
    ("formats", "ymd_hms_dot_f.log"),
    ("formats", "apache_error.log"),
    ("formats", "jsonl.jsonl"),
    ("basic", "app.log"),
])
def test_severity_scan_matches_ground_truth(corpus, manifests, scenario, name):
    truth = files_of(manifests[scenario])[name]
    raw = (corpus / scenario / name).read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, _ = split_lines(buf)
    sev = severity_scan(raw, starts, starts.size)
    expected = [e["severity"] for e in truth["entries"]]
    got = [int(sev[e["line_no"] - 1]) for e in truth["entries"]]
    assert got == expected, name


def test_severity_lowercase_is_matched(corpus, manifests):
    """apache_error writes [core:error]; jsonl writes "level":"info"."""
    for scenario, name in [("formats", "apache_error.log"), ("formats", "jsonl.jsonl")]:
        truth = files_of(manifests[scenario])[name]
        assert max(e["severity"] for e in truth["entries"]) >= 6, name


def test_severity_folds_to_the_highest_on_a_line():
    raw = b"2026-10-04 14:22:08.113456 INFO retry after ERROR and a WARN\n"
    buf = np.frombuffer(raw, np.uint8)
    starts, _ = split_lines(buf)
    sev = severity_scan(raw, starts, 1)
    assert int(sev[0]) == 6


def test_severity_requires_word_boundaries():
    """
    Embedded occurrences must not count. Note the lines may not contain a
    standalone severity word anywhere -- the scan is case-insensitive and looks
    at the whole line, so "is not an error word" would legitimately match.
    """
    raw = (b"2026-10-04 14:22:08.113456 TERROR should be ignored\n"
           b"2026-10-04 14:22:09.113456 INFORMATIONAL banner text\n"
           b"2026-10-04 14:22:10.113456 xWARNx and DEBUGGER and SEVERELY\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, _ = split_lines(buf)
    sev = severity_scan(raw, starts, 3)
    np.testing.assert_array_equal(sev, [0, 0, 0])


def test_severity_is_case_insensitive_on_standalone_words():
    raw = (b"2026-10-04 14:22:08.113456 an error occurred downstream\n"
           b"2026-10-04 14:22:09.113456 Warn: disk nearly full\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, _ = split_lines(buf)
    sev = severity_scan(raw, starts, 2)
    np.testing.assert_array_equal(sev, [6, 5])


def test_both_severity_folds_agree(corpus):
    raw = (corpus / "basic" / "app.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, _ = split_lines(buf)
    a = severity_scan(raw, starts, starts.size, method="lexsort")
    b = severity_scan(raw, starts, starts.size, method="maxat")
    np.testing.assert_array_equal(a, b)


def test_detect_severity_offset_finds_the_fixed_position(corpus):
    """`2026-10-04 14:22:08.113456 INFO ...` puts the level at offset 27."""
    raw = (corpus / "formats" / "ymd_hms_dot_f.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    assert detect_severity_offset(raw, starts, lens) == 27


def test_detect_severity_offset_returns_none_when_unpredictable(corpus):
    """A variable-length logger name before the level defeats the fast path."""
    raw = (corpus / "edge" / "variable_sev_pos.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    assert detect_severity_offset(raw, starts, lens) is None


def test_regex_fallback_is_correct_where_the_fast_path_cannot_apply(corpus, manifests):
    """
    The case the fast path must decline. This is the only corpus file whose
    level offset varies, so it is what keeps the fallback honest.
    """
    truth = files_of(manifests["edge"])["variable_sev_pos.log"]
    raw = (corpus / "edge" / "variable_sev_pos.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, _ = split_lines(buf)
    sev = severity_scan(raw, starts, starts.size)
    got = [int(sev[e["line_no"] - 1]) for e in truth["entries"]]
    assert got == [e["severity"] for e in truth["entries"]]


def test_detect_severity_offset_on_file_with_no_levels(corpus):
    raw = (corpus / "formats" / "apache_access.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    assert detect_severity_offset(raw, starts, lens) is None


@pytest.mark.parametrize("scenario,name", [
    ("formats", "ymd_hms_dot_f.log"),
    ("formats", "ymd_hms.log"),
    ("basic", "app.log"),
    ("edge", "ooo_unique.log"),
])
def test_fixed_offset_severity_agrees_with_regex(corpus, scenario, name):
    """
    The fast path must be indistinguishable from the fallback, or the 13x
    speed-up it buys is worthless.
    """
    raw = (corpus / scenario / name).read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    off = detect_severity_offset(raw, starts, lens)
    assert off is not None, f"{name}: expected a fixed level offset"
    fast = severity_at_offset(buf, starts, lens, off)
    slow = severity_scan(raw, starts, starts.size)
    np.testing.assert_array_equal(fast, slow)


@pytest.mark.parametrize("scenario,name", [
    ("formats", "ymd_hms_dot_f.log"),
    ("basic", "app.log"),
])
def test_fixed_offset_severity_matches_ground_truth(corpus, manifests, scenario, name):
    truth = files_of(manifests[scenario])[name]
    raw = (corpus / scenario / name).read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    off = detect_severity_offset(raw, starts, lens)
    sev = severity_at_offset(buf, starts, lens, off)
    got = [int(sev[e["line_no"] - 1]) for e in truth["entries"]]
    assert got == [e["severity"] for e in truth["entries"]]


def test_fixed_offset_severity_respects_word_boundaries():
    """DEBUGGER must not read as DEBUG, even on the fast path."""
    raw = (b"HDR DEBUGGER tail\n"
           b"HDR DEBUG tail\n"
           b"HDR WARN\n"              # word ends exactly at line end
           b"HDR INFO_X tail\n"
           b"HDR TRACE1 tail\n")
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    sev = severity_at_offset(buf, starts, lens, 4)
    np.testing.assert_array_equal(sev, [0, 2, 5, 0, 0])


def test_fixed_offset_severity_is_case_insensitive():
    raw = b"HDR error x\nHDR Warn x\nHDR info x\n"
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    sev = severity_at_offset(buf, starts, lens, 4)
    np.testing.assert_array_equal(sev, [6, 5, 3])


def test_fixed_offset_severity_prefers_longer_word():
    """WARNING must win over WARN, CRITICAL over nothing."""
    raw = b"HDR WARNING x\nHDR CRITICAL x\nHDR SEVERE x\n"
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    sev = severity_at_offset(buf, starts, lens, 4)
    np.testing.assert_array_equal(sev, [5, 7, 7])


def test_fixed_offset_severity_handles_short_lines():
    raw = b"HD\nHDR INFO x\n\n"
    buf = np.frombuffer(raw, np.uint8)
    starts, lens = split_lines(buf)
    sev = severity_at_offset(buf, starts, lens, 4)
    np.testing.assert_array_equal(sev, [0, 3, 0])


def test_severity_on_file_without_severity_words(corpus, manifests):
    truth = files_of(manifests["formats"])["apache_access.log"]
    raw = (corpus / "formats" / "apache_access.log").read_bytes()
    buf = np.frombuffer(raw, np.uint8)
    starts, _ = split_lines(buf)
    sev = severity_scan(raw, starts, starts.size)
    assert int(sev.max()) == 0
    assert {e["severity"] for e in truth["entries"]} == {0}


# ---------------------------------------------------------------------------
# histogram and strip aggregation (SPEC 8)
# ---------------------------------------------------------------------------

def test_histogram_counts_everything():
    ts = np.array([0, 10, 1_000_000, 2_500_000, 2_999_999], np.int64)
    sev = np.array([3, 6, 3, 5, 6], np.uint8)
    h = histogram(ts, sev, 0, 1_000_000, 3)
    assert h.shape == (3, 8)
    assert int(h.sum()) == 5
    assert int(h[0, 3]) == 1 and int(h[0, 6]) == 1
    assert int(h[2, 5]) == 1 and int(h[2, 6]) == 1


def test_strip_aggregate_preserves_totals():
    rng = np.random.default_rng(3)
    h = rng.integers(0, 50, size=(5000, 8)).astype(np.int64)
    cum = build_cumulative(h)
    edges = np.linspace(0, 5000, 401).astype(np.int64)
    agg = strip_aggregate(cum, edges)
    assert agg.shape == (400, 8)
    np.testing.assert_array_equal(agg.sum(axis=0), h.sum(axis=0))


def test_strip_aggregate_row_and_time_modes_share_one_structure(corpus, manifests):
    """SPEC 8.3: the row-proportional strip re-uses the time histogram."""
    truth = files_of(manifests["basic"])["app.log"]["entries"]
    ts = np.array([e["wall_us"] for e in truth], np.int64)
    sev = np.array([e["severity"] for e in truth], np.uint8)
    t0, span = int(ts[0]), int(ts[-1]) - int(ts[0])
    nbk = 200
    h = histogram(ts, sev, t0, max(1, span // nbk + 1), nbk)
    cum = build_cumulative(h)
    time_edges = np.linspace(0, nbk, 51).astype(np.int64)
    row_edges = np.linspace(0, nbk, 97).astype(np.int64)
    assert int(strip_aggregate(cum, time_edges).sum()) == len(truth)
    assert int(strip_aggregate(cum, row_edges).sum()) == len(truth)


def test_build_cumulative_is_separable_from_repaint():
    """
    The cumsum must not be inside the repaint. Guards the fix for the 8.3ms
    repaint the M0 benchmark found (half a 60fps frame, NFR-5).
    """
    h = np.ones((1000, 8), np.int64)
    cum = build_cumulative(h)
    assert cum.shape == (1001, 8)
    np.testing.assert_array_equal(cum[0], np.zeros(8, np.int64))
    # repainting twice from the same cumulative gives the same answer
    edges = np.linspace(0, 1000, 65).astype(np.int64)
    a = strip_aggregate(cum, edges)
    b = strip_aggregate(cum, edges)
    np.testing.assert_array_equal(a, b)
    assert int(a.sum()) == int(h.sum())


# ---------------------------------------------------------------------------
# checkpointed merge (SPEC 6.2)
# ---------------------------------------------------------------------------

def _basic_arrays(manifests):
    files = manifests["basic"]["files"]
    return [np.array([e["wall_us"] for e in f["entries"]], np.int64) for f in files]


def test_merge_matches_the_corpus_merged_order(manifests):
    """FR-17 tie-break, against the generator's independently computed order."""
    arrays = _basic_arrays(manifests)
    expected = manifests["basic"]["merged"]
    m = MergePrototype(arrays)
    f_s, i_s, _ = m.materialise()
    got = [[int(a), int(b)] for a, b in zip(f_s, i_s, strict=True)]
    assert got == expected


def test_row_at_matches_materialised_order(manifests):
    arrays = _basic_arrays(manifests)
    expected = manifests["basic"]["merged"]
    m = MergePrototype(arrays)
    m.build_checkpoints(take=97)          # tiny windows: force many checkpoints
    for i in [0, 1, 2, 17, 100, 511, len(expected) // 2, len(expected) - 1]:
        f, e, _ = m.row_at(i)
        assert [f, e] == expected[i], f"row {i}"


def test_block_is_contiguous_and_correct(manifests):
    arrays = _basic_arrays(manifests)
    expected = manifests["basic"]["merged"]
    m = MergePrototype(arrays)
    m.build_checkpoints(take=97)
    for start in (0, 5, 63, 500):
        n = 60
        f_s, i_s, _ = m.block(start, n)
        got = [[int(a), int(b)] for a, b in zip(f_s, i_s, strict=True)]
        assert got == expected[start:start + n], f"block at {start}"


def test_time_to_row_counts_entries_before_t(manifests):
    arrays = _basic_arrays(manifests)
    m = MergePrototype(arrays)
    allts = np.sort(np.concatenate(arrays))
    for t in [int(allts[0]), int(allts[0]) - 1, int(allts[len(allts) // 3]),
              int(allts[-1]), int(allts[-1]) + 1]:
        assert m.time_to_row(t) == int((allts < t).sum()), t


def test_seek_then_read_lands_on_the_right_time(manifests):
    """NFR-6 round trip: the row at time t is the first row at or after t."""
    arrays = _basic_arrays(manifests)
    m = MergePrototype(arrays)
    m.build_checkpoints(take=500)
    allts = np.sort(np.concatenate(arrays))
    for t in [int(allts[len(allts) // 4]), int(allts[len(allts) // 2])]:
        row = m.time_to_row(t)
        if row < m.total:
            _, _, got = m.row_at(row)
            assert got >= t
        if row > 0:
            _, _, prev = m.row_at(row - 1)
            assert prev < t


def test_duplicate_timestamps_keep_a_stable_order():
    """Every entry shares one timestamp: order must be file order then index."""
    arrays = [np.full(5, 1000, np.int64), np.full(5, 1000, np.int64)]
    m = MergePrototype(arrays)
    f_s, i_s, _ = m.materialise()
    expected = [[0, i] for i in range(5)] + [[1, i] for i in range(5)]
    assert [[int(a), int(b)] for a, b in zip(f_s, i_s, strict=True)] == expected


def test_checkpoints_survive_a_long_run_of_identical_timestamps():
    """The window-widening path in build_checkpoints."""
    arrays = [np.full(3000, 42, np.int64), np.full(3000, 42, np.int64)]
    m = MergePrototype(arrays)
    m.build_checkpoints(take=16)
    assert m.total == 6000
    f, e, t = m.row_at(5999)
    assert (f, e, t) == (1, 2999, 42)


def test_skew_offset_reorders_the_merge(manifests):
    """D2 / FR-18: a per-file offset changes the interleave."""
    arrays = _basic_arrays(manifests)
    plain = MergePrototype(arrays)
    shifted = MergePrototype(arrays, skews=[0, 3_600_000_000, 0])  # +1h on file 1
    f0, i0, _ = plain.materialise()
    f1, i1, _ = shifted.materialise()
    assert list(f0) != list(f1)
    assert (sorted(zip(f0.tolist(), i0.tolist(), strict=True))
            == sorted(zip(f1.tolist(), i1.tolist(), strict=True)))


def test_single_source_merge_is_identity():
    arrays = [np.arange(100, dtype=np.int64) * 1000]
    m = MergePrototype(arrays)
    m.build_checkpoints(take=7)
    for i in (0, 50, 99):
        assert m.row_at(i) == (0, i, i * 1000)


def test_checkpoint_memory_is_far_below_a_materialised_order():
    """
    SPEC 6.1's whole justification: checkpoints must cost orders of magnitude
    less than the 8 bytes/row a materialised merge order would need.
    """
    rng = np.random.default_rng(11)
    arrays = [np.cumsum(rng.integers(1, 1000, size=50_000, dtype=np.int64))
              for _ in range(8)]
    m = MergePrototype(arrays)
    m.build_checkpoints()
    materialised = m.total * 8
    assert m.checkpoint_bytes() * 20 < materialised, (
        f"{m.checkpoint_bytes()} bytes for {m.total} rows "
        f"vs {materialised} materialised")
