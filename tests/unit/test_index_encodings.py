"""
The index layer across encodings -- SPEC 3.3, 4.4, FR-5.

UTF-16 is the case that would otherwise fail silently. In UTF-16 an ASCII digit
occupies two bytes, so SPEC 4.4's fixed-offset kernels find a NUL at every other
position and match nothing: the file indexes *zero entries* with no error
anywhere. These tests exist because that failure is invisible.

The approach under test is SPEC 3.3's: narrow the buffer being scanned, do not
transcode the file. So every offset in the index stays a true on-disk offset,
which is what the manifest's `byte_off` values are measured in and what copy
attribution (D9) and the M2 sidecar both need.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import BytesSource, files_of, index_of

from logweave.index.indexer import resolve_encoding
from logweave.source import encoding

CASES = [
    ("utf16le.log", "utf-16-le", 2),
    ("utf8_bom.log", "utf-8", 3),
    ("cp1252.log", "cp1252", 0),
    ("invalid_utf8.log", None, 0),
    ("mixed_newlines.log", None, 0),
]


@pytest.mark.parametrize(("name", "expect_encoding", "expect_bom"), CASES)
def test_encoded_files_index_exactly(corpus, manifests, name, expect_encoding, expect_bom):
    """
    Timestamps, line numbers and byte offsets, against ground truth.

    Byte offsets are the assertion that matters most here: they are what proves
    the file was *not* transcoded. A UTF-16 file narrowed for scanning has half
    as many scan positions as file bytes, so an index that forgot to map back
    would be internally consistent and wrong by a factor of two.
    """
    src = BytesSource(corpus / "edge" / name)
    if expect_encoding is not None:
        assert src.encoding_info.name == expect_encoding
    assert src.data_start() == expect_bom

    idx = index_of(corpus / "edge" / name)
    entries = files_of(manifests["edge"])[name]["entries"]
    assert idx.n == len(entries), f"{name}: indexed {idx.n} of {len(entries)}"

    for field, got in (
        ("wall_us", idx.ts.astype(np.int64)),
        ("line_no", idx.line_no.astype(np.int64)),
        ("byte_off", idx.off[: idx.n].astype(np.int64)),
    ):
        exp = np.array([e[field] for e in entries], dtype=np.int64)
        assert np.array_equal(got, exp), (
            f"{name}: {field} wrong for {int((got != exp).sum())} entries"
        )


def test_a_utf16_file_indexes_at_all(corpus):
    """
    Stated on its own because the failure mode is zero entries and no error.

    If someone removes the narrowing, every other UTF-16 assertion in this file
    fails with a confusing comparison; this one fails with the actual reason.
    """
    idx = index_of(corpus / "edge" / "utf16le.log")
    assert idx.n > 0, (
        "a UTF-16 file indexed nothing: the scan buffer was probably not "
        "narrowed, so the fixed-offset kernels saw a NUL every other byte"
    )
    assert idx.format_name
    assert idx.fast_path, "UTF-16 should still reach the vectorised path"


def test_utf16_offsets_are_on_disk_offsets_not_scan_offsets(corpus):
    """
    The coordinate-space guarantee, checked by reading the bytes back.

    An entry's recorded offset must point at that entry in the *file*, so the
    renderer can fetch and decode it. Reading at the offset and decoding must
    yield text that starts with the timestamp.
    """
    src = BytesSource(corpus / "edge" / "utf16le.log")
    idx = index_of(corpus / "edge" / "utf16le.log")

    lo, hi = idx.entry_range(0)
    raw = src.read_range(lo, min(hi, lo + 64))
    text = raw.decode("utf-16-le", errors="replace")
    assert text[:4].isdigit(), f"entry 0 did not start with a year: {text[:32]!r}"

    # Consecutive offsets must be two bytes per character apart, not one.
    deltas = np.diff(idx.off[: idx.n].astype(np.int64))
    assert np.all(deltas >= 2)
    assert np.all(deltas % 2 == 0), "a UTF-16 entry cannot start mid-code-unit"


@pytest.mark.parametrize("chunk", [64, 512, 4096])
def test_utf16_chunking_is_stable(corpus, chunk):
    """
    Chunk boundaries in UTF-16 can fall between the two bytes of a code unit.
    The phase calculation exists for exactly that, so odd-sized chunks must give
    the same answer as one big one.
    """
    whole = index_of(corpus / "edge" / "utf16le.log")
    part = index_of(corpus / "edge" / "utf16le.log", chunk_bytes=chunk)
    assert part.n == whole.n
    assert np.array_equal(part.ts, whole.ts)
    assert np.array_equal(part.off, whole.off)
    assert np.array_equal(part.line_no, whole.line_no)


def test_the_recorded_encoding_is_reported(corpus):
    """FR-19: the user is told how their file was read."""
    idx = index_of(corpus / "edge" / "utf16le.log")
    assert idx.encoding == "utf-16-le"


def test_resolve_encoding_prefers_what_the_source_resolved(corpus):
    """
    A user override (FR-5) arrives as the source's own `encoding_info`, so that
    must win over anything the indexer would detect from the bytes.
    """
    src = BytesSource(corpus / "edge" / "cp1252.log")
    src.encoding_info = encoding.from_name("utf-16-le")
    assert resolve_encoding(src, 0).name == "utf-16-le"


def test_resolve_encoding_detects_when_the_source_has_no_opinion(corpus):
    """A source that does not resolve encoding must not force a wrong default."""
    src = BytesSource(corpus / "edge" / "utf16le.log")
    del src.encoding_info
    assert resolve_encoding(src, 0).terminator_width == 2


def test_scan_phase_matches_the_narrowing_it_describes():
    """
    The caller applies `scan_phase` to map offsets back, so the two must agree
    exactly -- at every phase, which is what an odd chunk boundary produces.
    """
    info = encoding.from_name("utf-16-le")
    raw = "AB".encode("utf-16-le")  # b"A\x00B\x00"
    for start in range(4):
        sliced = raw[start:]
        phase = encoding.scan_phase(info, byte_start=start)
        narrowed = encoding.narrow_for_scan(sliced, info, byte_start=start)
        for p, byte in enumerate(narrowed):
            assert sliced[phase + 2 * p] == byte


def test_scan_phase_is_zero_for_single_byte_encodings():
    """So a caller can apply the mapping unconditionally."""
    for name in ("utf-8", "cp1252"):
        info = encoding.from_name(name)
        assert encoding.scan_phase(info, byte_start=7) == 0
        assert encoding.narrow_for_scan(b"abc", info, byte_start=7) == b"abc"
