"""
The four copy presets M7 adds, and the streamed export -- FR-46b, FR-47.

`test_rowformat.py` covers `annotated` and `plain` from M3/M4; this file covers
`aligned`, `markdown`, `csv` and `template`, the stale-citation mark (risk R10),
and the export that FR-47 points the copy row cap at.
"""

from __future__ import annotations

import csv
import io

import pytest

from logweave.session import Session
from logweave.session.export import (
    ExportSpec,
    Progress,
    run,
    suffix_preset,
    write_now,
)
from logweave.session.rowformat import (
    DEFAULT_TEMPLATE,
    Columns,
    Preset,
    RowFormatter,
    SourceNaming,
    validate_template,
)


def write_log(path, rows):
    with path.open("w", encoding="utf-8") as fh:
        for sec, level, text in rows:
            h, rem = 10 + sec // 3600, sec % 3600
            fh.write(f"2026-10-04 {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
                     f".000000 {level} {text}\n")
    return path


@pytest.fixture
def session(tmp_path):
    # Eight rows and not three: the timestamp layout -- and with it the
    # message offset every preset trims by (SPEC 9.5) -- is only proven once
    # the fast path engages, which wants a handful of lines to agree.
    a = write_log(tmp_path / "app.log", [
        (0, "INFO", "connection pool exhausted"),
        (2, "ERROR", 'pipe | and "quotes" and, commas'),
        (4, "WARN", "retry 1/3"),
        (6, "INFO", "flushed 240 records"),
        (8, "DEBUG", "worker 7 started"),
        (10, "INFO", "checkpoint written"),
        (12, "INFO", "heartbeat ack in 4ms"),
        (14, "NOTICE", "pool resized 32 -> 64"),
    ])
    b = write_log(tmp_path / "soa.log",
                  [(1 + 2 * i, "INFO", f"upstream ok {i}") for i in range(8)])
    s = Session(use_cache=False)
    s.add_files([a, b])
    yield s
    s.close()


def fmt(session, **kw) -> RowFormatter:
    return RowFormatter(session, **kw)


# ---------------------------------------------------------------------------
# aligned
# ---------------------------------------------------------------------------

def test_aligned_shows_the_columns_the_view_shows(session):
    rows = session.rows(0, 8)
    only_time = fmt(session, preset=Preset.ALIGNED).format(rows)
    assert only_time.splitlines()[0].startswith("2026-10-04 10:00:00")
    assert "app" not in only_time.splitlines()[0]

    with_all = fmt(session, preset=Preset.ALIGNED,
                   columns=Columns(timestamp=True, alias=True, line_no=True)
                   ).format(rows)
    first = with_all.splitlines()[0]
    assert first.index("1") < first.index("app") < first.index("2026")


def test_aligned_pads_to_the_widest_value_in_the_selection(session):
    rows = session.rows(0, 8)
    text = fmt(session, preset=Preset.ALIGNED,
               columns=Columns(alias=True)).format(rows)
    starts = {line.index("2026") for line in text.splitlines()}
    assert len(starts) == 1, "the timestamp column has to line up"


def test_aligned_keeps_the_message_without_a_duplicated_timestamp(session):
    rows = session.rows(0, 1)
    text = fmt(session, preset=Preset.ALIGNED).format(rows)
    assert text.count("2026-10-04") == 1


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------

def test_markdown_has_a_header_and_a_row_each(session):
    rows = session.rows(0, 8)
    lines = fmt(session, preset=Preset.MARKDOWN).format(rows).splitlines()
    assert lines[0].startswith("| Source | Line | Time | Severity | Message |")
    assert set(lines[1]) <= set("|-")
    assert len(lines) == 2 + len(rows)
    assert lines[2].startswith("| app.log | 1 |")


def test_markdown_escapes_a_pipe_and_breaks_a_line(tmp_path):
    path = write_log(tmp_path / "multi.log",
                     [(i, "INFO", f"ordinary {i}") for i in range(8)])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("2026-10-04 10:01:00.000000 ERROR first | piped\n"
                 "    continued on a second line\n")
    s = Session(use_cache=False)
    s.add_files([path])
    body = fmt(s, preset=Preset.MARKDOWN).format(s.rows(8, 1)).splitlines()[2]
    delimiters = body.replace("\\|", "").count("|")
    assert delimiters == 6, "the escaped pipe must not open a cell"
    assert "\\|" in body and "<br>" in body
    s.close()


