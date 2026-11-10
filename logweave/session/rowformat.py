"""
Row formatting for copy and export -- D9, FR-46a, FR-46b, FR-47, SPEC 11.3.

One interface serves the clipboard and a file export, so there is exactly one
implementation of attribution, as SPEC 11.3 requires. M3 and M4 built
`annotated` and `plain` because a log viewer you cannot copy a line out of is
not a usable build; M7 adds the other four and the stale-citation marking that
PRD risk R10 asks for.

Qt-free, like everything at layer 4 -- which is what lets the same code serve
the clipboard, a streamed export and (one day) an HTML report without any of
them knowing about the others.

## The six presets, and what each is *for*

| Preset | For |
|---|---|
| `annotated` | pasting into a ticket: every row cites its file and line |
| `aligned` | pasting into a message where the columns should look as they did |
| `plain` | feeding a parser, a diff, or a search box |
| `markdown` | a pull request or a wiki page |
| `csv` | a spreadsheet or another tool |
| `template` | whatever the user's own tooling wants |

`aligned` is the only one that depends on what is on screen, and it takes that
as a `Columns` rather than reading a widget: the engine does not know what the
view is showing, so the view says.

## Why the column widths come from the selection

SPEC 11.3: widths are computed from the widest value in the *selection*, not in
the log. Copying forty rows out of a 41M-row file must not pad them to the
width of a line number that is not in the copy.

That is also why `export`, which streams, measures a **sample**: holding every
row to measure the widest would defeat the streaming, and a 10M-row export
padded to a width discovered on row 9,999,999 would have to go back and redo
the first ten million. `ExportSpec.sample_rows` says how many rows are measured
before the first is written, and the preset's own documentation says so too.

## Line numbers, multi-line entries, and the `?`

* **Line numbers are 1-based**, matching every editor a reader will open the
  file in.
* **A multi-line entry** emits its annotation once, on the first physical line,
  with the continuations indented beneath it -- otherwise a pasted stack trace
  interleaves annotations with Java frames and is unreadable.
  `annotate_every_line` switches to one annotation per physical line with
  incrementing numbers, for people pasting into tools that parse line
  references.
* **A citation that may not resolve** is marked with a trailing `?` on the line
  number, and `notes()` says why (PRD risk R10). A source that was rotated out
  from under the session is still perfectly readable -- the handle serves the
  renamed file -- but its path now points at something else, so `app.log:7` is
  a reference a colleague cannot follow. Marking the number is uniform across
  every preset, including the CSV column and the template's `{line}`, which is
  the reason it is done there rather than in a header comment.
"""

from __future__ import annotations

import csv
import io
import string
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..index.civil import format_wall_us
from ..index.severity import Severity
from .session import Row, Session
from .timefmt import TimeFormatter


class Preset(StrEnum):
    ANNOTATED = "annotated"
    ALIGNED = "aligned"
    PLAIN = "plain"
    MARKDOWN = "markdown"
    CSV = "csv"
    TEMPLATE = "template"


class SourceNaming(StrEnum):
    BASENAME = "basename"
    ALIAS = "alias"
    PATH = "path"


PRESET_LABELS: dict[str, str] = {
    Preset.ANNOTATED: "Annotated (file:line)",
    Preset.ALIGNED: "As displayed",
    Preset.PLAIN: "Plain lines",
    Preset.MARKDOWN: "Markdown table",
    Preset.CSV: "CSV",
    Preset.TEMPLATE: "Custom template",
}
"""Menu and dialog labels, here rather than in the UI so the two cannot drift."""

TEMPLATE_FIELDS = ("source", "alias", "path", "line", "timestamp", "severity",
                   "message", "row")
"""
What a `template` may refer to (FR-46b).

Fixed and enumerated, so a typo in a template is reported as a typo instead of
raising `KeyError` out of a copy.
"""

DEFAULT_TEMPLATE = "{source}:{line}  {timestamp}  {message}"

