"""
Indexing through the sidecar cache -- SPEC 5.4, NFR-4.

Three paths, and the middle one carries the risk. A *hit* either happens or
does not, and `test_sidecar.py` covers when it may. A *miss* is just indexing.
An **append** builds one index out of two halves produced minutes or days
apart, and if the halves disagree about anything -- the layout, the line
numbering, the ordering across the join -- the result is an index that is
wrong without being broken.

So the governing assertion in this file is that a spliced index is
*byte-identical* to a full re-index of the same file. Not equivalent, not
close: identical, array by array. Anything less and the cache changes what the
user sees depending on how they arrived at the file.
"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import BytesSource

from logweave.index import sidecar
from logweave.index.cached import open_index
from logweave.index.indexer import build_index


def lines(first: int, count: int) -> list[str]:
    return [f"2024-03-01 09:{(first + i) // 60 % 60:02d}:"
            f"{(first + i) % 60:02d}.{(first + i) % 1000:03d} "
            f"INFO  request {first + i} served\n"
            for i in range(count)]


def write(path, text: list[str]):
    path.write_text("".join(text), encoding="utf-8")
    return path


def opened(path, root, **kwargs):
    """Open `path` through the cache, as the session layer does."""
    src = BytesSource(path)
    return open_index(src, mtime_wall_us=src.mtime_wall_us, path=path,
                      root=root, **kwargs)


def built(path, **kwargs):
    src = BytesSource(path)
    return build_index(src, mtime_wall_us=src.mtime_wall_us, **kwargs)


def same_arrays(a, b) -> bool:
    return all(np.array_equal(np.asarray(getattr(a, n)),
                              np.asarray(getattr(b, n)))
               for n in ("off", "ts", "flags", "line_no"))


@pytest.fixture
def log(tmp_path):
    return write(tmp_path / "app.log", lines(1, 40))


@pytest.fixture
def root(tmp_path):
    return tmp_path / "cache"


# ---------------------------------------------------------------------------
# the three outcomes
# ---------------------------------------------------------------------------

def test_a_first_open_is_a_miss_and_leaves_a_sidecar(log, root):
    out = opened(log, root)
    assert out.status == "miss"
    assert out.reason == "no sidecar"
    assert out.wrote
    assert out.indexed_bytes == log.stat().st_size
    assert len(list(root.glob("*.lwi"))) == 1


def test_an_unchanged_reopen_is_a_hit_that_reads_nothing(log, root):
    first = opened(log, root)
    second = opened(log, root)

    assert second.status == "hit"
    assert second.indexed_bytes == 0
    assert second.reused_bytes == first.index.indexed_to
    assert same_arrays(first.index, second.index)


def test_a_hit_does_not_touch_the_log_file(log, root):
    """
    NFR-4 is met by *not reading*. A hit that still scanned the file would pass
    every correctness test here and miss the entire point.
    """
    opened(log, root)

    src = BytesSource(log)
    reads: list[tuple[int, int]] = []
    real = src.read_range

    def counting(start, end):
        reads.append((start, end))
        return real(start, end)

    src.read_range = counting
    out = open_index(src, mtime_wall_us=src.mtime_wall_us, path=log, root=root)

    assert out.status == "hit"
    # One read: the 64 KB head sample the lookup hashes. Nothing else.
    assert reads == [(0, sidecar.HEAD_SAMPLE)]


def test_a_grown_file_appends_rather_than_re_indexing(log, root):
    first = opened(log, root)
    write(log, lines(1, 55))

    out = opened(log, root)
    assert out.status == "append"
    assert out.reused_bytes == first.index.indexed_to
    assert out.indexed_bytes < log.stat().st_size
    assert out.index.n > first.index.n


def test_a_spliced_index_is_identical_to_a_full_re_index(log, root):
    """
    The assertion the append path lives or dies by. Array by array, including
    line numbers -- the user must not be able to tell how the index was built.
    """
    opened(log, root)
    write(log, lines(1, 55))
    spliced = opened(log, root)
    assert spliced.status == "append"

    fresh = built(log)
    assert same_arrays(spliced.index, fresh)
    assert spliced.index.n == fresh.n
    assert spliced.index.indexed_to == fresh.indexed_to
    assert spliced.index.stats.lines == fresh.stats.lines
    assert spliced.index.stats.monotonic == fresh.stats.monotonic


def test_repeated_appends_stay_identical_to_a_full_re_index(log, root):
    """
    One append is the easy case. Three in a row splice onto an index that was
    itself spliced, which is where an off-by-one in the boundary slot or the
    line base compounds instead of cancelling.
    """
    opened(log, root)
    for count in (48, 61, 77):
        write(log, lines(1, count))
        out = opened(log, root)
        assert out.status == "append", count
        assert same_arrays(out.index, built(log)), count


def test_an_append_keeps_the_boundary_entry_whole(log, root):
    """
    `off` has n+1 slots, the last being where the region ends. Keeping both the
    prefix's final slot and the tail's first would insert a zero-length entry
    at the join -- a blank row, in the middle of the file, only after a reopen.
    """
    first = opened(log, root)
    write(log, lines(1, 55))
    out = opened(log, root)

    off = np.asarray(out.index.off)
    assert off.size == out.index.n + 1
    assert np.all(np.diff(off) > 0), "a zero-length entry at the join"
    assert int(off[first.index.n]) == first.index.indexed_to


def test_line_numbers_continue_across_the_join(log, root):
    """
    D9: copied lines are attributed by file and line number. A tail numbered
    from 1 would put every attribution in the appended region wrong, which is
    exactly the part of the file someone tailing a log is looking at.
    """
    first = opened(log, root)
    write(log, lines(1, 55))
    out = opened(log, root)

    nums = np.asarray(out.index.line_no)
    assert nums[0] == 1
    assert np.all(np.diff(nums) > 0)
    assert nums[first.index.n] == int(np.asarray(first.index.line_no)[-1]) + 1
    assert np.array_equal(nums, np.asarray(built(log).line_no))


def test_line_numbers_continue_past_a_multi_line_entry(tmp_path, root):
    """
    The last indexed entry may span several physical lines, so its line count
    is read rather than assumed. A stack trace at the end of the prefix is the
    case that breaks the assumption.
    """
    log = tmp_path / "trace.log"
    head = [*lines(1, 10),
            "2024-03-01 09:00:30.000 ERROR upstream failed\n",
            "  File \"a.py\", line 12, in handle\n",
            "    raise Timeout()\n",
            "  Timeout: upstream\n"]
    write(log, head)
    opened(log, root)

    write(log, [*head, *lines(40, 6)])
    out = opened(log, root)
    assert out.status == "append"
    assert np.array_equal(np.asarray(out.index.line_no),
                          np.asarray(built(log).line_no))


def test_a_tail_that_opens_earlier_re_decides_monotonicity(tmp_path, root):
    """
    SPEC 4.6: each half can be sorted on its own while the pair is not. If
    monotonicity were carried over from the prefix, the merge would trust an
    order that does not hold and the appended rows would interleave wrongly.
    """
    log = tmp_path / "ooo.log"
    write(log, lines(30, 10))
    first = opened(log, root)
    assert first.index.stats.monotonic

    write(log, [*lines(30, 10), *lines(1, 5)])
    out = opened(log, root)

    assert out.status == "append"
    assert out.index.stats.monotonic is False
    assert out.index.order is not None
    ts = np.asarray(out.index.ts)
    ordered = ts[np.asarray(out.index.order)]
    assert np.all(np.diff(ordered) >= 0)
    assert np.array_equal(np.asarray(out.index.ts),
                          np.asarray(built(log).ts))


def test_an_append_writes_back_so_the_next_open_is_a_hit(log, root):
    opened(log, root)
    write(log, lines(1, 55))
    assert opened(log, root).status == "append"

    out = opened(log, root)
    assert out.status == "hit"
    assert out.index.n == built(log).n


def test_a_changed_head_falls_back_to_a_full_index(log, root):
    """Rotation: same path, different file. The prefix is worthless."""
    opened(log, root)
    write(log, ["2024-03-02 11:00:00.000 INFO  a new file entirely\n",
                *lines(1, 50)])

    out = opened(log, root)
    assert out.status == "miss"
    assert out.reason == "file head changed"
    assert same_arrays(out.index, built(log))


def test_a_truncated_file_falls_back_to_a_full_index(log, root):
    opened(log, root)
    write(log, lines(1, 12))

    out = opened(log, root)
    assert out.status == "miss"
    assert out.reason == "file shrank"
    assert same_arrays(out.index, built(log))


def test_a_sidecar_without_a_layout_falls_back_rather_than_guessing(log, root):
    """
    `_detection_for` returns None when the header carries no layout, and the
    caller must then re-index. Re-detecting on the tail alone is the thing that
    must not happen: a few hundred bytes can resolve to a different member of
    the ISO family than the whole file did.
    """
    out = opened(log, root)
    target = root / sidecar.cache_name(log, log.stat().st_size,
                                       log.stat().st_mtime_ns)
    assert target.exists()

    src = BytesSource(log)
    idx = build_index(src, mtime_wall_us=src.mtime_wall_us)
    idx.layout_json = None
    sidecar.write(idx, path=log, size=log.stat().st_size,
                  mtime_ns=log.stat().st_mtime_ns,
                  head=src.read_range(0, sidecar.HEAD_SAMPLE), root=root)

    write(log, lines(1, 55))
    out = opened(log, root)
    assert out.status == "miss"
    assert same_arrays(out.index, built(log))


# ---------------------------------------------------------------------------
# when the cache is not available
# ---------------------------------------------------------------------------

def test_the_cache_can_be_turned_off(log, root):
    out = opened(log, root, use_cache=False)
    assert out.status == "disabled"
    assert not out.wrote
    assert not root.exists()
    assert same_arrays(out.index, built(log))


def test_a_source_with_no_path_is_indexed_without_caching(log, root):
    """
    An in-memory or remote source has nothing stable to key on. Guessing a key
    would serve one source's arrays for another's bytes.
    """
    src = BytesSource(log)
    out = open_index(src, mtime_wall_us=src.mtime_wall_us, path=None, root=root)
    assert out.status == "disabled"
    assert not root.exists()


def test_an_unwritable_cache_is_a_missed_speedup_not_an_error(log, root,
                                                              monkeypatch):
    """
    A read-only cache directory, a full disk or a locked file must never stop
    someone reading their logs.
    """
    def boom(*args, **kwargs):
        raise OSError("cache directory is read-only")

    monkeypatch.setattr(sidecar, "write", boom)
    out = opened(log, root)
    assert out.status == "miss"
    assert out.wrote is False
    assert same_arrays(out.index, built(log))


def test_write_back_can_be_suppressed(log, root):
    out = opened(log, root, write_back=False)
    assert out.status == "miss"
    assert not out.wrote
    assert not root.exists() or not list(root.glob("*.lwi"))


def test_a_corrupt_sidecar_is_re_indexed(log, root):
    """
    Not every stale file is a well-formed one. A sidecar truncated by a crash
    has to read as a miss, not as a short index.
    """
    opened(log, root)
    target = next(root.glob("*.lwi"))
    target.write_bytes(b"garbage where a sidecar used to be")

    out = opened(log, root)
    assert out.status == "miss"
    assert same_arrays(out.index, built(log))


def test_an_empty_file_caches_nothing_and_reopens_cleanly(tmp_path, root):
    empty = write(tmp_path / "empty.log", [])
    first = opened(empty, root)
    assert first.index.n == 0
    assert not first.wrote

    second = opened(empty, root)
    assert second.status == "miss"
    assert second.index.n == 0


# ---------------------------------------------------------------------------
# what the UI is told
# ---------------------------------------------------------------------------

def test_each_outcome_describes_itself_for_the_status_bar(log, root):
    assert "indexed" in opened(log, root).describe()
    assert "cache hit" in opened(log, root).describe()

    write(log, lines(1, 55))
    text = opened(log, root).describe()
    assert "cache append" in text
    assert "reused" in text


def test_an_append_retires_the_sidecar_it_reused(log, root):
    """
    The prune this depends on cannot delete a file that is still mapped, and on
    Windows that failure is silent: a log reopened a few times would leave a
    near-full-size cache entry per visit, and the LRU sweep would spend its
    whole budget on dead copies of one file.
    """
    opened(log, root)
    write(log, lines(1, 55))
    out = opened(log, root)

    assert out.status == "append"
    names = list(root.glob("*.lwi"))
    assert len(names) == 1, [p.name for p in names]
    assert names[0].name == sidecar.cache_name(
        log, log.stat().st_size, log.stat().st_mtime_ns)


def test_a_spliced_index_can_be_written_back_and_appended_to_again(log, root):
    """
    A sidecar written from a spliced index has to carry everything the next
    append needs. Without the layout it does not, and every *other* reopen
    quietly becomes a full re-index -- a halving of the benefit that no single
    append test would notice.
    """
    opened(log, root)
    write(log, lines(1, 48))
    assert opened(log, root).status == "append"

    write(log, lines(1, 61))
    out = opened(log, root)
    assert out.status == "append"
    assert out.index.layout_json is not None
    assert out.index.severity_offset == built(log).severity_offset


# ---------------------------------------------------------------------------
# the overview histogram across a splice (SPEC 8.1, M5)
# ---------------------------------------------------------------------------

def test_a_spliced_index_has_the_histogram_a_full_re_index_would(log, root):
    """
    The same assertion this file is built on, applied to the histogram: a user
    must not be able to tell from the overview strips how the index was built.

    The splice *rebuilds* rather than adding the tail's counts to the prefix's,
    and the reason is in `_splice`: a longer span can widen the bucket, so the
    grid the prefix was counted on may no longer exist -- and an append that
    starts before the prefix's `t0`, which an out-of-order append does, has no
    bucket to be added to at all.
    """
    opened(log, root)
    write(log, lines(1, 55))
    spliced = opened(log, root)
    assert spliced.status == "append"

    fresh = built(log)
    assert spliced.index.hist is not None and fresh.hist is not None
    assert spliced.index.hist.t0 == fresh.hist.t0
    assert spliced.index.hist.bucket_us == fresh.hist.bucket_us
    assert np.array_equal(np.asarray(spliced.index.hist.counts),
                          np.asarray(fresh.hist.counts))
    assert spliced.index.hist.total == spliced.index.n


def test_a_splice_that_widens_the_span_recounts_on_the_new_grid(log, root):
    """
    The case adding would get wrong. The tail is days later, so the span grows
    past where one-second buckets fit and every bucket boundary moves.
    """
    opened(log, root)
    far = [f"2024-03-09 11:{i % 60:02d}:{i % 60:02d}.000 ERROR late {i}\n"
           for i in range(20)]
    write(log, lines(1, 40) + far)
    spliced = opened(log, root)
    assert spliced.status == "append"

    fresh = built(log)
    assert spliced.index.hist.bucket_us == fresh.hist.bucket_us
    assert np.array_equal(np.asarray(spliced.index.hist.counts),
                          np.asarray(fresh.hist.counts))
    assert spliced.index.hist.total == spliced.index.n


def test_the_histogram_is_mapped_on_a_hit_and_still_totals_the_entries(log, root):
    opened(log, root)
    out = opened(log, root)
    assert out.status == "hit"
    assert out.index.hist is not None
    assert out.index.hist.total == out.index.n