# ---------------------------------------------------------------------------
# csv
# ---------------------------------------------------------------------------

def test_csv_round_trips_through_a_reader(session):
    rows = session.rows(0, 8)
    text = fmt(session, preset=Preset.CSV).format(rows)
    got = list(csv.reader(io.StringIO(text)))
    assert got[0] == ["Source", "Line", "Time", "Severity", "Message"]
    assert len(got) == 1 + len(rows)
    # The row with a comma and a quote in it is the point: a hand-made CSV
    # breaks here and a spreadsheet reads it wrongly in silence.
    awkward = next(r for r in got if "commas" in r[4])
    # The level word stays in the message, as it does in the view's message
    # column: only the repeated *timestamp* is trimmed (SPEC 9.5). The Severity
    # column is the parsed class, which is not always the word on the line.
    assert awkward[4] == 'ERROR pipe | and "quotes" and, commas'
    assert awkward[3] == "ERROR"


def test_csv_keeps_a_multiline_entry_in_one_field(tmp_path):
    path = write_log(tmp_path / "multi.log",
                     [(i, "INFO", f"ordinary {i}") for i in range(8)])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("2026-10-04 10:01:00.000000 ERROR boom\n"
                 "    at frame one\n    at frame two\n")
    s = Session(use_cache=False)
    s.add_files([path])
    text = fmt(s, preset=Preset.CSV).format(s.rows(8, 1))
    got = list(csv.reader(io.StringIO(text)))
    assert len(got) == 2, "the continuation lines stay inside the quoted field"
    assert got[1][4].count("\n") == 2
    s.close()


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------

def test_the_default_template_matches_the_annotated_shape(session):
    rows = session.rows(0, 1)
    text = fmt(session, preset=Preset.TEMPLATE).format(rows)
    assert text.startswith("app.log:1  2026-10-04 10:00:00")


def test_every_documented_field_is_available(session):
    rows = session.rows(0, 1)
    every = ("{source}|{alias}|{path}|{line}|{timestamp}|{severity}|"
             "{message}|{row}")
    text = fmt(session, preset=Preset.TEMPLATE, template=every).format(rows)
    parts = text.split("|")
    assert parts[0] == "app.log" and parts[1] == "app"
    assert parts[2].endswith("app.log")
    assert parts[3] == "1" and parts[5] == "INFO" and parts[7] == "0"


def test_a_bad_template_is_reported_rather_than_raised():
    assert validate_template(DEFAULT_TEMPLATE) == ""
    assert "unknown field" in validate_template("{nope}")
    assert validate_template("")
    assert validate_template("{unclosed")


# ---------------------------------------------------------------------------
# naming and the stale mark (risk R10)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("naming,expected", [
    (SourceNaming.BASENAME, "app.log"),
    (SourceNaming.ALIAS, "app"),
])
def test_source_naming_applies_to_every_preset(session, naming, expected):
    rows = session.rows(0, 1)
    for preset in (Preset.ANNOTATED, Preset.MARKDOWN, Preset.CSV,
                   Preset.TEMPLATE):
        text = fmt(session, preset=preset, naming=naming).format(rows)
        assert expected in text, preset


def test_a_rotated_source_has_its_citations_marked(session):
    """
    Risk R10. The rows are still readable, but `app.log:7` now points at
    whatever took the path over, so the number is marked rather than printed as
    if it resolved.
    """
    session.sources[0].citation_note = "rotated; this is the previous file"
    rows = session.rows(0, 8)
    for preset in (Preset.ANNOTATED, Preset.MARKDOWN, Preset.CSV,
                   Preset.TEMPLATE):
        formatter = fmt(session, preset=preset)
        text = formatter.format(rows)
        assert "1?" in text, preset
        assert formatter.notes(rows) == [
            "app.log: rotated; this is the previous file"]
    # The source that is fine is not marked.
    assert fmt(session, naming=SourceNaming.ALIAS).notes(
        [r for r in rows if r.source == 1]) == []


# ---------------------------------------------------------------------------
# export (FR-47)
# ---------------------------------------------------------------------------

def test_export_writes_the_whole_view(session, tmp_path):
    out = tmp_path / "all.txt"
    last = write_now(session, fmt(session), ExportSpec(path=out))
    assert last.done and not last.error and last.written == session.total_rows
    assert len(out.read_text(encoding="utf-8").splitlines()) == session.total_rows


