"""
Tests for ``logweave.source.file`` and ``logweave.source.gzipped``.

SPEC 3.1, 3.2, 3.3, 5.4, 7.2; FR-4, FR-5, FR-23; NFR-20, NFR-21; D12.

Every ``read_range`` test runs twice, once mmapped and once buffered, because
the UNC path (SPEC 3.2) cannot be reached in CI but its code can: the strategy
is a constructor flag, so forcing it exercises the real branch.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys

import pytest

from logweave.source import gzipped
from logweave.source.base import ChangeKind, SourceError, SourceKind
from logweave.source.file import FileSource

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows share-mode behaviour")

BODY = b"".join(f"2026-10-04 12:00:{i:02d}.000000 INFO line {i}\n".encode() for i in range(60))

# True exercises the mmap strategy, False the positional-read strategy SPEC 3.2
# uses for UNC paths.
STRATEGIES = pytest.mark.parametrize("use_mmap", [True, False], ids=["mmap", "buffered"])


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "app.log"
    path.write_bytes(BODY)
    return path


@pytest.fixture(scope="module")
def edge(corpus):
    return corpus / "edge"


# -- construction and metadata ---------------------------------------------


def test_metadata_defaults_from_the_path(log):
    source = FileSource(log)
    assert source.meta.display_name == "app.log"
    assert source.meta.kind is SourceKind.FILE
    assert source.meta.origin == str(log)
    assert source.meta.alias == "app"
    assert str(source.id)


def test_source_id_is_not_a_path(log):
    """SPEC 3.1: ``SourceId`` is a session handle, and two opens stay distinct."""
    first, second = FileSource(log).id, FileSource(log).id
    assert str(log) not in str(first)
    assert first != second


def test_mmap_is_chosen_for_local_and_not_for_unc(log):
    assert FileSource(log)._use_mmap is True
    assert FileSource(r"\\server\share\app.log")._use_mmap is False


# -- open / close ----------------------------------------------------------


def test_open_reports_the_rung_that_worked(log):
    with FileSource(log) as source:
        assert source.report.method.value == "share_all"
        assert source.report.can_follow is True
        assert source.is_open


def test_open_is_idempotent_and_close_is_too(log):
    source = FileSource(log)
    source.open()
    fd = source._fd
    source.open()
    assert source._fd == fd
    source.close()
    source.close()
    assert not source.is_open


def test_opening_a_missing_file_raises_sourceerror(tmp_path):
    with pytest.raises(SourceError) as excinfo:
        FileSource(tmp_path / "nope.log").open()
    assert "nope.log" in str(excinfo.value)


def test_opening_a_directory_raises_sourceerror(tmp_path):
    with pytest.raises(SourceError) as excinfo:
        FileSource(tmp_path).open()
    assert "directory" in str(excinfo.value)


def test_reading_before_open_raises_sourceerror(log):
    source = FileSource(log)
    with pytest.raises(SourceError):
        source.read_range(0, 10)
    with pytest.raises(SourceError):
        source.size()


# -- size and read_range ---------------------------------------------------


@STRATEGIES
def test_size_and_full_read(log, use_mmap):
    with FileSource(log, use_mmap=use_mmap) as source:
        assert source.size() == len(BODY)
        assert source.read_range(0, source.size()) == BODY


@STRATEGIES
def test_read_range_clamps_to_the_extent(log, use_mmap):
    """SPEC 3.1: clamped, never short of an error."""
    with FileSource(log, use_mmap=use_mmap) as source:
        size = source.size()
        assert source.read_range(size - 10, size + 10_000) == BODY[-10:]
        assert source.read_range(-500, 5) == BODY[:5]
        assert source.read_range(-500, -1) == b""


@STRATEGIES
def test_read_range_out_of_range_is_empty_not_an_error(log, use_mmap):
    with FileSource(log, use_mmap=use_mmap) as source:
        size = source.size()
        assert source.read_range(size, size + 100) == b""
        assert source.read_range(size + 1000, size + 2000) == b""


@STRATEGIES
def test_read_range_inverted_is_empty(log, use_mmap):
    with FileSource(log, use_mmap=use_mmap) as source:
        assert source.read_range(100, 50) == b""
        assert source.read_range(100, 100) == b""


@STRATEGIES
def test_read_range_is_repeatable(log, use_mmap):
    """
    SPEC 3.1 calls for repeatable reads because the paint path re-reads the same
    ranges every frame while the view is merely redrawn.
    """
    with FileSource(log, use_mmap=use_mmap) as source:
        source.size()
        first = [source.read_range(a, a + 37) for a in (0, 11, 500, 1200)]
        second = [source.read_range(a, a + 37) for a in (0, 11, 500, 1200)]
        assert first == second
        assert first[0] == BODY[:37]
        assert first[2] == BODY[500:537]


def test_read_range_inside_the_known_extent_costs_no_syscall(log):
    """
    SPEC 3.1: "for a local file it should not cost a syscall".

    ``os.fstat`` is counted, not replaced with something that raises, and the
    patch covers only the two calls being measured. Patching it process-wide for
    the length of the test -- which is how this was first written -- also catches
    whatever the test runner itself does in that window: under pytest 9 it is
    pytest's own capture machinery that trips it, and the test then fails while
    reporting a fault in code that made no syscall at all.
    """
    with FileSource(log) as source:
        size = source.size()

        real = os.fstat
        calls = []

        def counted(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        os.fstat = counted
        try:
            head = source.read_range(0, 100)
            tail = source.read_range(size - 1, size)
        finally:
            os.fstat = real

        assert not calls, f"read_range re-stat-ed a range already in the extent: {calls}"
        assert head == BODY[:100]
        assert tail == BODY[-1:]


# -- the awkward files -----------------------------------------------------


@STRATEGIES
def test_zero_length_file(edge, use_mmap):
    """``mmap`` of 0 bytes is an error on every platform; the source must not be."""
    with FileSource(edge / "empty.log", use_mmap=use_mmap) as source:
        assert source.size() == 0
        assert source.read_range(0, 10) == b""
        assert source.identity().size == 0


@STRATEGIES
def test_a_file_that_grows_between_size_calls(tmp_path, use_mmap):
    path = tmp_path / "growing.log"
    path.write_bytes(BODY)
    with FileSource(path, use_mmap=use_mmap) as source:
        before = source.size()
        assert source.read_range(before, before + 10) == b""

        with open(path, "ab") as handle:
            handle.write(b"appended tail\n")

        after = source.size()
        assert after == before + 14
        assert source.read_range(before, after) == b"appended tail\n"
        assert source.read_range(0, after).endswith(b"appended tail\n")


@STRATEGIES
def test_a_file_that_grows_twice(tmp_path, use_mmap):
    """Each growth needs a fresh mapping; the second must not reuse the first."""
    path = tmp_path / "growing.log"
    path.write_bytes(b"one\n")
    with FileSource(path, use_mmap=use_mmap) as source:
        assert source.size() == 4
        for chunk in (b"two\n", b"three\n"):
            with open(path, "ab") as handle:
                handle.write(chunk)
            source.size()
        assert source.read_range(0, source.size()) == b"one\ntwo\nthree\n"


def test_a_vanished_file_is_still_readable_from_the_open_handle(tmp_path):
    """
    SPEC 7.2, "path gone, identity still open": keep reading to EOF, then stale.

    On Windows this works only because ``winfile`` granted FILE_SHARE_DELETE --
    which is also why the delete itself succeeds (NFR-20).
    """
    path = tmp_path / "doomed.log"
    path.write_bytes(BODY)
    with FileSource(path) as source:
        assert source.size() == len(BODY)
        os.remove(path)
        assert not path.exists()
        assert source.size() == len(BODY)
        assert source.read_range(0, 20) == BODY[:20]
        assert source.path_identity() is None, "the path no longer resolves"
        assert source.identity().size == len(BODY), "the handle still does"


@STRATEGIES
def test_the_one_megabyte_line(edge, use_mmap):
    """NFR-16. A single read must span it without special-casing."""
    with FileSource(edge / "huge_line.log", use_mmap=use_mmap) as source:
        raw = source.read_range(0, source.size())
        longest = max(raw.split(b"\n"), key=len)
        assert len(longest) > (1 << 20)


# -- identity, SPEC 7.2 ----------------------------------------------------


def test_identity_survives_reopen(log):
    with FileSource(log) as first:
        before = first.identity()
    with FileSource(log) as second:
        after = second.identity()
    assert before.same_file(after)
    assert before == after


def test_identity_sees_an_append_as_the_same_file(log):
    with FileSource(log) as source:
        before = source.identity()
        with open(log, "ab") as handle:
            handle.write(b"more\n")
        source.size()
        after = source.identity()
        assert after.same_file(before)
        assert after.size > before.size
        assert source.detect_change(before) is ChangeKind.APPENDED


def test_detect_change_classifies_the_spec_7_2_table(tmp_path):
    # Buffered, because on Windows a held mapping makes the file untruncatable --
    # see test_an_active_mapping_blocks_truncation_but_nothing_else.
    path = tmp_path / "app.log"
    path.write_bytes(BODY)
    with FileSource(path, use_mmap=False) as source:
        quiet = source.identity()
        assert source.detect_change(quiet) is ChangeKind.UNCHANGED

        with open(path, "ab") as handle:
            handle.write(b"tail\n")
        source.size()
        appended = source.identity()
        assert source.detect_change(quiet) is ChangeKind.APPENDED

        os.truncate(path, 6)
        source.size()
        assert source.detect_change(appended) is ChangeKind.TRUNCATED


def test_detect_change_does_not_call_a_small_append_a_rewrite(tmp_path):
    """
    The reason ``detect_change`` exists rather than a bare identity comparison.

    An identity-only comparison cannot decide this case. Two head hashes mean
    something only when they cover the same prefix, and for a log under 64KB --
    which every log is for its first few minutes -- an append extends the
    covered prefix, so the two hashes are not comparable at all. SPEC 7.2's
    action for a rewrite is "mark the discontinuity and re-index from 0", so
    guessing here would re-index a young log on every single append.

    ``replaced_in_place`` therefore declines to answer, and ``detect_change``
    re-hashes over the previous coverage to get the real one.
    """
    path = tmp_path / "young.log"
    path.write_bytes(b"line one\n")
    with FileSource(path) as source:
        before = source.identity()
        with open(path, "ab") as handle:
            handle.write(b"line two\n")
        source.size()

        assert not before.head_comparable(source.identity()), (
            "the append extended the covered prefix, which is the whole problem"
        )
        assert not before.replaced_in_place(source.identity()), (
            "with no comparable coverage the identity test must decline rather "
            "than call an append a rewrite"
        )
        assert source.detect_change(before) is ChangeKind.APPENDED


def test_detect_change_reports_a_rewrite_of_a_small_file(tmp_path):
    path = tmp_path / "young.log"
    path.write_bytes(b"line one\nline two\n")
    with FileSource(path) as source:
        before = source.identity()
        with open(path, "r+b") as handle:           # same length, new head
            handle.write(b"XXXX")
        source.size()
        assert source.detect_change(before) is ChangeKind.TRUNCATED


def test_detect_change_reports_a_vanished_path(tmp_path):
    path = tmp_path / "doomed.log"
    path.write_bytes(BODY)
    with FileSource(path) as source:
        held = source.identity()
        os.remove(path)
        assert source.detect_change(held) is ChangeKind.VANISHED


def test_detect_change_on_a_closed_source_is_unreachable(log):
    source = FileSource(log)
    source.open()
    held = source.identity()
    source.close()
    assert source.detect_change(held) is ChangeKind.UNREACHABLE


def test_identity_sees_a_rewrite_in_place(log):
    """SPEC 7.2: same inode, different head -- truncated and rewritten."""
    before = FileSource(log)
    before.open()
    original = before.identity()
    before.close()

    log.write_bytes(b"a totally different log, same path\n")
    with FileSource(log) as after:
        # The rewrite shrank the file, so the two head hashes cover different
        # prefixes and the identity-only test has nothing to go on.
        # `rewritten_given` re-hashes the live bytes over the original
        # coverage, which is a like-for-like comparison and sees it.
        assert not original.head_comparable(after.identity())
        assert original.rewritten_given(after.read_range(0, original.head_len))
        assert after.detect_change(original) is ChangeKind.TRUNCATED


def test_path_identity_detects_a_rotation(tmp_path):
    """A path resolving to a different file is the definition of a rotation."""
    path = tmp_path / "app.log"
    path.write_bytes(BODY)
    with FileSource(path) as source:
        held = source.identity()
        os.rename(path, tmp_path / "app.log.1")
        (tmp_path / "app.log").write_bytes(b"the successor\n")
        successor = source.path_identity()
        assert successor is not None
        assert not held.same_file(successor), "the name now belongs to another file"


# -- encoding wiring, SPEC 3.3 ---------------------------------------------


def test_open_resolves_the_encoding_onto_the_metadata(edge):
    with FileSource(edge / "utf16le.log") as source:
        assert source.encoding_info.name == "utf-16-le"
        assert source.encoding_info.terminator_width == 2
        assert source.meta.encoding == "utf-16-le"


def test_decode_range_of_a_utf16_source(edge):
    with FileSource(edge / "utf16le.log") as source:
        text = source.decode_range(0, 200)
        assert text.startswith("2026-")
        assert "\x00" not in text
        assert "\ufeff" not in text, "the BOM must be dropped at offset 0"


def test_decode_range_of_a_bom_source_drops_the_bom_once(edge):
    with FileSource(edge / "utf8_bom.log") as source:
        assert source.encoding_info.bom_len == 3
        assert not source.decode_range(0, 40).startswith("\ufeff")
        assert source.decode_range(3, 43).startswith("2026-")


def test_decode_range_never_raises_on_malformed_bytes(edge):
    """NFR-21, and the reason the index is in bytes (SPEC 3.3)."""
    with FileSource(edge / "invalid_utf8.log") as source:
        size = source.size()
        for start in range(0, size, 101):
            assert isinstance(source.decode_range(start, start + 101), str)


def test_set_encoding_is_instant_and_keeps_the_bom_length(edge):
    """
    FR-5 / SPEC 3.3: an override is a parameter swap, not a re-index.

    The byte offsets are untouched, so the same range decodes differently and
    nothing else in the source changes.
    """
    with FileSource(edge / "utf8_bom.log") as source:
        before = source.read_range(0, 64)
        info = source.set_encoding("cp1252")
        assert info.name == "cp1252"
        assert info.bom_len == 3, "the file still starts with a BOM"
        assert source.meta.encoding == "cp1252"
        assert source.read_range(0, 64) == before, "offsets must not move"
        assert not source.decode_range(0, 64).startswith("\ufeff")


def test_set_encoding_empty_string_redetects(edge):
    with FileSource(edge / "utf16le.log") as source:
        source.set_encoding("cp1252")
        assert source.set_encoding("").name == "utf-16-le"


def test_encoding_override_at_construction_skips_detection(edge):
    with FileSource(edge / "cp1252.log", encoding_override="latin-1") as source:
        assert source.encoding_info.name == "iso8859-1"
        assert source.encoding_info.detected_by == "override"


@pytest.mark.parametrize(
    ("name", "expect_name", "expect_width"),
    [
        ("utf8_bom.log", "utf-8", 1),
        ("utf16le.log", "utf-16-le", 2),
        ("mixed_newlines.log", "utf-8", 1),
        ("empty.log", "utf-8", 1),
    ],
)
def test_corpus_encodings(edge, name, expect_name, expect_width):
    with FileSource(edge / name) as source:
        assert source.encoding_info.name == expect_name
        assert source.encoding_info.terminator_width == expect_width


# -- data_start, the index layer's prologue contract -----------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("utf8_bom.log", 3),
        ("utf16le.log", 2),
        ("mixed_newlines.log", 0),
        ("cp1252.log", 0),
        ("invalid_utf8.log", 0),
        ("empty.log", 0),
    ],
)
def test_data_start(edge, name, expected):
    with FileSource(edge / name) as source:
        assert source.data_start() == expected


def test_data_start_matches_the_corpus_ground_truth(edge, manifests):
    """
    The number the index layer needs, checked against the generator's truth.

    ``gen_corpus`` records entry 0 of ``utf8_bom.log`` at byte 3 and of
    ``utf16le.log`` at byte 2, both offsets into the bytes as written to disk.
    That is what makes it an independent check rather than a restatement.
    """
    truth = {f["name"]: f for f in manifests["edge"]["files"]}
    for name in ("utf8_bom.log", "utf16le.log", "mixed_newlines.log", "cp1252.log"):
        with FileSource(edge / name) as source:
            assert source.data_start() == truth[name]["entries"][0]["byte_off"], name


def test_read_range_is_not_rebased_by_data_start(edge):
    """
    Offsets stay true offsets into the bytes on disk, as base.py requires.

    ``data_start`` only *reports* the prologue. Re-basing would silently shift
    every FileIndex offset, breaking copy attribution (D9) and the M2 sidecar.
    """
    path = edge / "utf8_bom.log"
    raw = path.read_bytes()
    with FileSource(path) as source:
        assert source.data_start() == 3
        assert source.size() == len(raw)
        assert source.read_range(0, 3) == b"\xef\xbb\xbf", "offset 0 is still the BOM"
        start = source.data_start()
        assert source.read_range(start, start + 4) == raw[3:7]


def test_utf16_offsets_are_original_file_offsets_not_transcoded(edge, manifests):
    """
    The UTF-16 coordinate-space decision, pinned.

    SPEC 3.3 keeps the index in the file's own bytes and handles UTF-16 with a
    2-byte terminator, so ``size()``, ``read_range()`` and ``data_start()`` are
    all in the on-disk space. Transcoding to UTF-8 would halve every offset and
    disagree with the generator's ground truth for every entry in the file.
    """
    path = edge / "utf16le.log"
    truth = {f["name"]: f for f in manifests["edge"]["files"]}["utf16le.log"]
    with FileSource(path) as source:
        assert source.size() == truth["size"] == path.stat().st_size
        assert source.data_start() == 2
        for entry in truth["entries"][:5]:
            off = entry["byte_off"]
            # Each entry begins with its timestamp, encoded UTF-16LE in place.
            assert source.read_range(off, off + 8) == "2026".encode("utf-16-le")


def test_terminator_bytes_tell_the_splitter_what_to_scan_for(edge):
    """FR-6: the splitter needs the terminator in bytes, having never decoded."""
    from logweave.source import encoding as enc

    with FileSource(edge / "utf16le.log") as source:
        term = enc.terminator_bytes(source.encoding_info)
        assert term == b"\n\x00"
        raw = source.read_range(0, source.size())
        assert raw.count(term) == len(raw.split(term)) - 1 >= 40

    with FileSource(edge / "mixed_newlines.log") as source:
        assert enc.terminator_bytes(source.encoding_info) == b"\n"


def test_gzip_reports_data_start_of_the_decompressed_bytes(tmp_path, cache):
    path = tmp_path / "bom.log.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"\xef\xbb\xbf2026-10-04 12:00:00 INFO first\n")
    with gzipped.GzipSource(path, cache_directory=cache) as source:
        assert source.data_start() == 3
        assert source.read_range(0, 3) == b"\xef\xbb\xbf"


# -- NFR-20, through the real source --------------------------------------


@pytest.mark.windows
@WINDOWS_ONLY
def test_an_open_filesource_does_not_block_rotation(tmp_path):
    """
    NFR-20 end to end: the mmapped read path must not pin the writer's file.

    This is the test that would catch a regression to ``open()`` in ``file.py``.
    """
    path = tmp_path / "app.log"
    path.write_bytes(BODY)
    with FileSource(path) as source:
        assert source.read_range(0, 16) == BODY[:16]
        rotated = tmp_path / "app.log.1"
        os.rename(path, rotated)
        os.remove(rotated)
        assert source.read_range(0, len(BODY)) == BODY


@pytest.mark.windows
@WINDOWS_ONLY
def test_an_active_mapping_blocks_truncation_but_nothing_else(tmp_path):
    """
    Pins the one measured limit of the mmap read path against NFR-20.

    Share flags do not govern mapped sections, so while a mapping is held
    Windows **does** allow append, rename and delete -- the rotation cases
    NFR-20 names -- but it refuses an open with truncate-on-open semantics
    (CREATE_ALWAYS, which ``open(path, "w")`` and ``logging.FileHandler(mode="w")``
    use) and, worse, it accepts ``SetEndOfFile`` while silently not shrinking the
    file. A copytruncate-style rotation would therefore believe it had emptied a
    log that keeps growing.

    ``use_mmap=False`` has no such limit, which is the escape hatch for a source
    whose writer truncates in place. If this test starts failing, Windows or
    CPython changed and the caveat in file.py's docstring can go.
    """
    path = tmp_path / "app.log"
    path.write_bytes(BODY)
    with FileSource(path, use_mmap=True) as source:
        source.size()
        with pytest.raises(OSError):
            open(path, "wb").close()
        os.truncate(path, 10)
        assert path.stat().st_size == len(BODY), "SetEndOfFile reports success and does nothing"
        with open(path, "ab") as handle:               # appending is unaffected
            handle.write(b"x")

    with FileSource(path, use_mmap=False) as buffered:  # buffered: no restriction
        buffered.size()
        os.truncate(path, 10)
        assert path.stat().st_size == 10
        open(path, "wb").close()


# -- GzipSource, D12 ------------------------------------------------------


@pytest.fixture
def gz(tmp_path):
    path = tmp_path / "app.log.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(BODY)
    return path


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


def test_gzip_decompresses_and_reads_randomly(gz, cache):
    with gzipped.GzipSource(gz, cache_directory=cache) as source:
        assert source.size() == len(BODY)
        assert source.read_range(0, 40) == BODY[:40]
        assert source.read_range(500, 540) == BODY[500:540]
        assert source.read_range(0, source.size()) == BODY
        assert source.meta.kind is SourceKind.GZIP
        assert "Decompressed" in source.report.note


def test_gzip_is_cached_and_reused(gz, cache):
    source = gzipped.GzipSource(gz, cache_directory=cache)
    source.open()
    target = source.cache_path()
    source.close()
    assert target.exists()
    assert list(cache.iterdir()) == [target]

    again = gzipped.GzipSource(gz, cache_directory=cache)
    again.open()
    try:
        assert "Reusing" in again.report.note
        assert again.size() == len(BODY)
    finally:
        again.close()


def test_gzip_cache_key_changes_when_the_archive_does(gz, cache, tmp_path):
    """SPEC 5.4's key includes size and mtime, so a rewrite cannot be mistaken."""
    first = gzipped.GzipSource(gz, cache_directory=cache).cache_path()
    with gzip.open(gz, "wb") as handle:
        handle.write(BODY * 2)
    second = gzipped.GzipSource(gz, cache_directory=cache).cache_path()
    assert first != second


