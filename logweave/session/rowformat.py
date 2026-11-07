"""
Row formatting for copy and export -- D9, FR-46a, SPEC 11.3.

SPEC 11.3 lists six presets and places them in M7. Two are implemented here,
`annotated` and `plain`, because M3 is meant to be a usable build and a log
viewer you cannot copy a line out of is not one. The rest (`aligned`,
`markdown`, `csv`, `template`) extend this module in M7; the interface is shaped
for them now so attribution has exactly one implementation, as SPEC 11.3
requires.

Qt-free, like everything at layer 4 -- which is what lets the same code serve
the clipboard and a file export without either knowing about the other.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from ..index.civil import format_wall_us
from .session import Row, Session


class Preset(StrEnum):
    ANNOTATED = "annotated"
    PLAIN = "plain"


class SourceNaming(StrEnum):
    BASENAME = "basename"
    ALIAS = "alias"
    PATH = "path"


class RowFormatter:
    """
    Renders rows to text with attribution.

    Column widths are computed from the *selection*, not the log, per SPEC 11.3 --
    so copying forty rows out of a 41M-row file does not pad them to the widest
    name in the whole session.
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
    ):
        self.session = session
        self.preset = preset
        self.naming = naming
        self.attribute = attribute
        self.micros = micros
        self.annotate_every_line = annotate_every_line
        self.trim_timestamp = trim_timestamp

    def source_name(self, source: int) -> str:
        ss = self.session.sources[source]
        if self.naming is SourceNaming.ALIAS:
            return ss.alias
        origin = ss.meta.origin or ss.alias
        if self.naming is SourceNaming.PATH:
            return origin
        return Path(origin).name or ss.alias

    def body(self, row: Row) -> str:
        """
        A row's text with a repeated leading timestamp trimmed off.

        `annotated` prints a timestamp column of its own, so without this the
        same value lands on the line twice -- the identical duplication the view
        avoids by the same means (SPEC 9.5). Nothing is trimmed when the
        timestamp sits inside the line rather than leading it, because the text
        in front of it is then content, not a repetition.
        """
        return row.message if self.trim_timestamp else row.text

    def format(self, rows: list[Row]) -> str:
        """
        Render rows to text.

        A multi-line entry is the case that makes this more than an f-string.
        Its text spans several physical lines, and SPEC 11.3 requires the
        annotation once, on the first, with the continuation lines indented
        beneath it -- otherwise a pasted stack trace interleaves annotations
        with Java frames and is unreadable. `annotate_every_line` switches to
        one annotation per physical line with incrementing numbers, for people
        pasting into tools that parse line references.
        """
        if not rows:
            return ""
        if self.preset is Preset.PLAIN:
            return "\n".join(r.text for r in rows)

        tags = [f"{self.source_name(r.source)}:{r.line_no}" for r in rows]
        if self.attribute and self.annotate_every_line:
            tags = [
                f"{self.source_name(r.source)}:{r.line_no + i}"
                for r in rows
                for i in range(len(self.body(r).splitlines()) or 1)
            ]
        width = max(len(t) for t in tags) if self.attribute else 0

        out: list[str] = []
        tag_iter = iter(tags)
        for r in rows:
            stamp = format_wall_us(r.ts, micros=self.micros)
            physical = self.body(r).splitlines() or [""]
            for i, part in enumerate(physical):
                if not self.attribute:
                    out.append(f"{stamp}  {part}" if i == 0 else f"{' ' * len(stamp)}  {part}")
                elif self.annotate_every_line:
                    out.append(f"{next(tag_iter):<{width}}  {stamp}  {part}")
                elif i == 0:
                    out.append(f"{next(tag_iter):<{width}}  {stamp}  {part}")
                else:
                    # Indented to the message column, so the continuation lines
                    # line up under the text they belong to.
                    out.append(f"{'':<{width}}  {' ' * len(stamp)}  {part}")
        return "\n".join(out)
