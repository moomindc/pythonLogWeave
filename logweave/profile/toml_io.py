"""
Reading and writing `.lwprofile` files -- FR-53, SPEC 12.1. Qt-free.

TOML, because a profile is meant to be read, diffed and reviewed by people:
it goes into a repository beside the service it describes, and a colleague
should be able to see from a pull request that someone added a log file.

## Reading is stdlib; writing is ours. A correction to SPEC 12.1

The SPEC specified `tomlkit` for writing, so that a user's own comments and
ordering survive a round-trip. That was the right instinct and the wrong
dependency: `tomlkit` is not in the engine's dependency set, and the engine's
one hard dependency being numpy is what keeps layers 1-4 installable and
testable without the GUI (SPEC 1, SPEC 16). A profile reader that only worked
where Qt was installed would make the headless export this architecture exists
to allow impossible.

So: **`tomllib` reads** (it has been in the standard library since 3.11), and
`dump` writes a file this module generates in a fixed order with its own
explanatory comments. The property that actually matters for a
version-controlled file is that a rewrite changes only what the user changed,
and that is guaranteed by construction here -- same key order, same comments,
same spacing, every time.

The one thing lost is **a user's own comments**, which a rewrite drops.
`comments_at_risk` exists so the caller can say so before overwriting rather
than afterwards, because quietly deleting someone's annotations is not
something a Save button should do.

## Unknown keys are kept out of the way, not treated as errors

A profile written by a later version opens here, minus what this build does not
understand, with a note saying so. The alternative -- refusing the file -- means
a team on mixed versions cannot share one, which is the whole point of the
feature. The same rule covers a source this build cannot open at all: an `ssh`
source (FR-7a, not built) is reported, not silently dropped, because a reader
who sees four files where they wrote five should be told which one is missing.
"""

from __future__ import annotations

import tomllib
from dataclasses import fields
from pathlib import Path

from .model import (
    FORMAT_VERSION,
    Bookmark,
    HighlightPrefs,
    Profile,
    ProfileError,
    SavedFilter,
    SourceSpec,
    ViewPrefs,
)

HEADER = """\
# A LogWeave profile -- the files to open and how to read them.
#
# Paths are absolute and are taken verbatim: no ~, no environment variables,
# no relative root. A profile is machine-specific by design.
#
# Saving over this file from LogWeave rewrites it in this order and does not
# keep comments you add. Keep notes in the `note` of a bookmark, or in the
# commit message.
"""


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def load(path: Path | str) -> Profile:
    """Read a profile, or raise `ProfileError` with something a user can act on."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"{path}: {exc.strerror or exc}") from exc
    return loads(raw.decode("utf-8", errors="replace"), name=path.stem)


def loads(text: str, *, name: str = "") -> Profile:
    """Parse profile text. `name` is the fallback title, normally the stem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"not valid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError("a profile must be a table")

    notes: list[str] = []
    profile = Profile(
        name=str(data.get("name") or name),
        format_version=_int(data.get("format_version"), FORMAT_VERSION),
        sources=_read_sources(data, notes),
        view=_read_view(data.get("view"), notes),
        copy_preset=str(_table(data.get("copy")).get("format", "") or ""),
        copy_naming=str(_table(data.get("copy")).get("source_as", "") or ""),
        filters=_read_filters(data.get("filters"), notes),
        bookmarks=_read_bookmarks(data, notes),
        highlight=_read_highlight(data.get("highlight"), notes),
    )
    _note_unknown(data, _TOP_KEYS, notes, "top level")
    return profile.with_notes(notes)


_TOP_KEYS = frozenset({"format_version", "name", "view", "copy", "source",
                       "filters", "highlight", "bookmark"})

_SOURCE_KEYS = frozenset(
    {f.name for f in fields(SourceSpec)} | {"ssh", "auth", "key_file",
                                            "credential_vault"})


def _read_sources(data: dict, notes: list[str]) -> list[SourceSpec]:
    out: list[SourceSpec] = []
    for entry in _tables(data.get("source")):
        if entry.get("ssh"):
            # FR-7a is phase 2. Named rather than dropped: five files written
            # and four opened is a thing the reader has to be told about.
            notes.append(
                f"{entry['ssh']}: remote sources are not in this build yet; "
                "the source was skipped")
            continue
        spec = SourceSpec(
            path=str(entry.get("path", "") or ""),
            glob=str(entry.get("glob", "") or ""),
            select=str(entry.get("select", "all") or "all"),
            count=_int(entry.get("count"), 3),
            within=str(entry.get("within", "7d") or "7d"),
            alias=str(entry.get("alias", "") or ""),
            colour=str(entry.get("colour", "") or ""),
            follow=_bool(entry.get("follow"), True),
            enabled=_bool(entry.get("enabled"), True),
            skew_us=_int(entry.get("skew_us"), 0),
            encoding=str(entry.get("encoding", "") or ""),
            ts_format=str(entry.get("ts_format", "") or ""),
        )
        _note_unknown(entry, _SOURCE_KEYS, notes, f"source {spec.target}")
        out.append(spec)
    return out


