"""
Warming the index cache before a session opens -- SPEC 5.3, NFR-3, NFR-5.

A first index runs at tens of MB per second, so a multi-gigabyte set takes
minutes. Done on the UI thread that is a frozen window; done on a background
thread in the same process the GIL makes it slower than doing nothing else.
Both are solved by the same observation: once a sidecar exists, opening a file
costs a memory map and nothing else (SPEC 5.4).

So this module does not open anything. Its one job is to **make sure a sidecar
exists** for each path, in worker processes, and then get out of the way. The
session that follows opens the same files on the calling thread and gets a
cache hit for every one -- about 7 ms a file regardless of size.

That split is what keeps the session single-threaded. A background thread
mutating the live `Session` while the view paints from it is a data race on
every array; here no object is shared with anyone. The workers touch the
filesystem and the cache directory, return a few hundred bytes of summary, and
exit. SPEC 5.3's rule holds literally: bulk data never crosses a process
boundary.

What this is not: within-file parallelism. A chunk cannot be indexed without
the carry state from the chunk before it -- the open multi-line entry, the
inferred year, the rollover count -- so splitting one file means either
speculating about the carry or stitching it afterwards. That is SPEC 5.3's
second phase, and it is not needed to stop the window freezing.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

PARALLEL_THRESHOLD = 32 * 1024 * 1024
"""
Total unindexed bytes below which the pool is not worth starting.