COLUMN_HEADINGS = ("Source", "Line", "Time", "Severity", "Message")
"""The columns `markdown` and `csv` share, per SPEC 11.3."""


@dataclass(frozen=True)
class Columns:
    """
    Which columns the view is showing, for `aligned` (FR-46b).

    Defaults match the view's own defaults: the timestamp column on, the alias
    and line-number gutters off.
    """

    timestamp: bool = True
    alias: bool = False
    line_no: bool = False


def validate_template(template: str) -> str:
    """Empty if `template` only uses known fields, else what is wrong with it."""
    if not template.strip():
        return "the template is empty"
    try:
        used = {name for _, name, _, _ in string.Formatter().parse(template)
                if name}
    except ValueError as exc:
        return str(exc)
    unknown = sorted(n.split(".")[0].split("[")[0] for n in used
                     if n.split(".")[0].split("[")[0] not in TEMPLATE_FIELDS)
    if unknown:
        return (f"unknown field{'s' if len(unknown) > 1 else ''}: "
                f"{', '.join('{' + u + '}' for u in unknown)}")
    return ""


class RowFormatter:
    """
    Renders rows to text with attribution.

    Stateless between calls apart from its options, so one instance can serve a
    clipboard copy and then an export, and a streamed export can call `chunk`
    repeatedly with the same measured widths.
    """

    def __init__(
        self,
        session: Session,
        *,
        preset: Preset = Preset.ANNOTATED,
        naming: SourceNaming = SourceNaming.BASENAME,
        attribute: bool = True,
        micros: bool = True,
        annotate_every_line: bool = False,
        trim_timestamp: bool = True,
        columns: Columns | None = None,
        template: str = DEFAULT_TEMPLATE,
        time_fmt: TimeFormatter | None = None,
    ):
        self.session = session
        self.preset = Preset(preset)
        self.naming = SourceNaming(naming)
        self.attribute = attribute
        self.micros = micros
        self.annotate_every_line = annotate_every_line
        self.trim_timestamp = trim_timestamp
        self.columns = columns or Columns()
        self.template = template or DEFAULT_TEMPLATE
        self.time_fmt = time_fmt
        """
        The view's timestamp format (FR-35), used by `aligned` **only**.

        `aligned` means "what is on screen", so a reader in delta mode gets
        `+1:23.456789` in their paste, because that is the column they were
        looking at and the thing they selected those rows to show.

        Every other preset keeps the canonical absolute rendering whatever the
        view is set to. A relative offset is meaningless away from the screen
        it was read on: `+1:23.456789` in a ticket cites nothing a colleague
        can find, and a CSV column of deltas cannot be sorted back into time
        order. Attribution exists to be followed, so the formats that attribute
        print the time the file states.
        """

    # ------------------------------------------------------------------
    # the pieces every preset shares
    # ------------------------------------------------------------------

    def source_name(self, source: int) -> str:
        ss = self.session.sources[source]
        if self.naming is SourceNaming.ALIAS:
            return ss.alias
        origin = ss.meta.origin or ss.alias
        if self.naming is SourceNaming.PATH:
            return origin
        return Path(origin).name or ss.alias

    def suspect(self, source: int) -> str:
        """
        Why this source's line numbers may not resolve, or empty (risk R10).

        Read from the session rather than decided here: whether a file was
        rotated or truncated under us is the follow engine's observation, and
        it records it on the source.
        """
        ss = self.session.sources[source]
        return getattr(ss, "citation_note", "") or ""

    def line_ref(self, row: Row, offset: int = 0) -> str:
        """A row's line number as it is cited, with the R10 mark if it needs one."""
        return f"{row.line_no + offset}{'?' if self.suspect(row.source) else ''}"

    def notes(self, rows: list[Row]) -> list[str]:
        """
        What the caller should say about this output, if anything.

        Only ever about citations that may not resolve: the status bar shows it
        after a copy, and the export dialog after a write.
        """
        seen: dict[str, str] = {}
        for row in rows:
            if (why := self.suspect(row.source)):
                seen[self.source_name(row.source)] = why
        return [f"{name}: {why}" for name, why in sorted(seen.items())]

    def body(self, row: Row) -> str:
        """
        A row's text with a repeated leading timestamp trimmed off.

        Every preset that prints a timestamp column of its own needs this, or
        the same value lands on the line twice -- the identical duplication the
        view avoids by the same means (SPEC 9.5). Nothing is trimmed when the
        timestamp sits inside the line rather than leading it, because the text
        in front of it is then content, not a repetition.
        """
        return row.message if self.trim_timestamp else row.text

    def stamp(self, row: Row) -> str:
        return format_wall_us(row.ts, micros=self.micros)

    @staticmethod
    def severity_name(row: Row) -> str:
        return Severity(row.severity).name

    # ------------------------------------------------------------------
    # one call for a whole selection
    # ------------------------------------------------------------------

    def format(self, rows: list[Row]) -> str:
        """Render `rows` complete, header and all. What the clipboard wants."""
        if not rows:
            return ""
        widths = self.measure(rows)
        head = self.header(widths)
        body = self.chunk(rows, widths)
        return f"{head}\n{body}" if head else body

    # ------------------------------------------------------------------
    # the streaming interface (FR-47)
    # ------------------------------------------------------------------

    def measure(self, rows: list[Row]) -> dict[str, int]:
        """
        Column widths for this selection -- or for a sample of a stream.

        Returned as plain data so a streamed export measures once and reuses
        it: see the module docstring on why a stream cannot measure everything.
        """
        tags = [self.tag(r) for r in rows]
        return {
            "tag": max((len(t) for t in tags), default=0),
            "stamp": max((len(self.stamp(r)) for r in rows), default=0),
            "alias": max((len(self.session.sources[r.source].alias)
                          for r in rows), default=0),
            "line": max((len(self.line_ref(r)) for r in rows), default=0),
        }

    def header(self, widths: dict[str, int] | None = None) -> str:
        """The lines that come before the first row, if the preset has any."""
        if self.preset is Preset.MARKDOWN:
            return ("| " + " | ".join(COLUMN_HEADINGS) + " |\n|"
                    + "|".join("---" for _ in COLUMN_HEADINGS) + "|")
        if self.preset is Preset.CSV:
            return _csv_row(COLUMN_HEADINGS)
        return ""

    def chunk(self, rows: list[Row], widths: dict[str, int] | None = None) -> str:
        """
        Render rows with no header -- the unit a streamed export writes.

        `widths` comes from `measure`; without it the chunk is measured on its
        own, which is right for a clipboard copy and wrong for a stream (every
        chunk would align differently).
        """
        if not rows:
            return ""
        widths = widths or self.measure(rows)
        render = {
            Preset.ANNOTATED: self._annotated,
            Preset.ALIGNED: self._aligned,
            Preset.PLAIN: self._plain,
            Preset.MARKDOWN: self._markdown,
            Preset.CSV: self._csv,
            Preset.TEMPLATE: self._template,
        }[self.preset]
        return render(rows, widths)

    # ------------------------------------------------------------------
    # the presets
    # ------------------------------------------------------------------

    def tag(self, row: Row) -> str:
        return f"{self.source_name(row.source)}:{self.line_ref(row)}"

    def _annotated(self, rows: list[Row], widths: dict[str, int]) -> str:
        width = widths["tag"] if self.attribute else 0
        out: list[str] = []
        for r in rows:
            stamp = self.stamp(r)
            physical = self.body(r).splitlines() or [""]
            for i, part in enumerate(physical):
                if not self.attribute:
                    pad = "" if i == 0 else " " * len(stamp)
                    out.append(f"{stamp}  {part}" if i == 0 else f"{pad}  {part}")
                elif self.annotate_every_line:
                    tag = f"{self.source_name(r.source)}:{self.line_ref(r, i)}"
                    out.append(f"{tag:<{width}}  {stamp}  {part}")
                elif i == 0:
                    out.append(f"{self.tag(r):<{width}}  {stamp}  {part}")
                else:
                    # Indented to the message column, so the continuation lines
                    # line up under the text they belong to.
                    out.append(f"{'':<{width}}  {' ' * len(stamp)}  {part}")
        return "\n".join(out)

    def _aligned(self, rows: list[Row], widths: dict[str, int]) -> str:
        """
        Exactly as displayed, in the column order the view draws (SPEC 9).

        Left to right: line number, source alias, timestamp, message -- and
        only the ones the view is actually showing, because "as displayed" with
        a column the reader cannot see is not as displayed.

        The timestamp is in the view's own format (FR-35), including a relative
        one: every mode renders to a fixed width, so the columns still line up.
        A delta needs the row above the selection, which is one `row_to_time`
        for the whole copy rather than a row fetched per row.
        """
        cols = self.columns
        fmt = self.time_fmt
        previous: int | None = None
        if fmt is not None and fmt.needs_previous and rows[0].row > 0:
            previous = self.session.row_to_time(rows[0].row - 1)
        out: list[str] = []
        for r in rows:
            prefix: list[str] = []
            if cols.line_no:
                prefix.append(f"{self.line_ref(r):>{widths['line']}}")
            if cols.alias:
                alias = self.session.sources[r.source].alias
                prefix.append(f"{alias:<{widths['alias']}}")
            if cols.timestamp:
                prefix.append(fmt.render(r.ts, previous) if fmt
                              else self.stamp(r))
            previous = r.ts
            lead = "  ".join(prefix)
            text = self.body(r) if cols.timestamp else r.text
            physical = text.splitlines() or [""]
            for i, part in enumerate(physical):
                head = lead if i == 0 else " " * len(lead)
                out.append(f"{head}  {part}" if lead else part)
        return "\n".join(out)

    def _plain(self, rows: list[Row], _widths: dict[str, int]) -> str:
        """
        The lines verbatim, as they appear in their own files.

        **A correction to SPEC 11.3**, which describes this preset as "message
        text only, no timestamp". M4 shipped it as the verbatim line and that
        is the right behaviour: the preset exists to feed a parser, a diff or a
        grep, and all three want the line that is in the file. Trimming the
        leading timestamp would produce text that matches nothing in the
        original -- and `aligned` already covers "what I can see on screen".
        """
        return "\n".join(r.text for r in rows)

    def _markdown(self, rows: list[Row], _widths: dict[str, int]) -> str:
        out: list[str] = []
        for r in rows:
            cells = (
                self.source_name(r.source),
                self.line_ref(r),
                self.stamp(r),
                self.severity_name(r),
                # A pipe in the text would end the cell, and a newline would
                # end the row. `<br>` is what a Markdown table has for a line
                # break, so a stack trace stays inside its cell.
                _md_escape(self.body(r)),
            )
            out.append("| " + " | ".join(cells) + " |")
        return "\n".join(out)

    def _csv(self, rows: list[Row], _widths: dict[str, int]) -> str:
        return "\n".join(_csv_row((
            self.source_name(r.source),
            self.line_ref(r),
            self.stamp(r),
            self.severity_name(r),
            self.body(r),
        )) for r in rows)

    def _template(self, rows: list[Row], _widths: dict[str, int]) -> str:
        out: list[str] = []
        for r in rows:
            ss = self.session.sources[r.source]
            out.append(self.template.format(
                source=self.source_name(r.source),
                alias=ss.alias,
                path=ss.meta.origin or "",
                line=self.line_ref(r),
                timestamp=self.stamp(r),
                severity=self.severity_name(r),
                message=self.body(r),
                row=r.row,
            ))
        return "\n".join(out)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


def _csv_row(cells) -> str:
    """
    One RFC4180 row.

    `csv.writer` and not an f-string: quoting, doubling an embedded quote and
    keeping a newline inside a quoted field are exactly the details a hand-made
    CSV gets wrong, and a spreadsheet reading it wrongly is silent.
    """
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(list(cells))
    return buf.getvalue()
