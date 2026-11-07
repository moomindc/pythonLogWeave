"""
Encoding detection and decode-on-demand -- SPEC 3.3, FR-5, FR-6.

Detection order is BOM, then a UTF-8 validity scan of the first 256KB, then the
system ANSI codepage. UTF-16 is recognised by its BOM or by a high density of
NUL bytes at alternating positions, which is what ASCII text looks like in
UTF-16 when the writer omitted the BOM.

**The design point that makes this safe.** The index stores byte offsets,
always. Nothing in this module is ever applied to a whole file: decoding happens
only for the handful of entries being painted, with ``errors="replace"``. Three
consequences, all of them required:

* a malformed byte cannot corrupt an index or an ordering, because the index
  never saw a character (NFR-21, and the ``invalid_utf8.log`` corpus case);
* changing the encoding override is instant, with no re-index (FR-5), because
  an ``EncodingInfo`` is just a parameter to ``decode``;
* a UTF-16 source only needs to tell the line splitter that its terminators are
  2 bytes wide -- it does not need a different index.

So the API here is deliberately a detector plus a range decoder, and there is no
function that decodes a file.

No Qt, stdlib only (SPEC 1).
"""

from __future__ import annotations

import codecs
import locale
import sys
from dataclasses import dataclass

if sys.platform == "win32":
    import ctypes

SCAN_BYTES = 256 * 1024
"""SPEC 3.3: how much of the head the UTF-8 validity scan reads."""

#: Minimum share of alternating-position bytes that must be NUL before a
#: BOM-less file is called UTF-16. Real UTF-16 log text is overwhelmingly ASCII,
#: so the true density is near 1.0; 0.30 leaves room for runs of non-Latin text
#: while staying far above anything a single-byte encoding produces.
UTF16_NUL_DENSITY = 0.30

#: The opposite-parity density above which the NUL pattern is not UTF-16 at all
#: but a file full of NULs (a sparse or zero-padded log), which must not be
#: mis-decoded as text.
UTF16_NUL_DENSITY_MAX_OTHER = 0.10

#: Fewest bytes worth testing for the alternating-NUL pattern.
UTF16_MIN_SAMPLE = 16

LEGACY_FALLBACK = "cp1252"
"""Last-resort single-byte codec; see ``ansi_codepage``."""


@dataclass(frozen=True)
class EncodingInfo:
    """
    Everything the rest of the engine needs to know about a source's bytes.

    Frozen and tiny: it is passed to ``decode`` per painted entry, and swapping
    one for another is what an encoding override *is* (SPEC 3.3).
    """

    name: str = "utf-8"
    bom_len: int = 0
    terminator_width: int = 1
    detected_by: str = "default"

    @property
    def is_utf16(self) -> bool:
        return self.terminator_width == 2


def ansi_codepage() -> str:
    """
    The system's legacy single-byte codepage -- the last rung of SPEC 3.3.

    Windows: ``GetACP()``, the codepage the writer of a non-Unicode log actually
    used. POSIX: the locale's preferred encoding, except that a UTF-8 locale is
    rejected in favour of ``cp1252``. That exception matters: this function is
    only ever called *after* the UTF-8 scan has already proved the bytes are not
    UTF-8, so answering "utf-8" would be self-contradictory and would turn every
    legacy byte into a replacement character. cp1252 is the encoding such a log
    was written in far more often than any other, and it decodes every byte
    value, so nothing can fail.
    """
    if sys.platform == "win32":
        return f"cp{ctypes.windll.kernel32.GetACP()}"
    try:
        preferred = codecs.lookup(locale.getpreferredencoding(False)).name
    except (LookupError, ValueError):
        return LEGACY_FALLBACK
    return LEGACY_FALLBACK if preferred.startswith("utf") else preferred


def _normalise(name: str) -> str:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return name.lower().replace("_", "-")