def test_export_respects_a_filter(session, tmp_path):
    from logweave.session import filters

    session.set_filter(filters.apply(session, filters.Filter(
        sources=frozenset({1}))))
    out = tmp_path / "filtered.txt"
    write_now(session, fmt(session), ExportSpec(path=out))
    body = out.read_text(encoding="utf-8")
    assert "soa.log" in body and "app.log" not in body


def test_export_can_write_a_range(session, tmp_path):
    out = tmp_path / "range.txt"
    write_now(session, fmt(session), ExportSpec(path=out, first_row=1, rows=2))
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2


def test_export_yields_progress_per_batch(session, tmp_path):
    out = tmp_path / "progress.txt"
    seen = [p.written for p in run(session, fmt(session),
                                   ExportSpec(path=out, batch_rows=1))]
    total = session.total_rows
    assert seen == [0] + list(range(1, total + 1)) + [total]
    assert seen == sorted(seen)


def test_abandoning_the_generator_closes_the_file(session, tmp_path):
    """
    A cancel is a caller that stops iterating. The file has to be closed
    anyway, or on Windows the next export to the same path fails.
    """
    out = tmp_path / "cancelled.txt"
    gen = run(session, fmt(session), ExportSpec(path=out, batch_rows=1))
    next(gen)
    next(gen)
    gen.close()
    out.unlink()        # would raise PermissionError on Windows if still open


def test_a_cancel_callback_stops_and_says_so(session, tmp_path):
    out = tmp_path / "stopped.txt"
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    last = Progress()
    for step in run(session, fmt(session), ExportSpec(path=out, batch_rows=1),
                    cancelled=cancel):
        last = step
    assert last.cancelled and last.done and last.written < session.total_rows


def test_an_unwritable_path_is_reported_rather_than_raised(session, tmp_path):
    last = write_now(session, fmt(session),
                     ExportSpec(path=tmp_path / "no-such-dir" / "x.txt"))
    assert last.done and last.error and last.written == 0


def test_a_header_preset_writes_its_header_once(session, tmp_path):
    out = tmp_path / "out.csv"
    write_now(session, fmt(session, preset=Preset.CSV),
              ExportSpec(path=out, batch_rows=1))
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("Source,Line,Time")
    assert len([ln for ln in lines if ln.startswith("Source,")]) == 1
    assert len(list(csv.reader(io.StringIO("\n".join(lines))))) == \
        1 + session.total_rows


def test_the_streamed_columns_are_measured_once(session, tmp_path):
    """
    Every batch must align the same way. Measuring per batch would make a
    40,000-row export change its column width part way down.
    """
    out = tmp_path / "aligned.txt"
    write_now(session, fmt(session, preset=Preset.ANNOTATED),
              ExportSpec(path=out, batch_rows=1))
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln]
    assert len({ln.index("2026") for ln in lines}) == 1


def test_export_of_an_empty_view_writes_a_header_and_nothing_else(tmp_path):
    s = Session(use_cache=False)
    out = tmp_path / "empty.csv"
    last = write_now(s, RowFormatter(s, preset=Preset.CSV),
                     ExportSpec(path=out))
    assert last.done and last.written == 0
    assert out.read_text(encoding="utf-8").strip().startswith("Source,")
    s.close()


@pytest.mark.parametrize("name,preset", [
    ("x.csv", Preset.CSV), ("x.md", Preset.MARKDOWN),
    ("x.txt", Preset.ANNOTATED), ("x", Preset.ANNOTATED),
])
def test_the_suffix_picks_the_preset(name, preset):
    assert suffix_preset(name) is preset


def test_the_finished_report_says_how_big_the_file_is(session, tmp_path):
    """
    `bytes_written` has to survive the last yield.

    It was read from `handle.tell()`, which is only answerable while the file
    is open -- and the final `Progress` is yielded after the `with` has closed
    it, so every completed export reported a size of zero. A report that looks
    like a result is worse than no report, so the size is remembered as the
    batches go.
    """
    out = tmp_path / "sized.log"
    last = write_now(session, fmt(session), ExportSpec(path=out,
                                                       batch_rows=2))
    assert last.done and last.written > 0
    assert last.bytes_written == out.stat().st_size


def test_a_cancelled_export_reports_the_bytes_it_had_written(session, tmp_path):
    out = tmp_path / "part.log"
    steps = run(session, fmt(session), ExportSpec(path=out, batch_rows=1),
                cancelled=lambda: True)
    last = None
    for step in steps:
        last = step
    assert last.cancelled and last.bytes_written == out.stat().st_size
