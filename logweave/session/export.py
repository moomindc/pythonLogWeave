"""
Exporting the view to a file -- FR-47, SPEC 11.3.

Export differs from copy in two ways and no others: where the text goes, and
that it offers the **whole view** rather than the selection. Filters apply to
both, which falls out for free -- the view is whatever `session.rows` serves,
and under a filter that is the filtered stream.

## Why it is a generator and not a function

A copy is capped (`COPY_ROW_LIMIT`) because a clipboard is one string in
memory. An export is the thing that cap points at, so it must not have one: a
41M-row view has to be writable. That means never holding more than a batch of
rows, and it means the caller has to be able to show progress and take a cancel
(NFR-11).

A generator gives both without a thread. `run()` yields a `Progress` after each
batch; the caller -- `ui.exportrun` -- drives it from a zero-delay timer, so
the event loop runs between batches and the window stays responsive. **That is
deliberately not a worker thread**, and the reason is the one `ui.indexing`
already documents: `session.rows()` mutates the decoded-text LRU and the
merge's block cache, so reading rows off-thread while the view paints is a
data race on every row. A find or a mark scan is safe off-thread because it
touches neither. This is not, so it stays on the UI thread and yields instead.

## What is measured, and what is not

Column widths for the aligned presets come from a **sample** -- the first
`sample_rows` of the export -- because measuring 41M rows before writing the
first would defeat the streaming. `RowFormatter.measure` documents the same
trade from the other side.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .rowformat import Preset, RowFormatter

BATCH_ROWS = 2000
"""
Rows per batch.

Sized by what the caller needs it for: a batch has to be short enough that the
UI does not stall between yields (2000 rows is a few milliseconds) and long
enough that the per-batch overhead -- a `rows()` call and a file write -- is
not what the export spends its time on.
"""

SAMPLE_ROWS = 2000
"""Rows measured for column widths before the first is written."""


def suffix_preset(path: Path | str) -> Preset:
    """
    The preset a filename implies, for the export dialog's default.

    A `.csv` that came out as aligned text would be a file the user has to
    throw away, and the extension is the clearest statement of intent
    available.
    """
    ext = Path(path).suffix.lower()
    return {".csv": Preset.CSV, ".md": Preset.MARKDOWN,
            ".markdown": Preset.MARKDOWN}.get(ext, Preset.ANNOTATED)


@dataclass
class ExportSpec:
    """What to write, where, and in what form."""

    path: Path
    first_row: int = 0
    rows: int | None = None
    """How many rows, or None for "to the end of the view"."""
    encoding: str = "utf-8"
    newline: str = "\n"
    """
    Written explicitly rather than left to the platform.

    `\\n` by default even on Windows: these files are read by greps, diffs and
    other tools far more often than by Notepad, and a user who wants CRLF can
    ask for it. The dialog offers both.
    """
    batch_rows: int = BATCH_ROWS
    sample_rows: int = SAMPLE_ROWS


@dataclass
class Progress:
    """Where an export has got to. Yielded once per batch."""

    written: int = 0
    total: int = 0
    bytes_written: int = 0
    done: bool = False
    cancelled: bool = False
    error: str = ""
    notes: tuple[str, ...] = ()
    """Citation warnings from `RowFormatter.notes` (PRD risk R10)."""

    @property
    def fraction(self) -> float:
        return min(1.0, self.written / self.total) if self.total else 1.0


def run(session, formatter: RowFormatter, spec: ExportSpec, *, cancelled=None):
    """
    Write the view to `spec.path`, yielding a `Progress` after every batch.

    Exhaust it to export; stop iterating to abandon it. The file is opened
    before the first yield so that an unwritable path is reported at once
    rather than after a 10M-row wait, and it is closed however the generator
    ends -- including a caller that simply drops it, which is what a cancel is.
    """
    total = session.total_rows
    first = max(0, int(spec.first_row))
    want = total - first if spec.rows is None else max(0, int(spec.rows))
    want = max(0, min(want, total - first))

    path = Path(spec.path)
    notes: tuple[str, ...] = ()
    try:
        handle = path.open("w", encoding=spec.encoding, newline="",
                           errors="replace")
    except OSError as exc:
        yield Progress(total=want, done=True, error=str(exc))
        return

    written = 0
    size = 0
    """
    Bytes on disk, remembered inside the `with` rather than read at the end.

    `handle.tell()` is only answerable while the file is open, and the final
    `Progress` is yielded after it has closed -- so reading it there gave every
    finished export a size of zero, which is a report that looks like a result.
    """
    try:
        with handle:
            widths = None
            if want:
                sample = session.rows(first, min(spec.sample_rows, want))
                widths = formatter.measure(sample)
                notes = tuple(formatter.notes(sample))
            head = formatter.header(widths)
            if head:
                handle.write(head + spec.newline)
            size = handle.tell()
            yield Progress(written=0, total=want,
                           bytes_written=size, notes=notes)

            while written < want:
                if cancelled is not None and cancelled():
                    yield Progress(written=written, total=want,
                                   bytes_written=size,
                                   cancelled=True, done=True, notes=notes)
                    return
                batch = session.rows(first + written,
                                     min(spec.batch_rows, want - written))
                if not batch:
                    # The view shrank under the export -- a source closed, a
                    # filter narrowed. Stopping here is honest; padding or
                    # raising would not be.
                    break
                text = formatter.chunk(batch, widths)
                handle.write(text + spec.newline)
                written += len(batch)
                size = handle.tell()
                yield Progress(written=written, total=want,
                               bytes_written=size, notes=notes)
    except OSError as exc:
        # A full disk or a vanished share, mid-write. NFR-21: report it and
        # leave a usable session; the partial file is left where it is, because
        # deleting what the user asked for is not this function's call.
        yield Progress(written=written, total=want, bytes_written=size,
                       done=True, error=str(exc), notes=notes)
        return

    yield Progress(written=written, total=want, bytes_written=size,
                   done=True, notes=notes)


def write_now(session, formatter: RowFormatter, spec: ExportSpec) -> Progress:
    """
    Run an export to completion. For tests, scripts, and a future headless
    mode -- anything with no event loop to be polite to.
    """
    last = Progress()
    for step in run(session, formatter, spec):
        last = step
    return last