def from_name(name: str, *, bom_len: int = 0, detected_by: str = "override") -> EncodingInfo:
    """
    Build an ``EncodingInfo`` for a user override (FR-5).

    The terminator width is derived from the codec, so overriding a source to
    UTF-16 correctly tells the splitter to look for 2-byte terminators without
    the caller having to know that rule.
    """
    canonical = _normalise(name)
    width = 2 if canonical.startswith("utf-16") else 1
    if canonical == "utf-8-sig":
        # The codec would eat the BOM itself, which would desynchronise byte
        # offsets from characters. Carry the BOM as a length instead.
        return EncodingInfo("utf-8", max(bom_len, 3), 1, detected_by)
    return EncodingInfo(canonical, bom_len, width, detected_by)


def _bom_info(sample: bytes) -> EncodingInfo | None:
    if sample.startswith(codecs.BOM_UTF8):
        return EncodingInfo("utf-8", 3, 1, "bom")
    # UTF-32 is tested before UTF-16: its LE BOM starts with the UTF-16LE BOM.
    if sample.startswith(codecs.BOM_UTF32_LE):
        return EncodingInfo("utf-32-le", 4, 4, "bom")
    if sample.startswith(codecs.BOM_UTF32_BE):
        return EncodingInfo("utf-32-be", 4, 4, "bom")
    if sample.startswith(codecs.BOM_UTF16_LE):
        return EncodingInfo("utf-16-le", 2, 2, "bom")
    if sample.startswith(codecs.BOM_UTF16_BE):
        return EncodingInfo("utf-16-be", 2, 2, "bom")
    return None


