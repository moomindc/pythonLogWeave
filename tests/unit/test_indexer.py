"""
The M1 acceptance tests: the index against independent ground truth.

These are the tests that retire risk R4 (a format silently misread, mis-ordering
a whole file). Everything is checked against `tools/gen_corpus.py`'s manifest,
which computes the same values with `datetime` arithmetic rather than the
engine's vectorised integer arithmetic -- so a bug in one cannot hide in the
other.

Three properties are asserted per entry, not one:

* `wall_us`  -- the timestamp. Wrong values mis-order the merged view.
* `line_no`  -- D9. Wrong values make copy attribution cite the wrong line.
* `byte_off` -- wrong values make the renderer show the wrong text.

A test that checked only timestamps would pass with completely broken
attribution, which is a user-visible promise of its own.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import files_of, index_of

from logweave.index.layouts import FLAG_MULTILINE, FLAG_TS_INFERRED

SCENARIOS = ("formats", "basic")


def _expect(entries: list[dict], key: str) -> np.ndarray:
    return np.array([e[key] for e in entries], dtype=np.int64)


def _cases(manifests: dict, scenario: str) -> list[str]:
    return sorted(files_of(manifests[scenario]))


# ---------------------------------------------------------------------------
# the whole pipeline, every format
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_entry_matches_ground_truth(corpus, manifests, scenario):
    """
    Every entry of every file: timestamp, line number and byte offset.

    This single test covers all 15 catalogue formats, including the two whose
    timestamps are incomplete in the text and must be inferred from mtime
    (syslog's missing year, strace's missing date).
    """
    truth = files_of(manifests[scenario])
    failures: list[str] = []

    for name, want in sorted(truth.items()):
        idx = index_of(corpus / scenario / name)
        entries = want["entries"]

        if idx.n != len(entries):
            failures.append(f"{name}: indexed {idx.n} entries, expected {len(entries)}")
            continue

        for field, got in (
            ("wall_us", idx.ts.astype(np.int64)),
            ("line_no", idx.line_no.astype(np.int64)),
            ("byte_off", idx.off[: idx.n].astype(np.int64)),
        ):
            exp = _expect(entries, field)
            if not np.array_equal(got, exp):
                i = int(np.flatnonzero(got != exp)[0])
                failures.append(
                    f"{name}: {field} wrong for {int((got != exp).sum())} entries; "
                    f"first at index {i}: got {got[i]}, expected {exp[i]} "
                    f"(format {idx.format_name})"
                )

    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_severity_matches_ground_truth(corpus, manifests, scenario):
    """Severity feeds the minimap and the timeline, so it is ground-truthed too."""
    truth = files_of(manifests[scenario])
    for name, want in sorted(truth.items()):
        idx = index_of(corpus / scenario / name)
        exp = _expect(want["entries"], "severity")
        got = idx.severity.astype(np.int64)
        assert got.size == exp.size, name
        assert np.array_equal(got, exp), (
            f"{name}: severity wrong for {int((got != exp).sum())} of {exp.size} entries"
        )


def test_every_catalogue_format_is_exercised(corpus, manifests):
    """
    Guards the corpus, not the engine.

    If a format is added to the catalogue with no corpus file, this test stays
    green and says nothing -- so it checks the other direction: that every file
    the generator claims to produce actually got a format detected. A silent
    drop to "no format" is how a regression would hide.
    """
    truth = files_of(manifests["formats"])
    undetected = []
    for name in sorted(truth):
        idx = index_of(corpus / "formats" / name)
        if not idx.format_name:
            undetected.append(name)
    assert not undetected, f"no format detected for: {undetected}"


# ---------------------------------------------------------------------------
# the index's own invariants
# ---------------------------------------------------------------------------

def test_index_costs_21_bytes_per_entry(corpus, manifests):
    """
    SPEC 2.3's budget, asserted rather than assumed.

    It sizes the sidecar cache and decides whether a multi-GB log fits in RAM,
    so a regression here is a capacity regression. A monotonic file must not
    allocate `order`; the extra 8 bytes are a third of the whole index.
    """
    idx = index_of(corpus / "formats" / "ymd_hms_dot_f.log")
    assert idx.stats.monotonic
    assert idx.order is None, "a monotonic file must not allocate the order permutation"
    assert idx.bytes_per_entry() == pytest.approx(21.0, abs=0.5)


def test_out_of_order_file_gets_a_sort_permutation(corpus):
    """
    SPEC 4.6. A log whose own timestamps go backwards still has to be
    searchable, so `order` is built -- and only then.
    """
    idx = index_of(corpus / "edge" / "ooo_unique.log")
    assert not idx.stats.monotonic
    assert idx.order is not None
    s = idx.ts_sorted
    assert np.all(np.diff(s) >= 0), "ts_sorted must be ascending"
    assert idx.bytes_per_entry() == pytest.approx(29.0, abs=0.5)


def test_ts_sorted_is_the_same_array_when_monotonic(corpus):
    """No copy for the common case: the merge engine binary-searches this."""
    idx = index_of(corpus / "formats" / "ymd_hms_dot_f.log")
    assert idx.ts_sorted is idx.ts


def test_duplicate_timestamps_are_kept_not_collapsed(corpus, manifests):
    """
    FR-17: entries sharing a timestamp are all present and stably ordered.
    Deduplicating them would silently lose log lines.
    """
    want = files_of(manifests["edge"])["ooo_duplicate.log"]
    idx = index_of(corpus / "edge" / "ooo_duplicate.log")
    assert idx.n == len(want["entries"])
    assert np.all(np.diff(idx.ts_sorted) >= 0)


def test_offsets_are_ascending_and_sealed(corpus):
    """
    `off` has n+1 slots and entry i's text runs to off[i+1] (SPEC 2.3), so the
    array must be ascending and its last slot must end the indexed region.
    """
    idx = index_of(corpus / "basic" / "app.log")
    assert idx.off.size == idx.n + 1
    assert np.all(np.diff(idx.off.astype(np.int64)) > 0)
    assert idx.indexed_to == int(idx.off[-1])
    lo, hi = idx.entry_range(0)
    assert 0 <= lo < hi <= idx.indexed_to


def test_multiline_entries_are_flagged(corpus, manifests):
    """FR-7: a stack trace is one entry, and is marked as spanning lines."""
    want = files_of(manifests["basic"])["app.log"]
    expected = sum(1 for e in want["entries"] if e["n_lines"] > 1)
    idx = index_of(corpus / "basic" / "app.log")
    assert expected > 0, "corpus should contain multi-line entries"
    assert int(idx.is_multiline.sum()) == expected
    assert idx.stats.multiline_entries == expected


def test_inferred_timestamps_are_flagged(corpus):
    """
    FR-19: a timestamp the engine completed from mtime is not as trustworthy as
    one that was written in the file, and the UI must be able to say so.
    """
    syslog = index_of(corpus / "formats" / "syslog_bdhms.log")
    assert np.all(syslog.ts_inferred), "every syslog entry's year was inferred"
    assert syslog.flags[0] & FLAG_TS_INFERRED

    iso = index_of(corpus / "formats" / "ymd_hms_dot_f.log")
    assert not np.any(iso.ts_inferred), "a complete timestamp is not inferred"


def test_flag_bits_do_not_collide(corpus, manifests):
    """
    SPEC 2.3 packs severity and three booleans into one byte. Severity occupies
    bits 0-2, so a severity of 7 must not read as a multi-line flag.
    """
    want = files_of(manifests["basic"])["app.log"]
    idx = index_of(corpus / "basic" / "app.log")
    exp_sev = _expect(want["entries"], "severity")
    assert np.array_equal(idx.severity.astype(np.int64), exp_sev)
    assert int(idx.severity.max()) <= 7
    # Independence: multi-line entries occur at all severities, so the two
    # fields must be separable.
    both = idx.is_multiline & (idx.severity == exp_sev)
    assert int(both.sum()) == int(idx.is_multiline.sum())
    assert FLAG_MULTILINE == 0b1000


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chunk", [64, 512, 4096, 100_003])
def test_chunk_size_does_not_change_the_result(corpus, chunk):
    """
    A chunk boundary must never split a line or an entry.

    Awkward sizes on purpose, including a prime that cannot align with any line
    length. This is the cheapest possible test for the carry-over logic and it
    covers the case that would otherwise only appear on a file larger than
    64 MB -- which no test suite should have to generate.
    """
    whole = index_of(corpus / "basic" / "app.log")
    part = index_of(corpus / "basic" / "app.log", chunk_bytes=chunk)
    assert part.n == whole.n, f"chunk_bytes={chunk} changed the entry count"
    assert np.array_equal(part.ts, whole.ts)
    assert np.array_equal(part.line_no, whole.line_no)
    assert np.array_equal(part.off, whole.off)


def test_chunking_survives_a_line_longer_than_the_chunk(corpus):
    """
    The corpus contains a 1 MB line. With a small chunk it cannot fit, so the
    indexer must accumulate rather than spin or truncate.
    """
    whole = index_of(corpus / "edge" / "huge_line.log")
    part = index_of(corpus / "edge" / "huge_line.log", chunk_bytes=8192)
    assert part.n == whole.n > 0
    assert np.array_equal(part.ts, whole.ts)
