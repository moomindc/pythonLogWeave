"""
Tests for ``logweave.source.encoding`` -- SPEC 3.3, FR-5, FR-6.

Driven off the generated ``edge`` corpus, which exists precisely because these
files have each broken a log viewer somewhere: a BOM, UTF-16, a legacy codepage,
a stray continuation byte, mixed terminators, and nothing at all.

The invariant under test everywhere below is SPEC 3.3's: *bytes in, characters
only at the last moment*. No test here decodes a whole file, because no
production code path does either.
"""

from __future__ import annotations

import codecs

import pytest

from logweave.source import encoding as enc


@pytest.fixture(scope="module")
def edge(corpus):
    return corpus / "edge"


def head(path, limit=enc.SCAN_BYTES):
    return path.read_bytes()[:limit]


# -- BOMs -------------------------------------------------------------------


def test_utf8_bom_is_detected_and_its_length_reported(edge):
    info = enc.detect(head(edge / "utf8_bom.log"))
    assert info.name == "utf-8"
    assert info.bom_len == 3
    assert info.terminator_width == 1
    assert info.detected_by == "bom"


def test_utf8_bom_codec_is_not_utf_8_sig(edge):
    """
    ``utf-8-sig`` would swallow the BOM itself, desynchronising bytes from
    characters. SPEC 3.3 keeps the index in bytes, so the BOM is carried as a
    length and dropped explicitly by ``decode``.
    """
    info = enc.detect(head(edge / "utf8_bom.log"))
    assert info.name != "utf-8-sig"
    assert enc.from_name("utf-8-sig") == enc.EncodingInfo("utf-8", 3, 1, "override")


def test_utf16le_bom_sets_a_two_byte_terminator(edge):
    info = enc.detect(head(edge / "utf16le.log"))
    assert info.name == "utf-16-le"
    assert info.bom_len == 2
    assert info.terminator_width == 2, "FR-6: the splitter needs 2-byte terminators"
    assert info.is_utf16


def test_utf16be_bom(edge):
    info = enc.detect(codecs.BOM_UTF16_BE + "hello\n".encode("utf-16-be"))
    assert info.name == "utf-16-be"
    assert info.bom_len == 2
    assert info.terminator_width == 2


def test_utf32_bom_is_not_mistaken_for_utf16le():
    """The UTF-32LE BOM begins with the UTF-16LE BOM, so order matters."""
    info = enc.detect(codecs.BOM_UTF32_LE + "hi\n".encode("utf-32-le"))
    assert info.name == "utf-32-le"
    assert info.bom_len == 4


# -- the UTF-8 scan ---------------------------------------------------------


def test_plain_utf8_is_detected_by_the_scan(edge):
    info = enc.detect(head(edge / "mixed_newlines.log"))
    assert info.name == "utf-8"
    assert info.bom_len == 0
    assert info.terminator_width == 1
    assert info.detected_by == "utf8-scan"


def test_a_multibyte_character_cut_at_the_sample_boundary_is_still_utf8():
    """
    The 256KB sample boundary lands mid-character for most files containing any
    non-ASCII text, so the scan must tolerate an incomplete tail rather than
    declaring the whole file a legacy codepage.
    """
    full = ("café — naïve\n" * 8).encode("utf-8")
    assert enc.is_valid_utf8(full)
    assert enc.is_valid_utf8(full[:-1])
    assert enc.detect(full[:-1]).name == "utf-8"


def test_a_genuinely_broken_sequence_is_not_utf8():
    assert not enc.is_valid_utf8(b"ok\n\x80\xfe bad\n")


# -- UTF-16 without a BOM ---------------------------------------------------


def test_bomless_utf16le_is_detected_by_nul_density(edge):
    """SPEC 3.3: NUL at alternating positions. A UTF-8 scan would pass this."""
    body = head(edge / "utf16le.log")[2:]      # strip the BOM
    assert enc.is_valid_utf8(body), "NUL is valid UTF-8, which is the trap"
    info = enc.detect(body)
    assert info.name == "utf-16-le"
    assert info.bom_len == 0
    assert info.terminator_width == 2
    assert info.detected_by == "nul-density"


def test_bomless_utf16be_is_detected_by_nul_density():
    body = ("2026-10-04 12:00:00 INFO hello\n" * 40).encode("utf-16-be")
    info = enc.detect(body)
    assert info.name == "utf-16-be"
    assert info.terminator_width == 2


