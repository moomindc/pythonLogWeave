"""Shared fixtures. Generates a fresh corpus per test session."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gen_corpus  # noqa: E402

from logweave.index.civil import epoch_us_to_wall_us  # noqa: E402
from logweave.index.indexer import build_index  # noqa: E402
from logweave.source import encoding  # noqa: E402
from logweave.source.base import (  # noqa: E402
    LogSource,
    OpenReport,
    SourceId,
    SourceIdentity,
    SourceMeta,
)


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    """
    Generate the small scenarios into a session temp directory.

    Always generated rather than reused from tests/corpus, so a stale or
    hand-edited corpus can never make a test pass. Also guarantees the zone-aware
    ground truth (see gen_corpus docstring) was produced on this machine.
    """
    out = tmp_path_factory.mktemp("corpus")
    rc = gen_corpus.main(["--out", str(out), "--only", "basic,formats,edge"])
    assert rc == 0, "corpus generation failed"
    return out


@pytest.fixture(scope="session")
def manifests(corpus: Path) -> dict:
    return {
        p.parent.name: json.loads(p.read_text(encoding="utf-8"))
        for p in corpus.glob("*/manifest.json")
    }


def files_of(manifest: dict) -> dict:
    return {f["name"]: f for f in manifest["files"]}


class BytesSource(LogSource):
    """
    A `LogSource` backed by a file read wholly into memory.

    Exists so the index layer can be tested on its own, without waiting on the
    real file source, and so index tests are not also testing Windows file
    handles. It is deliberately the dumbest possible conforming implementation:
    if an index test fails against this, the index layer is at fault.

    It does resolve its encoding, because that is not incidental to the index
    layer: the byte-order mark decides where the first entry starts, and a
    UTF-16 file indexes nothing at all unless the scan is narrowed (SPEC 3.3).
    Both come straight from `logweave.source.encoding`, so the stub agrees with
    the real `FileSource` rather than approximating it.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data = self.path.read_bytes()
        self.id = SourceId(self.path.name)
        self.encoding_info = encoding.detect(self._data[: encoding.SCAN_BYTES])
        self.meta = SourceMeta(
            display_name=self.path.name, encoding=self.encoding_info.name
        )

    def open(self) -> OpenReport:
        return OpenReport()

    def size(self) -> int:
        return len(self._data)

    def read_range(self, start: int, end: int) -> bytes:
        return self._data[max(0, start):max(0, end)]

    def identity(self) -> SourceIdentity:
        return SourceIdentity(size=len(self._data))

    def data_start(self) -> int:
        return self.encoding_info.bom_len

    @property
    def mtime_wall_us(self) -> int:
        """
        The file's mtime as naive-local `wall_us`, which is what the indexer
        wants (D1). `st_mtime` is a UTC epoch, so it must be converted -- the
        corpus generator sets each file's mtime to its last entry, so this is
        the anchor SPEC 4.5 infers yearless and dateless formats from.
        """
        return epoch_us_to_wall_us(int(self.path.stat().st_mtime * 1_000_000))


def index_of(path: Path, **kwargs):
    """Index one corpus file through the real pipeline."""
    src = BytesSource(path)
    return build_index(src, mtime_wall_us=src.mtime_wall_us, **kwargs)
