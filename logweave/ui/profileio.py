"""
A window into a profile, and a profile into a window -- FR-49 to FR-52.

The `logweave.profile` package knows what a profile *is* and what its globs
mean on disk, and knows nothing about a window. This module is the join: it
reads a `MainWindow` into a `Profile`, and installs a `Profile` into one. It
lives in `ui` because everything it touches on the window side -- the view's
columns, the strips, the follow engine, the highlight group -- is Qt.

## What a profile saves, and what it leaves alone

The rule from SPEC 12.3, applied field by field:

* **Saved**: which files, their aliases, colours, skews, follow and enabled
  state, the filter in force, the bookmarks, the timestamp format, which
  columns and strips are showing, and the user's highlight rules.
* **Not saved**: anything about this machine -- window size, cache location,
  recent lists -- and anything that would make opening someone else's profile
  feel like an intrusion. The font size is saved, because a profile may be for
  a wall display, and it is restored *only* if the profile says so.

A profile's view preferences are therefore `None` where it has no opinion, and
`apply` touches nothing that is `None`.

## Opening is asynchronous, so applying is in two halves

Files index on a worker thread (SPEC 5.3), so `open` cannot put the aliases and
the bookmarks on afterwards in the same call -- there is nothing to put them on
yet. `pending` carries the profile until the window's open completes, and
`finish` does the rest. A test with `background=False` goes through exactly the
same two halves, which is what keeps that path from being a special case that
nobody runs.
"""

from __future__ import annotations

from pathlib import Path

from ..highlight.builtins import USER_SET
from ..highlight.rules import Ruleset
from ..highlight.store import rules_from_dicts, rules_to_dicts
from ..index.severity import Severity
from ..profile import (
    Bookmark,
    HighlightPrefs,
    Profile,
    Resolution,
    SavedFilter,
    SourceSpec,
    ViewPrefs,
)
from ..session import OpenOptions
from ..session.filters import Filter
from ..session.timefmt import TimeDisplay, TimeMode

# ---------------------------------------------------------------------------
# a window into a profile (FR-49)
# ---------------------------------------------------------------------------


def capture(window, *, name: str = "") -> Profile:
    """
    Read the window's state into a profile.

    The files are written as **paths, not globs**, even when the session was
    opened from a glob. What is open is what the reader checked; writing the
    glob back would save a question whose answer they never saw. `freeze` on
    the resolve side does the same thing for the same reason, and a reader who
    wants the glob keeps the profile they opened.
    """
    session = window.session
    sources = [
        SourceSpec(
            path=ss.meta.origin or "",
            alias=ss.alias,
            colour=ss.colour,
            follow=bool(ss.meta.follow),
            enabled=bool(ss.meta.enabled),
            skew_us=int(ss.meta.skew_us),
            encoding="",
            ts_format="",
        )
        for ss in session.sources
    ]
    # Encoding and format are left empty on purpose. Both are *detected* for
    # an ordinary open, and writing a detected value back as an override would
    # freeze a guess into the file -- so that a later build which detects
    # better, or a file whose encoding changed at rotation, would be forced to
    # keep reading it the old way. A profile carries them only when a reader
    # has overridden them, which is a thing they do by editing the file.

    return Profile(
        name=name or window.windowTitle().replace("LogWeave", "").strip(" -")
        or "LogWeave session",
        sources=sources,
        view=_capture_view(window),
        filters=_capture_filters(window),
        bookmarks=_capture_bookmarks(window),
        highlight=_capture_highlight(window),
    )


def _capture_view(window) -> ViewPrefs:
    view = window.view
    display = window.time_display
    return ViewPrefs(
        timestamp_mode=display.mode.value,
        # The elapsed zero point is not saved, for the reason `settings`
        # gives: it is a row in the log that was open, not a preference.
        timestamp_format=display.pattern,
        show_timestamp=view.show_timestamp,
        show_alias=view.show_alias,
        show_line_no=view.show_line_no,
        lane_width=view.lane_width,
        minimap=window.minimap.isVisibleTo(window),
        timeline=window.timeline.isVisibleTo(window),
        font_point=view.font_point,
    )


def _capture_filters(window) -> list[SavedFilter]:
    """
    The filter in force, if any, under the name the bar shows for it.

    One, not a library: FR-45's filters are built in the bar and this is the
    one the reader has decided on. A hand-written profile may carry several,
    and `Search ▸ Saved Filters` offers all of them -- which is how a team
    shares "the four views we always look at" without a filter manager nobody
    asked for.
    """
    filt = window.filterbar.filter() if window.filterbar.isVisibleTo(window) \
        else (window.session.filter.filt if window.session.filter else None)
    if filt is None or not filt.active:
        return []
    aliases = [window.session.sources[i].alias for i in sorted(filt.sources)
               if i < len(window.session.sources)]
    return [SavedFilter(
        name=filt.describe() or "saved filter",
        severities=tuple(Severity(s).name for s in sorted(filt.severities)),
        sources=tuple(aliases),
        include=filt.include,
        exclude=filt.exclude,
        regex=filt.regex,
        case_sensitive=filt.case_sensitive,
        since_us=filt.since,
        until_us=filt.until,
    )]


def _capture_bookmarks(window) -> list[Bookmark]:
    return [Bookmark(source=alias, wall_us=wall_us, line_no=line_no, note=note)
            for alias, wall_us, line_no, note
            in window.bookmarks.to_pairs(window.session)]


