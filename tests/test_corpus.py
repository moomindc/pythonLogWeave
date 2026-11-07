"""
The corpus is the ground truth every later milestone is measured against, so it
gets checked for internal consistency first. A bug here would silently validate
a bug in the engine.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from conftest import files_of

LINEBREAK = re.compile(rb"\r\n|\n|\r")
SCENARIOS = ["basic", "formats", "edge"]


def test_all_scenarios_generated(manifests):
    assert set(manifests) == set(SCENARIOS)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_files_exist_with_recorded_size(corpus, manifests, scenario):
    for f in manifests[scenario]["files"]:
        path = corpus / scenario / f["name"]
        assert path.exists(), f["name"]
        assert path.stat().st_size == f["size"], f["name"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_line_numbering_is_contiguous(manifests, scenario):
    """line_no must advance by exactly the previous entry's physical line count."""
    for f in manifests[scenario]["files"]:
        entries = f["entries"]
        for prev, cur in zip(entries, entries[1:], strict=False):
            assert cur["line_no"] == prev["line_no"] + prev["n_lines"], \
                f"{f['name']}: line_no gap at {cur['line_no']}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_offsets_strictly_increase(manifests, scenario):
    for f in manifests[scenario]["files"]:
        offs = [e["byte_off"] for e in f["entries"]]
        assert offs == sorted(offs), f["name"]
        assert len(set(offs)) == len(offs), f["name"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_entry_spans_hold_exactly_n_lines(corpus, manifests, scenario):
    """
    The bytes between two entries must contain exactly as many line breaks as the
    first entry claims. This is what ties byte_off, n_lines and line_no together.
    """
    for f in manifests[scenario]["files"]:
        if "16" in f["encoding"]:
            continue                       # 2-byte terminators; covered separately
        raw = (corpus / scenario / f["name"]).read_bytes()
        entries = f["entries"]
        for i, e in enumerate(entries):
            end = entries[i + 1]["byte_off"] if i + 1 < len(entries) else len(raw)
            span = raw[e["byte_off"]:end]
            breaks = len(LINEBREAK.findall(span))
            # the final entry of a file may lack its terminator (SPEC 7.3)
            expected = e["n_lines"] if i + 1 < len(entries) else (e["n_lines"], e["n_lines"] - 1)
            if isinstance(expected, tuple):
                assert breaks in expected, f"{f['name']} last entry: {breaks}"
            else:
                assert breaks == expected, f"{f['name']} entry {i}: {breaks} != {expected}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_entry_starts_at_a_line_boundary(corpus, manifests, scenario):
    for f in manifests[scenario]["files"]:
        if "16" in f["encoding"]:
            continue
        raw = (corpus / scenario / f["name"]).read_bytes()
        bom = 3 if f["name"].endswith("bom.log") else 0
        for e in f["entries"]:
            off = e["byte_off"]
            if off in (0, bom):
                continue
            assert raw[off - 1] in (0x0A, 0x0D), f"{f['name']} offset {off}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_severity_in_range(manifests, scenario):
    for f in manifests[scenario]["files"]:
        for e in f["entries"]:
            assert 0 <= e["severity"] <= 7, f["name"]


def test_merged_order_is_sorted(manifests):
    """FR-17: (wall_us, file_order, entry_index), and every entry appears once."""
    m = manifests["basic"]
    files = m["files"]
    merged = m["merged"]
    assert merged is not None
    total = sum(len(f["entries"]) for f in files)
    assert len(merged) == total
    assert len({(a, b) for a, b in merged}) == total

    keys = [(files[fi]["entries"][ei]["wall_us"], fi, ei) for fi, ei in merged]
    assert keys == sorted(keys)


def test_formats_scenario_covers_every_format(manifests):
    import gen_corpus
    got = {f["format"] for f in manifests["formats"]["files"]}
    assert got == {f.name for f in gen_corpus.FORMATS}


def test_edge_cases_present(manifests):
    names = set(files_of(manifests["edge"]))
    for required in ["empty.log", "no_timestamps.log", "leading_banner.log",
                     "partial_last_line.log", "huge_line.log", "ooo_unique.log",
                     "ooo_duplicate.log", "newyear_syslog.log", "midnight_strace.log",
                     "mixed_newlines.log", "utf8_bom.log", "utf16le.log",
                     "cp1252.log", "invalid_utf8.log", "csv_quoted.csv",
                     "jsonl_varying.jsonl", "variable_sev_pos.log"]:
        assert required in names, required


def test_empty_file_is_empty(corpus, manifests):
    f = files_of(manifests["edge"])["empty.log"]
    assert f["size"] == 0
    assert f["entries"] == []


def test_huge_line_is_at_least_1mb(corpus, manifests):
    raw = (corpus / "edge" / "huge_line.log").read_bytes()
    longest = max(len(x) for x in raw.split(b"\n"))
    assert longest >= (1 << 20), longest


def test_out_of_order_files_really_are_out_of_order(manifests):
    """Guards against the generator accidentally emitting sorted timestamps."""
    for name in ("ooo_unique.log", "ooo_duplicate.log"):
        f = files_of(manifests["edge"])[name]
        ts = [e["wall_us"] for e in f["entries"]]
        assert ts != sorted(ts), name


def test_duplicate_timestamps_exist(manifests):
    f = files_of(manifests["edge"])["ooo_duplicate.log"]
    ts = [e["wall_us"] for e in f["entries"]]
    assert len(set(ts)) < len(ts)


def _as_datetime(wall_us: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(microseconds=wall_us)


def test_rollover_files_span_the_boundary(manifests):
    """New Year and midnight must actually be crossed, or the test is vacuous."""
    f = files_of(manifests["edge"])["newyear_syslog.log"]
    years = {_as_datetime(e["wall_us"]).year for e in f["entries"]}
    assert years == {2025, 2026}, years

    f = files_of(manifests["edge"])["midnight_strace.log"]
    days = {_as_datetime(e["wall_us"]).day for e in f["entries"]}
    assert len(days) == 2, days


def test_multiline_entries_exist(manifests):
    f = files_of(manifests["basic"])["app.log"]
    assert any(e["n_lines"] > 1 for e in f["entries"]), "no multi-line entries generated"


def test_apache_access_has_no_severity(manifests):
    f = files_of(manifests["formats"])["apache_access.log"]
    assert {e["severity"] for e in f["entries"]} == {0}


def test_manifest_records_local_tz(manifests):
    for m in manifests.values():
        assert "local_tz_offset_s" in m