def _read_view(raw, notes: list[str]) -> ViewPrefs:
    table = _table(raw)
    view = ViewPrefs()
    stamp = _table(table.get("timestamp"))
    if "mode" in stamp:
        view.timestamp_mode = str(stamp["mode"])
    if "format" in stamp:
        view.timestamp_format = str(stamp["format"])
    for key, attr in (("alias_column", "show_alias"),
                      ("timestamp_column", "show_timestamp"),
                      ("line_number_column", "show_line_no")):
        if key in table:
            setattr(view, attr, _bool(table[key], True))
    for key, attr in (("minimap", "minimap"), ("timeline", "timeline")):
        if key in table:
            setattr(view, attr, _bool(_table(table[key]).get("visible",
                                                             table[key]), True))
    if "lane_width" in table:
        view.lane_width = _int(table["lane_width"], 6)
    if "font_point" in table:
        view.font_point = _int(table["font_point"], 10)
    _note_unknown(table, _VIEW_KEYS, notes, "view")
    return view


_VIEW_KEYS = frozenset({"timestamp", "alias_column", "timestamp_column",
                        "line_number_column", "minimap", "timeline",
                        "lane_width", "font_point"})


def _read_filters(raw, notes: list[str]) -> list[SavedFilter]:
    saved = _table(_table(raw).get("saved"))
    out = []
    for name, body in saved.items():
        body = _table(body)
        out.append(SavedFilter(
            name=str(name),
            severities=tuple(str(s) for s in _list(body.get("severity"))),
            sources=tuple(str(s) for s in _list(body.get("source"))),
            include=str(body.get("include", "") or ""),
            exclude=str(body.get("exclude", "") or ""),
            regex=_bool(body.get("regex"), False),
            case_sensitive=_bool(body.get("case_sensitive"), False),
            since_us=_opt_int(body.get("since_us")),
            until_us=_opt_int(body.get("until_us")),
        ))
        _note_unknown(body, _FILTER_KEYS, notes, f"filter {name!r}")
    return out


_FILTER_KEYS = frozenset({"severity", "source", "include", "exclude", "regex",
                          "case_sensitive", "since_us", "until_us"})


def _read_bookmarks(data: dict, notes: list[str]) -> list[Bookmark]:
    out = []
    for entry in _tables(data.get("bookmark")):
        out.append(Bookmark(source=str(entry.get("source", "") or ""),
                            wall_us=_int(entry.get("wall_us"), 0),
                            line_no=_int(entry.get("line"), 0),
                            note=str(entry.get("note", "") or "")))
        _note_unknown(entry, {"source", "wall_us", "line", "note"}, notes,
                      "bookmark")
    return out