def is_valid_utf8(sample: bytes) -> bool:
    """
    True if ``sample`` is UTF-8, tolerating a multibyte sequence cut off at the end.

    The tolerance is not cosmetic: the sample is the first ``SCAN_BYTES`` of the
    file and that boundary lands mid-character for roughly three files in four
    that contain any non-ASCII text at all. An incremental decoder with
    ``final=False`` buffers the partial tail instead of rejecting it.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(sample, False)
    except UnicodeDecodeError:
        return False
    return True


def utf16_by_nul_density(sample: bytes) -> EncodingInfo | None:
    """
    Detect BOM-less UTF-16 by NUL bytes at alternating positions -- SPEC 3.3.

    ASCII text in UTF-16LE is ``c \\0 c \\0``, so the odd-indexed bytes are NUL;
    UTF-16BE puts them at even indices. Both densities being high means the
    sample is mostly NULs and is not text in either encoding, so it is rejected.
    """
    if len(sample) < UTF16_MIN_SAMPLE:
        return None
    even, odd = sample[0::2], sample[1::2]
    if not even or not odd:
        return None
    d_even = even.count(0) / len(even)
    d_odd = odd.count(0) / len(odd)
    if d_odd >= UTF16_NUL_DENSITY and d_even <= UTF16_NUL_DENSITY_MAX_OTHER:
        return EncodingInfo("utf-16-le", 0, 2, "nul-density")
    if d_even >= UTF16_NUL_DENSITY and d_odd <= UTF16_NUL_DENSITY_MAX_OTHER:
        return EncodingInfo("utf-16-be", 0, 2, "nul-density")
    return None


def detect(sample: bytes) -> EncodingInfo:
    """
    Resolve the encoding of a source from its first bytes -- SPEC 3.3, FR-5.

    ``sample`` should be the first ``SCAN_BYTES`` of the file; a shorter sample
    is fine and an empty one yields the UTF-8 default, because an empty file has
    no encoding to detect and must not be an error (the ``empty.log`` corpus
    case).

    Never raises. A file this cannot classify is reported as the ANSI codepage,
    which decodes every byte value, so the degrade-never-crash rule (NFR-21)
    holds without a special case anywhere above.
    """
    if not sample:
        return EncodingInfo()
    if (bom := _bom_info(sample)) is not None:
        return bom
    # Ahead of the UTF-8 scan on purpose. NUL is a perfectly valid UTF-8
    # character, so BOM-less UTF-16 ASCII text passes a UTF-8 validity check and
    # would be silently mis-decoded into text riddled with NULs.
    if (wide := utf16_by_nul_density(sample)) is not None:
        return wide
    if is_valid_utf8(sample):
        return EncodingInfo("utf-8", 0, 1, "utf8-scan")
    return EncodingInfo(ansi_codepage(), 0, 1, "ansi-codepage")


def terminator_bytes(info: EncodingInfo | None = None) -> bytes:
    """
    The exact bytes a line terminator occupies in this encoding -- SPEC 3.3, FR-6.

    For the index layer's line splitter, which scans bytes and never decodes.
    SPEC 3.3's rule for UTF-16 is that the splitter looks for a 2-byte
    terminator, not that the file is transcoded: ``b"\\n\\x00"`` for little
    endian, ``b"\\x00\\n"`` for big. A preceding CR, likewise widened, is part of
    the terminator and not part of the line.
    """
    info = info or EncodingInfo()
    if info.name == "utf-16-le":
        return b"\n\x00"
    if info.name == "utf-16-be":
        return b"\x00\n"
    return b"\n"


def narrow_for_scan(raw: bytes, info: EncodingInfo | None = None, *, byte_start: int = 0) -> bytes:
    """
    Drop the NUL half of UTF-16 code units, so byte-scanning kernels still work.

    SPEC 4.4's timestamp and severity extractors compare bytes at fixed offsets
    within a line. In UTF-16 an ASCII digit occupies two bytes, so those
    comparisons find a NUL every other position and match nothing -- which is
    why a UTF-16 file indexes zero entries through an unmodified scanner. The
    answer SPEC 3.3 implies is not to rewrite the file but to narrow the *buffer
    being scanned*: for ASCII-range text, taking every other byte recovers
    exactly the single-byte form the kernels expect.

    Returns ``raw`` unchanged for any single-byte encoding, so a caller can apply
    it unconditionally.

    **The offset relationship, which the caller must apply.** One narrowed byte
    per two file bytes, so a position ``p`` in the result is at ``byte_start +
    phase + 2 * p`` in the file, and a length ``n`` in the result spans ``2 * n``
    file bytes. ``byte_start`` is the range's file offset and fixes the phase:
    for UTF-16LE the low byte of each unit sits at an even file offset, so an odd
    ``byte_start`` is handled by skipping one byte. Non-ASCII characters are not
    representable in the narrow form and come through as their low byte; that is
    acceptable because this buffer is only ever scanned for ASCII literals and
    digits, never displayed. Display goes through ``decode``.
    """
    info = info or EncodingInfo()
    if info.terminator_width != 2:
        return raw
    return raw[scan_phase(info, byte_start=byte_start) :: 2]


def scan_phase(info: EncodingInfo | None = None, *, byte_start: int = 0) -> int:
    """
    Which byte of each UTF-16 unit ``narrow_for_scan`` keeps -- 0 or 1.

    Exposed because the caller cannot apply the offset relationship without it:
    a position ``p`` in the narrowed buffer is at ``byte_start + phase + 2 * p``
    in the file, and the index layer has to write real file offsets into a
    ``FileIndex``. Keeping the endianness rule in one place here means the
    indexer cannot drift out of step with the narrowing.

    Always 0 for a single-byte encoding, so a caller can apply it blind.
    """
    info = info or EncodingInfo()
    if info.terminator_width != 2:
        return 0
    low_byte_first = info.name != "utf-16-be"
    return byte_start % 2 if low_byte_first else (byte_start + 1) % 2


def decode(raw: bytes, info: EncodingInfo | None = None, *, byte_start: int = 0) -> str:
    """
    Decode one byte range for painting -- SPEC 3.3, FR-5.

    ``byte_start`` is the range's offset in the file, used only to drop the BOM
    when the range begins at the start of the file. Everything else is offset
    independent, which is what lets the index stay in bytes.

    ``errors="replace"`` is not a fallback here, it is the contract: a malformed
    byte becomes U+FFFD in one painted row and affects nothing else. For UTF-16 a
    range that starts or ends mid-unit is likewise replaced rather than refused,
    so a clamped or mid-character read cannot raise.
    """
    info = info or EncodingInfo()
    if info.bom_len and byte_start < info.bom_len:
        raw = raw[info.bom_len - byte_start:]
    if not raw:
        return ""
    try:
        return raw.decode(info.name, errors="replace")
    except LookupError:
        # An override naming a codec this Python does not have -- a hand-edited
        # profile, typically (SPEC 12.1). latin-1 maps every byte to a character
        # and cannot fail, so the row is still legible and nothing crashes.
        return raw.decode("latin-1", errors="replace")