def test_a_file_of_nothing_but_nuls_is_not_called_utf16():
    """Both parities dense means zero padding, not text in either byte order."""
    assert enc.utf16_by_nul_density(b"\x00" * 512) is None


def test_ascii_is_not_called_utf16():
    assert enc.utf16_by_nul_density(b"plain ascii log line\n" * 20) is None


# -- the ANSI fallback ------------------------------------------------------


def test_cp1252_falls_back_to_a_single_byte_codepage(edge):
    info = enc.detect(head(edge / "cp1252.log"))
    assert info.detected_by == "ansi-codepage"
    assert info.bom_len == 0
    assert info.terminator_width == 1
    assert not info.name.startswith("utf"), (
        "the scan already proved the bytes are not UTF-8, so claiming a UTF-8 "
        "codepage here would turn every legacy byte into U+FFFD"
    )


def test_cp1252_round_trips_its_accented_text(edge):
    raw = (edge / "cp1252.log").read_bytes()
    info = enc.detect(raw[: enc.SCAN_BYTES])
    first = raw.split(b"\r\n")[0]
    text = enc.decode(first, info)
    assert "café" in text
    assert "naïve" in text


def test_ansi_codepage_never_returns_a_utf8_codec():
    assert not enc.ansi_codepage().startswith("utf")


# -- malformed bytes, NFR-21 ------------------------------------------------


def test_invalid_utf8_detects_without_raising(edge):
    info = enc.detect(head(edge / "invalid_utf8.log"))
    assert info.terminator_width == 1
    assert info.bom_len == 0


def test_invalid_utf8_decodes_without_raising(edge):
    """
    NFR-21. The corpus file is valid UTF-8 except one line holding a lone 0x80.

    Decoded as UTF-8 the bad bytes become U+FFFD and the surrounding text is
    intact: the damage is confined to the one painted row, which is the whole
    reason the index is in bytes.
    """
    raw = (edge / "invalid_utf8.log").read_bytes()
    bad_line = next(line for line in raw.split(b"\n") if b"\x80" in line)

    text = enc.decode(bad_line, enc.from_name("utf-8"))
    assert "�" in text
    assert "bad byte ->" in text
    assert "<- there" in text

    # And decoded with whatever detection chose, it also cannot raise.
    info = enc.detect(raw[: enc.SCAN_BYTES])
    assert enc.decode(bad_line, info)


def test_decoding_a_range_cut_mid_character_cannot_raise():
    raw = "café au lait\n".encode()
    for cut in range(1, len(raw)):
        assert isinstance(enc.decode(raw[:cut], enc.from_name("utf-8")), str)


def test_decoding_an_odd_length_utf16_range_cannot_raise():
    raw = "hello\n".encode("utf-16-le")
    assert isinstance(enc.decode(raw[:5], enc.EncodingInfo("utf-16-le", 0, 2)), str)


# -- empty files ------------------------------------------------------------


def test_empty_file_gets_the_utf8_default(edge):
    path = edge / "empty.log"
    assert path.read_bytes() == b""
    info = enc.detect(b"")
    assert info == enc.EncodingInfo("utf-8", 0, 1, "default")


def test_decoding_nothing_yields_an_empty_string():
    assert enc.decode(b"", enc.EncodingInfo()) == ""


# -- decode-on-demand semantics (SPEC 3.3) ---------------------------------


def test_decode_drops_the_bom_only_at_the_start_of_the_file(edge):
    raw = (edge / "utf8_bom.log").read_bytes()
    info = enc.detect(raw[: enc.SCAN_BYTES])
    first_line_end = raw.index(b"\n")

    at_zero = enc.decode(raw[:first_line_end], info, byte_start=0)
    assert not at_zero.startswith("﻿")
    assert at_zero.startswith("2026-")

    # A range from the middle of the file has no BOM to drop, and must not have
    # its first three bytes eaten. Asserted against a plain decode of the same
    # bytes rather than against the file's text, which the corpus generator does
    # not reproduce run to run.
    start = first_line_end + 1
    chunk = raw[start:start + 20]
    assert enc.decode(chunk, info, byte_start=start) == chunk.decode("utf-8", "replace")