def _read_highlight(raw, notes: list[str]) -> HighlightPrefs:
    table = _table(raw)
    rules = tuple(dict(r) for r in _tables(table.get("rule")))
    _note_unknown(table, {"inherit_global", "rule"}, notes, "highlight")
    return HighlightPrefs(
        inherit_global=_bool(table.get("inherit_global"), True), rules=rules)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def comments_at_risk(path: Path | str) -> int:
    """
    How many comment lines an overwrite of `path` would not keep.

    Zero for a file this module wrote and nobody has annotated -- the generated
    header is excluded, because warning about losing a comment that will be
    written back identically would be noise.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    generated = {line.strip() for line in HEADER.splitlines()}
    return sum(1 for line in text.splitlines()
               if line.lstrip().startswith("#")
               and line.strip() not in generated)


def dump(profile: Profile) -> str:
    """A profile as TOML text, in a fixed order with its reasons written in."""
    out: list[str] = [HEADER]
    out.append(f"format_version = {FORMAT_VERSION}")
    if profile.name:
        out.append(f"name = {_s(profile.name)}")
    out.append("")

    if not profile.view.empty:
        view = profile.view
        # Scalars first and the sub-table last: TOML allows a table to be
        # defined once, so `[view]` cannot be reopened after `[view.timestamp]`.
        out.append("[view]")
        for key, value in (("timestamp_column", view.show_timestamp),
                           ("alias_column", view.show_alias),
                           ("line_number_column", view.show_line_no),
                           ("minimap", view.minimap),
                           ("timeline", view.timeline)):
            if value is not None:
                out.append(f"{key} = {_b(value)}")
        for key, value in (("lane_width", view.lane_width),
                           ("font_point", view.font_point)):
            if value is not None:
                out.append(f"{key} = {int(value)}")
        out.append("")
        if view.timestamp_mode or view.timestamp_format:
            out.append("[view.timestamp]")
            if view.timestamp_mode:
                out.append(f"mode = {_s(view.timestamp_mode)}"
                           "            # absolute | elapsed | delta")
            if view.timestamp_format:
                out.append(f"format = {_s(view.timestamp_format)}")
            out.append("")

    if profile.copy_preset or profile.copy_naming:
        out.append("[copy]")
        if profile.copy_preset:
            out.append(f"format = {_s(profile.copy_preset)}")
        if profile.copy_naming:
            out.append(f"source_as = {_s(profile.copy_naming)}")
        out.append("")

    for spec in profile.sources:
        out.append("[[source]]")
        out.append(f"{'glob' if spec.is_glob else 'path'} = {_s(spec.target)}")
        if spec.is_glob and spec.select != "all":
            out.append(f"select = {_s(spec.select)}")
            if spec.select == "newest":
                out.append(f"count = {int(spec.count)}")
            if spec.select == "modified_within":
                out.append(f"within = {_s(spec.within)}")
        if spec.alias:
            out.append(f"alias = {_s(spec.alias)}")
        if spec.colour:
            out.append(f"colour = {_s(spec.colour)}")
        out.append(f"follow = {_b(spec.follow)}")
        if not spec.enabled:
            out.append("enabled = false            # opened, but out of the merge")
        if spec.skew_us:
            out.append(f"skew_us = {int(spec.skew_us)}"
                       "            # added to every timestamp (FR-18)")
        if spec.encoding:
            out.append(f"encoding = {_s(spec.encoding)}")
        if spec.ts_format:
            out.append(f"ts_format = {_s(spec.ts_format)}")
        out.append("")

    for filt in profile.filters:
        out.append(f"[filters.saved.{_s(filt.name)}]")
        if filt.severities:
            out.append(f"severity = {_a(filt.severities)}")
        if filt.sources:
            out.append(f"source = {_a(filt.sources)}"
                       "            # aliases, not positions")
        for key, value in (("include", filt.include), ("exclude", filt.exclude)):
            if value:
                out.append(f"{key} = {_s(value)}")
        if filt.regex:
            out.append("regex = true")
        if filt.case_sensitive:
            out.append("case_sensitive = true")
        for key, value in (("since_us", filt.since_us),
                           ("until_us", filt.until_us)):
            if value is not None:
                out.append(f"{key} = {int(value)}")
        out.append("")

    if profile.highlight.rules or not profile.highlight.inherit_global:
        out.append("[highlight]")
        out.append(f"inherit_global = {_b(profile.highlight.inherit_global)}")
        out.append("")
        for rule in profile.highlight.rules:
            out.append("[[highlight.rule]]")
            for key, value in rule.items():
                out.append(f"{key} = {_value(value)}")
            out.append("")

    for mark in profile.bookmarks:
        out.append("[[bookmark]]")
        out.append(f"source = {_s(mark.source)}")
        out.append(f"wall_us = {int(mark.wall_us)}")
        if mark.line_no:
            out.append(f"line = {int(mark.line_no)}"
                       "            # which line, when several share the time")
        if mark.note:
            out.append(f"note = {_s(mark.note)}")
        out.append("")

    return "\n".join(out).rstrip("\n") + "\n"


def save(profile: Profile, path: Path | str) -> Path:
    """
    Write `profile` to `path`, adding the extension if it has none.

    Written whole and then moved into place, so an interrupted save leaves the
    previous profile intact rather than half a file. A profile is the record of
    how to open a set of logs, and losing it to a crash during Save would be
    the worst possible moment to lose it.
    """
    path = Path(path)
    if not path.suffix:
        path = path.with_suffix(".lwprofile")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump(profile), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _table(raw) -> dict:
    return raw if isinstance(raw, dict) else {}


def _tables(raw) -> list[dict]:
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _list(raw) -> list:
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [] if raw is None else [raw]


def _int(raw, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _opt_int(raw) -> int | None:
    return None if raw is None else _int(raw, 0)


def _bool(raw, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return default


def _note_unknown(table: dict, known, notes: list[str], where: str) -> None:
    extra = sorted(set(table) - set(known))
    if extra:
        notes.append(f"{where}: ignored unknown key"
                     f"{'s' if len(extra) > 1 else ''} {', '.join(extra)}")


def _s(text: str) -> str:
    r"""
    A TOML string. Escaped by hand, and that is why it is one place.

    A Windows path is written as a **literal** string ('C:\logs\a.log') rather
    than a basic one. Both are valid TOML and `tomllib` reads either, but a
    basic string has to double every separator -- "C:\\logs\\a.log" -- and
    FR-53 asks for a file a person can read and diff. A doubled path is the
    first thing a reader would try to "fix", and a diff of two of them is
    unreadable.

    Anything carrying a single quote of its own, or a control character, goes
    back to a basic string: a literal string has no escapes at all, so there
    would be no way to write it.
    """
    text = str(text)
    if ("\\" in text and "'" not in text
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)):
        return f"'{text}'"
    out = []
    for ch in text:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _b(value: bool) -> str:
    return "true" if value else "false"


def _a(values) -> str:
    return "[" + ", ".join(_s(v) for v in values) + "]"


def _value(value) -> str:
    if isinstance(value, bool):
        return _b(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return _a(value)
    return _s(value)