def test_gzip_leaves_no_partial_file_behind(gz, cache):
    with gzipped.GzipSource(gz, cache_directory=cache) as source:
        source.size()
    assert not list(cache.glob("*.part"))


def test_gzip_identity_describes_the_decompressed_bytes(gz, cache):
    """
    Delegating identity to the cache copy keeps ``identity().size`` and ``size()``
    in agreement, which SPEC 5.4's sidecar validation depends on.
    """
    with gzipped.GzipSource(gz, cache_directory=cache) as source:
        assert source.identity().size == source.size() == len(BODY)


def test_gzip_detects_the_encoding_of_the_decompressed_bytes(tmp_path, cache):
    path = tmp_path / "bom.log.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"\xef\xbb\xbf2026-10-04 12:00:00 INFO caf\xc3\xa9\n")
    with gzipped.GzipSource(path, cache_directory=cache) as source:
        assert source.encoding_info.bom_len == 3
        assert "caf\u00e9" in source.decode_range(0, source.size())


def test_gzip_cannot_be_followed(gz, cache):
    with gzipped.GzipSource(gz, cache_directory=cache) as source:
        assert source.watch(lambda event: None) is None


def test_gzip_rejects_a_file_that_is_not_gzip(tmp_path, cache):
    path = tmp_path / "plain.log.gz"
    path.write_bytes(BODY)
    with pytest.raises(SourceError) as excinfo:
        gzipped.GzipSource(path, cache_directory=cache).open()
    assert "not a gzip file" in str(excinfo.value)


