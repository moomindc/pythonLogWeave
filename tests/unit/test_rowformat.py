"""
Copy and export formatting -- D9, FR-46a, SPEC 11.3.

The user-facing promise is that a pasted row stays traceable: `app.log:1`,
`SOA.log:7`. These tests hold that exact shape, because it is the part a human
reads in a ticket and the part that silently rots if the formatter is changed
carelessly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from logweave.session import Preset, RowFormatter, Session, SourceNaming


@pytest.fixture
def session(corpus: Path):
    s = Session()
    s.add_files(sorted((corpus / "basic").glob("*.log")))
    yield s
    s.close()


TAG = re.compile(r"^(?P<name>\S+?):(?P<line>\d+)\s{2,}(?P<stamp>\d{4}-\d{2}-\d{2}[^ ]*( [^ ]+)?)  ")


def annotated(text: str) -> list[re.Match]:
    """The lines that carry an annotation -- i.e. entry first-lines."""
    return [m for m in (TAG.match(line) for line in text.splitlines()) if m]


def test_annotated_rows_carry_file_and_line(session):
    rows = session.rows(0, 6)
    text = RowFormatter(session).format(rows)
    assert len(annotated(text)) == len(rows)


def test_attribution_names_the_basename_by_default(session):
    """
    SPEC 11.3: basename, because it is what someone reading a ticket needs in
    order to go and find the file.
    """
    text = RowFormatter(session).format(session.rows(0, 30))
    cited = {m["name"] for m in annotated(text)}
    assert cited <= {"app.log", "iis.log", "SOA.log"}
    assert len(cited) > 1, "a merged selection should cite more than one file"


def test_line_numbers_match_the_rows_they_came_from(session):
    rows = session.rows(0, 20)
    text = RowFormatter(session).format(rows)
    for row, m in zip(rows, annotated(text), strict=True):
        assert int(m["line"]) == row.line_no


def test_attribution_resolves_against_the_real_file(session, corpus):
    """The promise, checked end to end: open the cited file at the cited line."""
    rows = session.rows(200, 25)
    text = RowFormatter(session).format(rows)
    lines = text.splitlines()
    for m in annotated(text):
        on_disk = (corpus / "basic" / m["name"]).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[int(m["line"]) - 1]
        rendered = next(ln for ln in lines if ln.startswith(m[0][: m.end("stamp")]))
        message = rendered[m.end():]
        # A suffix, not an equality: the formatter prints its own timestamp
        # column, so a timestamp that led the line is trimmed out of the message
        # to avoid printing it twice (SPEC 9.5). What must still hold is that
        # the message is the tail of the cited line, untouched.
        assert on_disk.endswith(message), f"{m['name']}:{m['line']}"


def test_columns_are_padded_to_the_selection_not_the_log(session):
    """
    SPEC 11.3: a 40-row copy must not be padded to the widest name in a 41M-row
    file. A selection from one source therefore pads to that one tag width.
    """
    rows = [r for r in session.rows(0, 400) if r.source == 0][:5]
    text = RowFormatter(session).format(rows)
    widths = {len(line.split("  ")[0]) for line in text.splitlines()}
    assert len(widths) == 1
    assert max(widths) == max(
        len(f"{RowFormatter(session).source_name(r.source)}:{r.line_no}") for r in rows
    )


def test_alias_naming_uses_the_alias(session):
    f = RowFormatter(session, naming=SourceNaming.ALIAS)
    text = f.format(session.rows(0, 10))
    aliases = {s.alias for s in session.sources}
    assert {m["name"] for m in annotated(text)} <= aliases


def test_path_naming_gives_something_openable(session):
    f = RowFormatter(session, naming=SourceNaming.PATH)
    first = f.format(session.rows(0, 1)).splitlines()[0]
    cited = first.rsplit(":", 1)[0] if first[1:3] != ":\\" else first
    # Just enough to show it is a path and not a bare name.
    assert "basic" in first
    assert Path(cited.split("  ")[0].rsplit(":", 1)[0]).name.endswith(".log")


def test_plain_is_message_text_only(session):
    rows = session.rows(0, 8)
    text = RowFormatter(session, preset=Preset.PLAIN).format(rows)
    assert text == "\n".join(r.text for r in rows)


def test_attribution_can_be_turned_off_while_keeping_the_timestamp(session):
    rows = session.rows(0, 5)
    text = RowFormatter(session, attribute=False).format(rows)
    stamped = [ln for ln in text.splitlines() if ln[:1].isdigit()]
    assert len(stamped) == len(rows)
    for line in stamped:
        assert re.match(r"^\d{4}-\d{2}-\d{2} ", line), line


def test_whole_seconds_can_be_requested(session):
    text = RowFormatter(session, micros=False).format(session.rows(0, 3))
    for m in annotated(text):
        assert "." not in m["stamp"], m["stamp"]


def test_formatting_nothing_gives_nothing(session):
    assert RowFormatter(session).format([]) == ""


def test_one_row_needs_no_padding_beyond_its_own_tag(session):
    rows = [r for r in session.rows(0, 60) if not r.multiline][:1]
    line = RowFormatter(session).format(rows)
    assert "\n" not in line
    assert line.startswith(
        f"{RowFormatter(session).source_name(rows[0].source)}:{rows[0].line_no}"
    )


# ---------------------------------------------------------------------------
# the repeated leading timestamp -- SPEC 9.5
# ---------------------------------------------------------------------------

def leading_ts_row(session):
    """A row from a format whose timestamp leads the line (app, SOA)."""
    return next(r for r in session.rows(0, 200) if r.msg_from and not r.multiline)


def anchored_row(session):
    """A row from a format whose timestamp sits inside the line (iis)."""
    return next(r for r in session.rows(0, 200) if not r.msg_from)


def test_a_leading_timestamp_is_trimmed_out_of_the_message(session):
    """
    Otherwise `annotated` prints it twice: once in its own column and again at
    the head of the message.
    """
    r = leading_ts_row(session)
    line = RowFormatter(session).format([r])
    assert line.count("2026-10-04") == 1, line


def test_trimming_removes_a_prefix_and_nothing_else(session):
    r = leading_ts_row(session)
    assert r.text.endswith(r.message)
    assert len(r.text) - len(r.message) == r.msg_from
    assert not r.message.startswith(" ")


def test_a_timestamp_inside_the_line_is_left_alone(session):
    """
    An access log's leading field is the client address, not a repetition of
    the timestamp. Trimming it would throw real information away.
    """
    r = anchored_row(session)
    assert r.msg_from == 0
    assert r.message == r.text
    assert RowFormatter(session).format([r]).endswith(r.text)


def test_trimming_can_be_turned_off(session):
    r = leading_ts_row(session)
    line = RowFormatter(session, trim_timestamp=False).format([r])
    assert line.endswith(r.text)
    assert line.count("2026-10-04") == 2


def test_plain_is_always_verbatim(session):
    """`plain` prints no timestamp of its own, so it has nothing to de-duplicate."""
    r = leading_ts_row(session)
    assert RowFormatter(session, preset=Preset.PLAIN).format([r]) == r.text


# ---------------------------------------------------------------------------
# multi-line entries -- SPEC 11.3
# ---------------------------------------------------------------------------

def multiline_row(session):
    for r in session.rows(0, session.total_rows):
        if r.multiline and len(r.text.splitlines()) > 2:
            return r
    raise AssertionError("the corpus should contain a multi-line entry")


def test_a_multiline_entry_is_annotated_once_not_per_line(session):
    """
    SPEC 11.3. Annotating every frame of a stack trace makes the paste
    unreadable, which defeats the point of copying it.
    """
    r = multiline_row(session)
    text = RowFormatter(session).format([r])
    assert len(text.splitlines()) == len(r.text.splitlines())
    marks = annotated(text)
    assert len(marks) == 1
    assert int(marks[0]["line"]) == r.line_no


def test_continuation_lines_are_indented_to_the_message_column(session):
    r = multiline_row(session)
    original = r.text.splitlines()
    lines = RowFormatter(session).format([r]).splitlines()
    msg_col = lines[0].index(original[0])
    for line, part in zip(lines[1:], original[1:], strict=True):
        assert line == " " * msg_col + part


def test_every_physical_line_can_be_annotated_with_incrementing_numbers(session):
    """The setting SPEC 11.3 provides for tools that parse file:line references."""
    r = multiline_row(session)
    text = RowFormatter(session, annotate_every_line=True).format([r])
    marks = annotated(text)
    assert len(marks) == len(r.text.splitlines())
    assert [int(m["line"]) for m in marks] == list(
        range(r.line_no, r.line_no + len(marks))
    )


def test_a_multiline_entry_mixed_with_single_lines_keeps_both_readable(session):
    rows = session.rows(0, 60)
    assert any(r.multiline for r in rows), "need a multi-line entry in range"
    text = RowFormatter(session).format(rows)
    assert len(annotated(text)) == len(rows)
    assert len(text.splitlines()) == sum(
        len(r.text.splitlines()) or 1 for r in rows
    )