def test_decode_is_offset_independent_everywhere_past_the_bom(edge):
    """
    The property that makes an encoding override instant (SPEC 3.3): decoding one
    range never depends on having decoded the previous one.
    """
    raw = (edge / "cp1252.log").read_bytes()
    info = enc.detect(raw[: enc.SCAN_BYTES])
    start, end = 200, 260
    assert enc.decode(raw[start:end], info, byte_start=start) == enc.decode(raw[start:end], info)


def test_an_override_changes_nothing_but_the_parameter(edge):
    """FR-5: switching codec is a field swap, with no file access at all."""
    raw = (edge / "cp1252.log").read_bytes()
    detected = enc.detect(raw[: enc.SCAN_BYTES])
    forced = enc.from_name("latin-1")
    sample = raw[:400]
    assert enc.decode(sample, detected) != enc.decode(sample, forced) or detected == forced
    assert forced.terminator_width == 1
    assert forced.detected_by == "override"


@pytest.mark.parametrize(
    ("name", "width"),
    [
        ("utf-8", 1),
        ("cp1252", 1),
        ("latin-1", 1),
        ("utf-16-le", 2),
        ("utf-16-be", 2),
        ("UTF_16_LE", 2),
    ],
)
def test_from_name_derives_the_terminator_width(name, width):
    assert enc.from_name(name).terminator_width == width


# -- what the byte-scanning index layer needs (SPEC 3.3, 4.4) ---------------


def test_terminator_bytes(edge):
    assert enc.terminator_bytes(enc.EncodingInfo()) == b"\n"
    assert enc.terminator_bytes(enc.from_name("cp1252")) == b"\n"
    assert enc.terminator_bytes(enc.detect(head(edge / "utf16le.log"))) == b"\n\x00"
    assert enc.terminator_bytes(enc.from_name("utf-16-be")) == b"\x00\n"


def test_narrow_for_scan_is_a_no_op_for_single_byte_encodings(edge):
    raw = head(edge / "cp1252.log", 128)
    assert enc.narrow_for_scan(raw, enc.detect(raw)) is raw


def test_narrow_for_scan_recovers_the_scannable_form_of_utf16(edge):
    """
    SPEC 4.4's extractors compare bytes at fixed offsets within a line, which
    finds a NUL every other position in UTF-16 and matches nothing. Narrowing the
    scanned buffer -- not the file -- is what makes a UTF-16 source indexable.
    """
    raw = (edge / "utf16le.log").read_bytes()
    info = enc.detect(raw[: enc.SCAN_BYTES])
    start = info.bom_len                      # 2: first content byte
    narrow = enc.narrow_for_scan(raw[start:], info, byte_start=start)
    assert narrow.startswith(b"2026-10-04 ")
    assert narrow.count(b"\n") >= 40


def test_narrow_for_scan_offsets_map_back_by_a_factor_of_two(edge):
    """The relationship the index layer has to apply to stay in file offsets."""
    raw = (edge / "utf16le.log").read_bytes()
    info = enc.detect(raw[: enc.SCAN_BYTES])
    start = info.bom_len
    narrow = enc.narrow_for_scan(raw[start:], info, byte_start=start)

    position = narrow.index(b"\n")             # end of the first line, narrowed
    file_offset = start + 2 * position
    assert raw[file_offset:file_offset + 2] == b"\n\x00"


@pytest.mark.parametrize("byte_start", [0, 1, 2, 3])
def test_narrow_for_scan_handles_either_phase(byte_start):
    """
    An odd range start must not shear every character in half.

    The result begins at the first whole code unit at or after ``byte_start``, so
    it is always a suffix of the text and never contains a high byte.
    """
    raw = "abcdef\n".encode("utf-16-le")
    narrow = enc.narrow_for_scan(raw[byte_start:], enc.from_name("utf-16-le"),
                                 byte_start=byte_start)
    assert b"abcdef\n".endswith(narrow)
    assert narrow
    assert b"\x00" not in narrow


def test_narrow_for_scan_handles_big_endian():
    raw = "abc\n".encode("utf-16-be")
    assert enc.narrow_for_scan(raw, enc.from_name("utf-16-be")) == b"abc\n"


def test_from_name_tolerates_an_unknown_codec_name():
    """NFR-21: a bad override from a hand-edited profile must not crash the open."""
    info = enc.from_name("not-a-real-codec")
    assert info.name == "not-a-real-codec"
    assert info.terminator_width == 1
    assert enc.decode(b"plain text\n", info) == "plain text\n"