def _capture_highlight(window) -> HighlightPrefs:
    user = window.highlight_group.sets.get(USER_SET)
    rules = list(getattr(user, "rules", ()))
    return HighlightPrefs(inherit_global=True, rules=tuple(rules_to_dicts(rules)))


# ---------------------------------------------------------------------------
# a profile into a window (FR-50 to FR-52)
# ---------------------------------------------------------------------------


def open_options(resolution: Resolution) -> list[OpenOptions]:
    """
    One `OpenOptions` per resolved file, in the order they will be opened.

    A glob that resolved to four files gives all four the same spec, which is
    what a reader means by `gateway-*.log` with `alias = "gw"`: one name for
    one rotated log. The aliases are then made distinct by the session's own
    rule (`_assign_aliases` leaves custom names alone), so they arrive as
    `gw`, `gw~1` and so on rather than four identical columns.
    """
    return [
        OpenOptions(
            alias=spec.alias,
            colour=spec.colour,
            skew_us=spec.skew_us,
            enabled=spec.enabled,
            follow=spec.follow,
            encoding=spec.encoding,
            ts_format=spec.ts_format,
        )
        for _, spec in resolution.opened
    ]


def forced_formats(resolution: Resolution) -> dict[str, str]:
    """Paths whose format a profile overrides, for the indexing warm (FR-13)."""
    return {str(path): spec.ts_format
            for path, spec in resolution.opened if spec.ts_format}


def apply_view(window, view: ViewPrefs) -> None:
    """
    Install the display choices a profile has an opinion about.

    Silent about the rest: a `None` means the profile says nothing, and the
    reader's own preference stays exactly as it was. This is the whole of
    "opening a colleague's profile does not resize your text".
    """
    if view.font_point is not None:
        window.view.set_font_point(int(view.font_point))
    if view.lane_width is not None:
        window._set_lane_width(int(view.lane_width))
    if view.show_timestamp is not None:
        window._timestamp_toggled(bool(view.show_timestamp))
    if view.show_alias is not None:
        window._alias_toggled(bool(view.show_alias))
    if view.show_line_no is not None:
        window._line_no_toggled(bool(view.show_line_no))
    if view.minimap is not None or view.timeline is not None:
        window._set_strips_visible(minimap=view.minimap, timeline=view.timeline)

    mode, pattern = window.time_display.mode, window.time_display.pattern
    if view.timestamp_mode:
        try:
            mode = TimeMode(view.timestamp_mode)
        except ValueError:
            pass            # reported by `Profile.validate`, not fatal here
    if view.timestamp_format:
        pattern = view.timestamp_format
    window.time_display = TimeDisplay(mode=mode, pattern=pattern)
    window._apply_time_display()
    window._sync_setting_menus()


def apply_highlight(window, prefs: HighlightPrefs) -> list[str]:
    """
    Add a profile's rules to this window, without touching the stored ones.

    **Not written to `QSettings`.** A profile's rules belong to the profile:
    opening a colleague's must not edit your own list, and closing it must
    leave yours as it was. They are merged by name with `inherit_global` on,
    so a rule you both have appears once -- and the profile's version wins,
    because that is the one the profile is for.
    """
    rules, notes = rules_from_dicts(prefs.rules)
    if prefs.inherit_global:
        existing = list(getattr(window.highlight_group.sets.get(USER_SET),
                                "rules", ()))
        by_name = {r.name: r for r in existing}
        by_name.update({r.name: r for r in rules})
        rules = list(by_name.values())
    window.highlight_group.sets[USER_SET] = Ruleset(rules)
    window._apply_highlights()
    return list(notes)


def to_filter(window, saved: SavedFilter) -> tuple[Filter, list[str]]:
    """
    A saved filter as a live one, naming the sources that are actually open.

    An alias the profile names and the session does not have is reported, not
    quietly dropped: a filter that was "these two services" and silently
    becomes "everything" shows the reader rows they thought they had excluded,
    which is the kind of wrong that gets believed.
    """
    notes: list[str] = []
    by_alias = {ss.alias: i for i, ss in enumerate(window.session.sources)}
    positions = set()
    for alias in saved.sources:
        if alias in by_alias:
            positions.add(by_alias[alias])
        else:
            notes.append(f"filter {saved.name!r}: no open source called {alias!r}")

    severities = set()
    for name in saved.severities:
        try:
            severities.add(int(Severity[name]))
        except KeyError:
            notes.append(f"filter {saved.name!r}: unknown severity {name!r}")

    return Filter(
        severities=frozenset(severities),
        sources=frozenset(positions),
        since=saved.since_us,
        until=saved.until_us,
        include=saved.include,
        exclude=saved.exclude,
        regex=saved.regex,
        case_sensitive=saved.case_sensitive,
    ), notes


def default_name(path: Path | str) -> str:
    return Path(path).stem


def describe(resolution: Resolution, profile: Profile) -> list[str]:
    """
    Every line the open-outcome panel shows (FR-52), worst news first.

    One list rather than a dialog per problem: a profile written for a
    rotating set of logs legitimately has unmatched globs most mornings, and a
    modal per glob would make the feature unusable on exactly the files it
    exists for.
    """
    lines: list[str] = []
    for why in profile.validate():
        lines.append(f"⚠ {why}")
    for path in resolution.missing:
        lines.append(f"missing: {path}")
    for pattern in resolution.unmatched:
        lines.append(f"matched nothing: {pattern}")
    for why in resolution.invalid:
        lines.append(f"invalid: {why}")
    for note in profile.notes:
        lines.append(f"note: {note}")
    return lines
