"""
The sidecar index cache -- SPEC 5.4, NFR-4, NFR-22.

The thing worth testing here is not that a cache hit is fast. It is that a hit
only ever happens when the arrays still describe the file. A sidecar served
when it should not be produces an index that disagrees with the bytes: wrong
timestamps, wrong offsets, garbled text, and no error anywhere. So most of this
file is about refusal -- every way a file can change, plus the parser version
that covers the changes a file cannot show.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import BytesSource

from logweave.index import sidecar
from logweave.index.indexer import build_index

LINES = [
    f"2024-03-01 09:15:{i % 60:02d}.{i:03d} INFO  worker started\n"
    for i in range(1, 41)
]
GROWN = [*LINES, "2024-03-01 09:17:00.000 WARN  slow\n"]


def write_log(path: Path, lines: list[str]) -> Path:
    path.write_text("".join(lines), encoding="utf-8")
    return path


def indexed(path: Path, **kwargs):
    src = BytesSource(path)
    return src, build_index(src, mtime_wall_us=src.mtime_wall_us, **kwargs)


def stat_of(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def store(path: Path, root: Path):
    """Index `path` and write its sidecar. Returns (source, index, head)."""
    src, idx = indexed(path)
    head = src.read_range(0, sidecar.HEAD_SAMPLE)
    size, mtime_ns = stat_of(path)
    sidecar.write(idx, path=path, size=size, mtime_ns=mtime_ns, head=head,
                  root=root)
    return src, idx, head


def look(path: Path, root: Path, head: bytes, **kwargs) -> sidecar.Reuse:
    size, mtime_ns = stat_of(path)
    return sidecar.lookup(path, size=size, mtime_ns=mtime_ns, head=head,
                          root=root, **kwargs)


def sidecar_of(path: Path, root: Path) -> Path:
    return root / sidecar.cache_name(path, *stat_of(path))


@pytest.fixture
def log(tmp_path) -> Path:
    return write_log(tmp_path / "app.log", LINES)


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "cache"


# ---------------------------------------------------------------------------
# the file format
# ---------------------------------------------------------------------------

def test_a_round_trip_reproduces_every_array_exactly(log, root):
    _, idx, head = store(log, root)
    got = look(log, root, head)

    assert got.full
    for name in ("off", "ts", "flags", "line_no"):
        assert np.array_equal(getattr(got.index, name), getattr(idx, name)), name
    assert got.index.n == idx.n
    assert got.index.indexed_to == idx.indexed_to


def test_the_scalar_fields_survive_the_round_trip(log, root):
    _, idx, head = store(log, root)
    got = look(log, root, head).index

    assert got.format_name == idx.format_name
    assert got.encoding == idx.encoding
    assert got.fast_path == idx.fast_path
    assert got.confidence == pytest.approx(idx.confidence)
    assert got.message_offset == idx.message_offset
    assert got.severity_offset == idx.severity_offset
    assert got.layout_json == idx.layout_json
    assert got.stats.lines == idx.stats.lines
    assert got.stats.monotonic == idx.stats.monotonic


def test_the_arrays_are_memory_mapped_not_copied(log, root):
    """
    NFR-4 is met only because nothing is read. A plain ndarray here would mean
    the whole sidecar had been loaded, which for a 10 GB log is 2 GB of arrays.
    """
    _, _, head = store(log, root)
    got = look(log, root, head).index
    assert isinstance(got.off, np.memmap)
    assert isinstance(got.ts, np.memmap)


def test_every_array_starts_on_a_page_boundary(log, root):
    """
    The whole point of the layout: an unaligned array has to be read and
    copied, an aligned one is handed over by the page cache.
    """
    _, _, head = store(log, root)
    header = sidecar.read_header(sidecar_of(log, root))
    offsets = [spec["offset"] for spec in header["arrays"]]
    assert offsets == sorted(offsets)
    assert all(off % sidecar.PAGE == 0 for off in offsets)


def test_the_header_names_the_file_and_the_parser(log, root):
    store(log, root)
    header = sidecar.read_header(sidecar_of(log, root))
    assert header["parser_version"] == sidecar.PARSER_VERSION
    assert header["format_version"] == sidecar.FORMAT_VERSION
    assert header["source"]["path"] == str(log)
    assert header["source"]["size"] == log.stat().st_size


def test_a_non_sidecar_file_reads_as_no_header(tmp_path):
    junk = tmp_path / "not-an-index.lwi"
    junk.write_bytes(b"this is not a sidecar at all")
    assert sidecar.read_header(junk) is None
    assert sidecar.read_header(tmp_path / "absent.lwi") is None


def test_a_truncated_sidecar_reads_as_no_header(log, root):
    """
    A half-written file must not read back as truth. The write is atomic, so
    this should be unreachable -- which is exactly why it is worth asserting.
    """
    target = sidecar_of(log, root)
    store(log, root)
    blob = target.read_bytes()
    target.write_bytes(blob[: len(sidecar.MAGIC) + 2])
    assert sidecar.read_header(target) is None


def test_an_oversized_header_is_refused_rather_than_written(log, root):
    """
    The array offsets assume the header fits in one page. A header that did not
    fit would put the arrays somewhere the reader does not look, so the writer
    fails loudly instead.
    """
    _, idx = indexed(log)
    idx.stats.notes = ["x" * 200] * 40
    with pytest.raises(ValueError, match="over one page"):
        sidecar.write(idx, path=log, size=1, mtime_ns=1, head=b"", root=root)
    assert not list(root.glob("*.lwi"))


def test_no_temporary_file_survives_a_write(log, root):
    store(log, root)
    assert not list(root.glob("*.tmp"))


# ---------------------------------------------------------------------------
# refusing to reuse
# ---------------------------------------------------------------------------

def test_no_sidecar_is_a_miss(log, root):
    got = look(log, root, b"head")
    assert got.miss
    assert got.reason == "no sidecar"


def test_a_changed_head_is_refused(log, root):
    """What rotation looks like: the same path, a different file."""
    store(log, root)
    # Same length, different bytes -- so this is the head check failing and
    # not the length check in front of it.
    got = look(log, root, b"X" * log.stat().st_size)
    assert got.miss
    assert got.reason == "file head changed"


def test_a_file_shorter_than_the_hashed_head_is_refused(log, root):
    """
    The head is compared over the length that was recorded, so a file now
    shorter than that cannot be compared at all -- and must not be reused on
    the strength of a short digest that happens to agree.
    """
    store(log, root)
    got = look(log, root, b"only a few bytes\n")
    assert got.miss
    assert got.reason == "file is shorter than the hashed head"


def test_a_grown_file_matches_on_the_prefix_that_was_hashed(log, root):
    """
    A log under 64 KB would otherwise hash its whole contents, so appending a
    line would look exactly like rotation and the append path could never fire
    for a young file -- which is precisely the file someone is tailing.
    """
    _, idx, head = store(log, root)
    write_log(log, GROWN)
    grown = log.read_bytes()
    assert len(grown) > len(head)

    got = look(log, root, grown, terminator_at=lambda off: True)
    assert got.partial
    assert got.resume_at == idx.indexed_to


def test_a_bumped_parser_version_invalidates_everything(log, root, monkeypatch):
    """
    The only check that catches a change the *file* cannot show: new extraction
    code producing different arrays from identical bytes.
    """
    _, _, head = store(log, root)
    assert look(log, root, head).full

    monkeypatch.setattr(sidecar, "PARSER_VERSION", sidecar.PARSER_VERSION + 1)
    got = look(log, root, head)
    assert got.miss
    assert got.reason == "parser version differs"


def test_a_differing_format_version_invalidates_everything(log, root, monkeypatch):
    _, _, head = store(log, root)
    monkeypatch.setattr(sidecar, "FORMAT_VERSION", sidecar.FORMAT_VERSION + 1)
    got = look(log, root, head)
    assert got.miss
    assert got.reason == "sidecar format version differs"


def test_a_shrunken_file_is_re_indexed(log, root):
    _, _, head = store(log, root)
    write_log(log, LINES[:10])
    got = look(log, root, head)
    assert got.miss
    assert got.reason == "file shrank"


def test_a_rewrite_of_identical_length_is_refused(log, root):
    """
    Same size, same head, a different mtime. The tail may have been rewritten
    in place and there is no cheap way to know, so it is not reused.
    """
    _, _, head = store(log, root)
    size, mtime_ns = stat_of(log)
    _restamp(sidecar_of(log, root), root, log, size=size, mtime_ns=mtime_ns - 10**9)

    got = look(log, root, head)
    assert got.miss
    assert got.reason == "mtime changed without growth"


def test_an_extent_past_the_end_of_the_file_is_refused(log, root):
    _, _, head = store(log, root)
    size, mtime_ns = stat_of(log)
    _restamp(sidecar_of(log, root), root, log, size=size - 1,
             mtime_ns=mtime_ns - 10**9, indexed_to=10**9)

    got = look(log, root, head)
    assert got.miss
    assert got.reason == "sidecar extent does not fit the file"


def test_an_indexed_region_ending_mid_line_is_refused(log, root):
    """
    The check that cannot be skipped. A sidecar whose last entry covers half a
    line puts every later offset wrong -- which surfaces as garbled text a
    thousand rows away, not as an error here.
    """
    _, _, head = store(log, root)
    write_log(log, GROWN)
    got = look(log, root, head, terminator_at=lambda off: False)
    assert got.miss
    assert got.reason == "indexed region ends mid-line"


def test_a_grown_file_offers_its_prefix_for_reuse(log, root):
    _, idx, head = store(log, root)
    before = idx.indexed_to
    write_log(log, GROWN)

    got = look(log, root, head, terminator_at=lambda off: True)
    assert got.partial
    assert got.resume_at == before
    assert "reusing" in got.reason


def test_the_terminator_is_only_asked_about_the_indexed_extent(log, root):
    _, idx, head = store(log, root)
    write_log(log, GROWN)
    asked: list[int] = []

    look(log, root, head, terminator_at=lambda off: asked.append(off) or True)
    assert asked == [idx.indexed_to]


def test_a_full_hit_never_consults_the_terminator(log, root):
    """An exact size and mtime match needs no boundary check -- nothing moved."""
    _, _, head = store(log, root)
    asked: list[int] = []

    got = look(log, root, head, terminator_at=lambda off: asked.append(off) or True)
    assert got.full
    assert asked == []


def test_the_newest_prefix_is_the_one_offered(tmp_path, log, root):
    """
    Several sidecars for one path can exist if a prune failed. The largest
    extent is the useful one; reusing an older, shorter prefix would be correct
    but would re-index bytes that are already indexed.
    """
    store(log, root)
    short = sidecar_of(log, root)
    stashed = shutil.copy2(short, tmp_path / "short.lwi")

    write_log(log, GROWN)
    _, idx, head = store(log, root)
    assert not short.exists(), "the prune should have taken the shorter entry"
    shutil.copy2(stashed, short)

    write_log(log, [*GROWN, "2024-03-01 09:18:00.000 INFO  done\n"])
    got = look(log, root, head, terminator_at=lambda off: True)
    assert got.partial
    assert got.resume_at == idx.indexed_to


# ---------------------------------------------------------------------------
# naming and pruning
# ---------------------------------------------------------------------------

def test_two_paths_never_share_a_cache_entry(tmp_path):
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.touch()
    b.touch()
    assert sidecar.cache_name(a, 1, 1) != sidecar.cache_name(b, 1, 1)


def test_a_long_and_punctuated_path_still_names_a_valid_file(tmp_path, root):
    deep = tmp_path / "dir with spaces & punctuation (v2)" / ("x" * 120 + ".log")
    deep.parent.mkdir(parents=True)
    write_log(deep, LINES)
    _, _, head = store(deep, root)
    assert look(deep, root, head).full


def test_size_and_mtime_are_in_the_name_so_staleness_is_visible(log, root):
    size, mtime_ns = stat_of(log)
    assert sidecar.cache_name(log, size, mtime_ns).endswith(
        f"-{size}-{mtime_ns}.lwi")


def test_a_successful_write_prunes_the_superseded_entry(log, root):
    """
    Size and mtime are in the filename, so a growing log would otherwise leave
    one near-full-size sidecar per reopen, and the LRU sweep would spend its
    whole budget on dead copies of a single file.
    """
    store(log, root)
    assert len(list(root.glob("*.lwi"))) == 1

    write_log(log, GROWN)
    store(log, root)
    names = list(root.glob("*.lwi"))
    assert len(names) == 1
    assert names[0].name == sidecar.cache_name(log, *stat_of(log))


def test_pruning_leaves_other_files_alone(tmp_path, root):
    one = write_log(tmp_path / "one.log", LINES)
    two = write_log(tmp_path / "two.log", LINES[:20])
    store(one, root)
    store(two, root)
    two_entry = sidecar_of(two, root)

    write_log(one, GROWN)
    store(one, root)
    assert len(list(root.glob("*.lwi"))) == 2
    assert two_entry.exists()


# ---------------------------------------------------------------------------
# housekeeping (NFR-22)
# ---------------------------------------------------------------------------

def test_usage_counts_files_and_bytes(tmp_path, root):
    assert sidecar.usage(root) == (0, 0)
    store(write_log(tmp_path / "one.log", LINES), root)
    store(write_log(tmp_path / "two.log", LINES[:20]), root)

    count, total = sidecar.usage(root)
    assert count == 2
    assert total == sum(p.stat().st_size for p in root.glob("*.lwi"))


def test_sweep_evicts_until_the_cache_fits(tmp_path, root):
    for i in range(4):
        store(write_log(tmp_path / f"f{i}.log", LINES), root)
    _, total = sidecar.usage(root)
    cap = total // 2

    freed = sidecar.sweep(cap_bytes=cap, root=root)
    assert freed > 0
    assert sidecar.usage(root)[1] <= cap


def test_sweep_evicts_the_least_recently_used_first(tmp_path, root):
    """
    By access time, not creation: the set of logs someone keeps returning to
    predicts what they will open next far better than the order they first
    opened them in.
    """
    paths = []
    for i in range(3):
        log = write_log(tmp_path / f"f{i}.log", LINES)
        store(log, root)
        paths.append(sidecar_of(log, root))

    now = time.time()
    os.utime(paths[0], (now, now))
    os.utime(paths[1], (now - 3600, now))
    os.utime(paths[2], (now - 86400, now))

    sidecar.sweep(cap_bytes=sum(p.stat().st_size for p in paths) - 1, root=root)
    assert not paths[2].exists()
    assert paths[0].exists()


def test_sweep_under_the_cap_evicts_nothing(tmp_path, root):
    store(write_log(tmp_path / "one.log", LINES), root)
    assert sidecar.sweep(cap_bytes=sidecar.DEFAULT_CAP_BYTES, root=root) == 0
    assert sidecar.usage(root)[0] == 1


def test_clear_empties_the_cache_and_reports_what_it_freed(tmp_path, root):
    store(write_log(tmp_path / "one.log", LINES), root)
    store(write_log(tmp_path / "two.log", LINES[:20]), root)
    _, total = sidecar.usage(root)

    assert sidecar.clear(root) == total
    assert sidecar.usage(root) == (0, 0)


def test_housekeeping_on_a_missing_directory_is_quiet(tmp_path):
    absent = tmp_path / "never-created"
    assert sidecar.usage(absent) == (0, 0)
    assert sidecar.sweep(root=absent) == 0
    assert sidecar.clear(absent) == 0


def test_the_default_root_is_per_user_and_outside_the_log_directory(
        real_cache_root):
    """
    SPEC 5.4: log directories are often read-only, watched by a shipper, or on
    a share where a stray file would be noticed and resented.
    """
    root = real_cache_root()
    assert root.is_absolute()
    assert "logweave" in str(root).casefold()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _restamp(target: Path, root: Path, log: Path, *, size: int, mtime_ns: int,
             indexed_to: int | None = None) -> Path:
    """
    Rewrite a sidecar's header and rename it to match, in place.

    Rewriting rather than regenerating is what lets a test present a header the
    writer would never produce -- an impossible extent, a mismatched mtime --
    which is precisely what the reader has to survive. The header region is
    padded to a page, so everything from the first page onward is untouched and
    the arrays stay where the header says they are.
    """
    blob = target.read_bytes()
    at = len(sidecar.MAGIC)
    (hlen,) = sidecar.HEADER_LEN.unpack(blob[at:at + sidecar.HEADER_LEN.size])
    at += sidecar.HEADER_LEN.size
    header = json.loads(blob[at:at + hlen].decode("utf-8"))

    header["source"]["size"] = size
    header["source"]["mtime_ns"] = mtime_ns
    if indexed_to is not None:
        header["indexed_to"] = indexed_to

    new = json.dumps(header, separators=(",", ":")).encode("utf-8")
    head = sidecar.MAGIC + sidecar.HEADER_LEN.pack(len(new)) + new
    assert len(head) <= sidecar.PAGE
    target.write_bytes(head + b"\0" * (sidecar.PAGE - len(head))
                       + blob[sidecar.PAGE:])

    moved = root / sidecar.cache_name(log, size, mtime_ns)
    target.replace(moved)
    return moved
