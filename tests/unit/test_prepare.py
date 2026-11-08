"""
Warming the cache before a session opens -- SPEC 5.3, NFR-3, NFR-5.

Two things have to hold. The obvious one: after `prepare`, opening those files
is a cache hit. The one that actually bites: `prepare` must never be able to
make opening *fail*. It is an optimisation that runs in other processes on
someone's machine, where pools break, sandboxes forbid spawning and files
vanish between the stat and the read -- and every one of those has to come out
as a missed speedup rather than a log the user cannot see.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import BytesSource

from logweave.index import sidecar
from logweave.index.cached import open_index
from logweave.session import prepare as mod
from logweave.session.prepare import PARALLEL_THRESHOLD, Prepared, prepare, worker_count


def lines(first: int, count: int) -> list[str]:
    return [f"2024-03-01 09:{(first + i) // 60 % 60:02d}:"
            f"{(first + i) % 60:02d}.{(first + i) % 1000:03d} "
            f"INFO  request {first + i} served\n"
            for i in range(count)]


def write(path: Path, text: list[str]) -> Path:
    path.write_text("".join(text), encoding="utf-8")
    return path


def reopen(path: Path, root: Path):
    """Open through the cache the way the session does, and report the status."""
    src = BytesSource(path)
    return open_index(src, mtime_wall_us=src.mtime_wall_us, path=path, root=root)


@pytest.fixture
def logs(tmp_path) -> list[Path]:
    return [write(tmp_path / f"app{i}.log", lines(1 + i, 30 + i))
            for i in range(3)]


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "cache"


# ---------------------------------------------------------------------------
# what warming is for
# ---------------------------------------------------------------------------

def test_warming_turns_the_next_open_into_a_hit(logs, root):
    """The whole contract. Everything else here is about not breaking it."""
    result = prepare(logs, root=root)

    assert [f.status for f in result.files] == ["miss"] * len(logs)
    for path in logs:
        assert reopen(path, root).status == "hit"


def test_warming_an_already_warm_set_does_nothing(logs, root):
    prepare(logs, root=root)
    again = prepare(logs, root=root)

    assert [f.status for f in again.files] == ["hit"] * len(logs)
    assert again.indexed_bytes == 0
    assert "already indexed" in again.summary()


def test_warming_a_grown_file_appends(logs, root):
    prepare(logs, root=root)
    write(logs[0], lines(1, 60))

    result = prepare([logs[0]], root=root)
    assert [f.status for f in result.files] == ["append"]
    assert reopen(logs[0], root).status == "hit"


def test_the_entry_count_comes_back_without_the_arrays(logs, root):
    """
    SPEC 5.3: bulk data never crosses a process boundary. What a worker returns
    is a summary; the parent gets the arrays by mapping the sidecar.
    """
    result = prepare(logs, root=root)
    assert len(result.files) == len(logs)
    for got in result.files:
        assert got.entries > 0
        assert got.entries == reopen(Path(got.path), root).index.n
    assert all(isinstance(f, Prepared) for f in result.files)


def test_progress_is_reported_once_per_file(logs, root):
    seen: list[tuple[int, int, str]] = []
    prepare(logs, root=root,
            progress=lambda done, total, got: seen.append(
                (done, total, got.status)))

    assert [d for d, _, _ in seen] == [1, 2, 3]
    assert all(total == 3 for _, total, _ in seen)
    assert {Path(f.path).name for f in prepare(logs, root=root).files} == \
        {p.name for p in logs}


def test_cancellation_stops_between_files(logs, root):
    seen: list[str] = []

    def progress(done, total, got):
        seen.append(got.path)

    result = prepare(logs, root=root, progress=progress,
                     cancelled=lambda: len(seen) >= 1)
    assert result.cancelled
    assert len(result.files) == 1
    # The file that did finish is in the cache and costs nothing to open.
    assert reopen(Path(result.files[0].path), root).status == "hit"


def test_cancelling_before_the_first_file_does_nothing(logs, root):
    result = prepare(logs, root=root, cancelled=lambda: True)
    assert result.cancelled
    assert result.files == []
    assert not root.exists() or not list(root.glob("*.lwi"))


# ---------------------------------------------------------------------------
# failing without taking the open down with it
# ---------------------------------------------------------------------------

def test_a_missing_file_is_reported_not_raised(tmp_path, root):
    """
    The session opens the same path straight afterwards and reports the real
    error against the file it belongs to. Raising here would lose the other
    forty-nine files in the drop.
    """
    result = prepare([tmp_path / "not-there.log"], root=root)
    assert [f.status for f in result.files] == ["failed"]
    assert result.files[0].reason


def test_one_bad_file_does_not_stop_the_others(logs, root, tmp_path):
    paths = [logs[0], tmp_path / "gone.log", logs[1]]
    result = prepare(paths, root=root)

    assert {f.status for f in result.files} == {"miss", "failed"}
    assert reopen(logs[0], root).status == "hit"
    assert reopen(logs[1], root).status == "hit"


def test_a_broken_pool_falls_back_to_indexing_in_process(logs, root, monkeypatch):
    """
    A worker killed by the OS, a sandbox that forbids spawning, a frozen build
    without multiprocessing: slower, not broken. And still warm afterwards.
    """
    class Exploding:
        def __init__(self, *a, **k):
            raise OSError("no process pool here")

    monkeypatch.setattr(mod, "ProcessPoolExecutor", Exploding)
    monkeypatch.setattr(mod, "PARALLEL_THRESHOLD", 0)

    result = prepare(logs, root=root, workers=4)
    assert result.parallel is False
    assert result.workers == 1
    assert any("no worker pool" in f.reason for f in result.files)
    for path in logs:
        assert reopen(path, root).status == "hit"


def test_a_worker_dying_mid_flight_falls_back(logs, root, monkeypatch):
    """
    `BrokenProcessPool` is a `RuntimeError`, not an `OSError` -- the one that
    the obvious `except OSError` would miss, and the one that happens when
    antivirus or an OOM killer takes a worker.
    """
    from concurrent.futures.process import BrokenProcessPool

    class Dying:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, *a, **k):
            raise BrokenProcessPool("worker vanished")

    monkeypatch.setattr(mod, "ProcessPoolExecutor", Dying)
    monkeypatch.setattr(mod, "PARALLEL_THRESHOLD", 0)

    result = prepare(logs, root=root, workers=4)
    assert result.parallel is False
    for path in logs:
        assert reopen(path, root).status == "hit"


def test_with_the_cache_off_there_is_nothing_to_warm(logs, root):
    result = prepare(logs, root=root, use_cache=False)
    assert [f.status for f in result.files] == ["disabled"] * len(logs)
    assert not root.exists()


def test_an_empty_list_is_not_an_error(root):
    result = prepare([], root=root)
    assert result.files == []
    assert result.summary() == "nothing to index"


def test_an_empty_file_warms_without_writing_a_sidecar(tmp_path, root):
    empty = write(tmp_path / "empty.log", [])
    result = prepare([empty], root=root)
    assert result.files[0].entries == 0
    assert not list(root.glob("*.lwi")) if root.exists() else True


# ---------------------------------------------------------------------------
# deciding whether to use the pool
# ---------------------------------------------------------------------------

def test_a_small_set_is_indexed_in_process(logs, root):
    """
    A spawned interpreter has to import numpy before it does anything useful.
    For a handful of small files that costs more than the work itself.
    """
    result = prepare(logs, root=root, workers=4)
    assert result.parallel is False
    assert result.workers == 1


def test_a_large_set_uses_the_pool(logs, root, monkeypatch):
    monkeypatch.setattr(mod, "PARALLEL_THRESHOLD", 0)
    result = prepare(logs, root=root, workers=2)
    assert result.parallel is True
    assert result.workers == 2
    for path in logs:
        assert reopen(path, root).status == "hit"


def test_the_threshold_is_measured_in_bytes_to_index(logs):
    assert mod.pending_bytes(logs) == sum(p.stat().st_size for p in logs)
    assert mod.pending_bytes([Path("does-not-exist")]) == 0
    assert PARALLEL_THRESHOLD > 0


def test_worker_count_never_exceeds_the_number_of_files(logs):
    assert worker_count(logs) <= len(logs)
    assert worker_count(logs, 1) == 1
    assert worker_count(logs, 99) == 99
    assert worker_count([]) == 1


def test_worker_count_leaves_a_core_for_the_interface():
    """
    NFR-5: the window has to stay responsive while indexing. Saturating every
    core is how a background task makes an application feel broken.
    """
    many = [Path(f"f{i}.log") for i in range(64)]
    import os

    expected = max(1, min(mod.MAX_WORKERS, (os.cpu_count() or 1) - 1))
    assert worker_count(many) == expected


def test_the_largest_file_is_submitted_first(logs, root, monkeypatch):
    """
    With a 10 GB file beside a dozen small ones, any other order leaves one
    worker still grinding long after the rest have gone idle.
    """
    write(logs[1], lines(1, 400))
    submitted: list[str] = []

    class Recording:
        """A pool that records the submission order and runs the work here."""

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn, path, *rest):
            from concurrent.futures import Future

            submitted.append(path)
            future: Future = Future()
            future.set_result(fn(path, *rest))
            return future

    monkeypatch.setattr(mod, "ProcessPoolExecutor", Recording)
    monkeypatch.setattr(mod, "PARALLEL_THRESHOLD", 0)

    result = prepare(logs, root=root, workers=2)
    assert result.parallel is True
    assert submitted[0] == str(logs[1])
    assert submitted == sorted(submitted,
                               key=lambda p: -Path(p).stat().st_size)


# ---------------------------------------------------------------------------
# the one thing a worker must not leave behind
# ---------------------------------------------------------------------------

def test_warming_does_not_leave_a_file_mapped(logs, root):
    """
    A sidecar still mapped cannot be deleted on Windows, which breaks both the
    prune and the user's "clear cache". The worker drops its mappings before
    returning, and inline warming has to do the same.
    """
    prepare(logs, root=root)
    assert sidecar.usage(root)[0] == len(logs)
    sidecar.clear(root)
    assert sidecar.usage(root) == (0, 0)