def test_gzip_reports_a_truncated_archive(gz, cache):
    """NFR-21: a corrupt archive is reported, never indexed as if complete."""
    gz.write_bytes(gz.read_bytes()[: max(20, gz.stat().st_size // 2)])
    with pytest.raises(SourceError) as excinfo:
        gzipped.GzipSource(gz, cache_directory=cache).open()
    assert "truncated or corrupt" in str(excinfo.value)
    assert not list(cache.glob("*")) or not list(cache.glob("*.log"))


def test_gzip_refuses_when_the_disk_is_short(gz, cache, monkeypatch):
    """The check that has to happen *before* the write, not halfway through."""
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(100, 100, 1024)
    )
    with pytest.raises(SourceError) as excinfo:
        gzipped.GzipSource(gz, cache_directory=cache).open()
    message = str(excinfo.value)
    assert "Not enough free disk space" in message
    assert "headroom" in message
    assert not list(cache.glob("*")), "nothing was written"


def test_isize_estimate_matches_a_small_archive(gz):
    assert gzipped.estimate_uncompressed_size(gz) == len(BODY)


def test_isize_estimate_is_lifted_past_a_wrap(tmp_path, monkeypatch):
    """
    ISIZE is the uncompressed size mod 2**32, so a value below the compressed
    size proves it wrapped. Text never compresses to more than its own length.
    """
    path = tmp_path / "big.log.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(BODY)
    real = os.stat
    monkeypatch.setattr(
        os, "stat",
        lambda p, *a, **k: type("S", (), {"st_size": (1 << 32) + 5_000_000})()
        if str(p) == str(path) else real(p, *a, **k),
    )
    estimate = gzipped.estimate_uncompressed_size(path)
    assert estimate >= (1 << 32) + 5_000_000
    assert estimate % gzipped.ISIZE_MODULUS == len(BODY) % gzipped.ISIZE_MODULUS


def test_cache_dir_follows_the_platform_convention(monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
        assert gzipped.cache_dir().parts[-2:] == ("LogWeave", "cache")
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg")
        assert gzipped.cache_dir().as_posix() == "/tmp/xdg/logweave/cache"


def test_gzip_methods_before_open_raise_sourceerror(gz, cache):
    source = gzipped.GzipSource(gz, cache_directory=cache)
    with pytest.raises(SourceError):
        source.size()
    with pytest.raises(SourceError):
        source.read_range(0, 10)
    with pytest.raises(SourceError):
        source.identity()