A spawned interpreter has to import numpy before it can do anything, which on
Windows is a few hundred milliseconds. Below this much work the pool loses to
indexing in-process, and a dropped handful of small files is the common case.
"""

MAX_WORKERS = 8
"""
More workers than this stops helping: indexing is memory-bandwidth-bound once
a few cores are reading, and each worker holds a chunk buffer.
"""


@dataclass
class Prepared:
    """What warming did to one file."""

    path: str
    status: str = "skipped"
    """`hit`, `append`, `miss`, `disabled`, `skipped` or `failed`."""
    reason: str = ""
    indexed_bytes: int = 0
    entries: int = 0


@dataclass
class PrepareResult:
    files: list[Prepared] = field(default_factory=list)
    parallel: bool = False
    workers: int = 0
    cancelled: bool = False

    @property
    def indexed_bytes(self) -> int:
        return sum(f.indexed_bytes for f in self.files)

    def summary(self) -> str:
        if not self.files:
            return "nothing to index"
        fresh = [f for f in self.files if f.status in ("miss", "append")]
        how = f"{self.workers} workers" if self.parallel else "in process"
        if not fresh:
            return f"{len(self.files)} files already indexed"
        return (f"indexed {self.indexed_bytes / 1e6:.1f} MB "
                f"across {len(fresh)} files, {how}")


Progress = Callable[[int, int, Prepared], None]
"""Called after each file with (done, total, result). Must be cheap."""


def pending_bytes(paths: Iterable[Path]) -> int:
    """
    Roughly how much there is to index. Used only to decide about the pool.

    A file with an exact sidecar still counts here. Checking properly would
    mean a lookup per file, which is most of the cost of just opening it, and
    being wrong only means starting a pool that finishes immediately.
    """
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total


def worker_count(paths: list[Path], workers: int | None = None) -> int:
    if workers is not None:
        return max(1, workers)
    cpus = os.cpu_count() or 1
    return max(1, min(MAX_WORKERS, cpus - 1, len(paths)))


def warm_one(path: str, mtime_wall_us: int, root: str | None,
             force_format: str = "") -> Prepared:
    """
    Make sure a sidecar exists for one file. Runs in a worker process.

    Module-level and taking only primitives, because Windows spawns rather
    than forks: everything an argument touches has to pickle, and a `LogSource`
    holds a file handle.

    The index built here is deliberately thrown away. It lives in the wrong
    process, and the parent can map the sidecar for almost nothing.

    `force_format` is carried through because a sidecar is only reusable by a
    reader asking for the same format (FR-13, SPEC 5.4). Warming without it
    would build an index of the detected reading, which the session then
    refuses and rebuilds -- the file indexed twice, and the slower time is the
    one the reader waits for.
    """
    from ..index.cached import open_index
    from ..index.civil import epoch_us_to_wall_us
    from ..source.file import FileSource

    p = Path(path)
    try:
        if mtime_wall_us == 0:
            mtime_wall_us = epoch_us_to_wall_us(int(p.stat().st_mtime * 1_000_000))
        src = FileSource(p)
        src.open()
        try:
            out = open_index(src, mtime_wall_us=mtime_wall_us, path=p,
                             root=Path(root) if root else None,
                             force_format=force_format)
            got = Prepared(path=path, status=out.status, reason=out.reason,
                           indexed_bytes=out.indexed_bytes, entries=out.index.n)
            # Drop the mappings before the process exits: on Windows an open
            # mapping is what stops a superseded sidecar being pruned, and the
            # parent is about to map the same file.
            out.index.release()
            return got
        finally:
            src.close()
    except Exception as exc:                        # noqa: BLE001 - see below
        # Any failure here is a lost speedup, never a lost file: the session
        # opens the same path afterwards and will report a real error then,
        # with the context to say what the user should do about it. Letting an
        # exception out of a worker would instead take down the whole batch.
        return Prepared(path=path, status="failed", reason=str(exc))


def prepare(
    paths: Iterable[Path | str],
    *,
    root: Path | None = None,
    use_cache: bool = True,
    workers: int | None = None,
    progress: Progress | None = None,
    cancelled: Callable[[], bool] | None = None,
    formats: dict[str, str] | None = None,
) -> PrepareResult:
    """
    Ensure every path has a sidecar, in parallel where that pays.

    `cancelled` is consulted between files. Stopping mid-file would mean
    abandoning a partial index, and a file indexes in seconds even when the set
    takes minutes, so the granularity is honest rather than ideal.

    `formats` maps a path to a forced timestamp format (FR-13), for the files
    a profile has an opinion about. Keyed by the string of the path because it
    crosses a process boundary.

    With the cache off there is nothing to warm and this returns immediately --
    the session will index on the calling thread, which is the documented cost
    of running without a cache.
    """
    todo = [Path(p) for p in paths]
    result = PrepareResult()
    if not todo or not use_cache:
        result.files = [Prepared(path=str(p), status="disabled") for p in todo]
        return result

    n = worker_count(todo, workers)
    result.parallel = n > 1 and pending_bytes(todo) >= PARALLEL_THRESHOLD
    result.workers = n if result.parallel else 1

    if result.parallel:
        _run_pooled(todo, result, root=root, progress=progress,
                    cancelled=cancelled, formats=formats or {})
    else:
        _run_inline(todo, result, root=root, progress=progress,
                    cancelled=cancelled, formats=formats or {})
    return result


def _run_inline(todo: list[Path], result: PrepareResult, *, root: Path | None,
                progress: Progress | None,
                cancelled: Callable[[], bool] | None,
                formats: dict[str, str]) -> None:
    for done, path in enumerate(todo, 1):
        if cancelled is not None and cancelled():
            result.cancelled = True
            return
        got = warm_one(str(path), 0, str(root) if root else None,
                       formats.get(str(path), ""))
        result.files.append(got)
        if progress is not None:
            progress(done, len(todo), got)


def _run_pooled(todo: list[Path], result: PrepareResult, *, root: Path | None,
                progress: Progress | None,
                cancelled: Callable[[], bool] | None,
                formats: dict[str, str]) -> None:
    """
    One file per worker, reported as each finishes.

    Submitted in descending size order so the longest job starts first: with a
    10 GB file beside a dozen small ones, any other order leaves one worker
    still running long after the rest are idle.
    """
    ordered = sorted(todo, key=lambda p: -_size(p))
    root_s = str(root) if root else None
    try:
        with ProcessPoolExecutor(max_workers=result.workers) as pool:
            futures = {pool.submit(warm_one, str(p), 0, root_s,
                                   formats.get(str(p), "")): p
                       for p in ordered}
            done = 0
            for future in as_completed(futures):
                if cancelled is not None and cancelled():
                    result.cancelled = True
                    for f in futures:
                        f.cancel()
                    break
                done += 1
                got = future.result()
                result.files.append(got)
                if progress is not None:
                    progress(done, len(ordered), got)
    except (OSError, RuntimeError) as exc:
        # No usable pool. `RuntimeError` covers both a worker dying mid-flight
        # (`BrokenProcessPool`) and a caller whose entry point lacks the
        # `if __name__ == "__main__"` guard that spawning requires; `OSError`
        # covers a sandbox, an exhausted handle table, or a frozen build
        # without multiprocessing. Indexing in process is slower, not broken,
        # and anything already warmed is now a cache hit.
        result.parallel = False
        result.workers = 1
        result.files.clear()
        _run_inline(todo, result, root=root, progress=progress,
                    cancelled=cancelled, formats=formats)
        result.files.append(Prepared(path="", status="skipped",
                                     reason=f"no worker pool: {exc}"))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
