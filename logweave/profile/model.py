"""
What a profile *is* -- FR-49 to FR-53, SPEC 12.1. Qt-free, no I/O.

Plain dataclasses and nothing else: the file format is `toml_io`, resolving
globs against the disk is `resolve`, and turning one into an open session is
the window's job. Keeping the three apart is what lets a profile be built in a
test, written, read back and compared without a file system or a widget.

## Why a profile is not the preferences

The two were separated at M4 and the line has held since (SPEC 12.3). A
**preference** is about the reader -- how big their text is, which columns they
like, their own highlight rules -- and lives in `QSettings`, where it survives
whatever they open. A **profile** is about a particular set of logs: which
files, which skews, which aliases, which filters. Opening a colleague's profile
must not resize your text or take their colours, and sharing yours must not
leak where your windows were.

Two things sit on that line and are in a profile deliberately:

* **The view's column choices and the timestamp format.** A profile for an IIS
  investigation genuinely wants the line-number gutter on and the date off,
  because that is a fact about *these logs*. They are stored as
  `None`-defaulting overrides, so a profile that says nothing about them leaves
  the reader's own preferences exactly as they were.
* **Highlight rules**, under `inherit_global`. A team's profile may carry the
  three rules that matter for its subsystem without replacing a reader's own.

## Absolute paths, and nothing clever (D10, FR-50)

A path or a glob in a profile is taken verbatim. No `~`, no environment
variables, no relative root. A profile is machine-specific by design and says
so; the alternative is a file that resolves differently depending on where it
was opened from, which is the failure mode that makes "it works on my machine"
a sentence people say about log tooling.

A relative path is therefore a **validation error** rather than something to
resolve against the current directory: the current directory of a GUI is
whatever the shell or the shortcut happened to leave it as, and quietly opening
the wrong file is worse than refusing a line.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import PurePath

FORMAT_VERSION = 1
"""
Bumped only for a change a previous version could not read correctly.

Adding a key does not bump it: an older build ignores what it does not know,
which is the behaviour `toml_io` implements deliberately rather than by
accident. Removing or re-meaning one does.
"""

SUFFIX = ".lwprofile"
"""FR-53. Its own extension so it can carry a file association of its own."""


class ProfileError(ValueError):
    """A profile that cannot be read, or asks for something impossible."""


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

SELECT_ALL = "all"
SELECT_NEWEST = "newest"
SELECT_WITHIN = "modified_within"

SELECTIONS = (SELECT_ALL, SELECT_NEWEST, SELECT_WITHIN)


@dataclass
class SourceSpec:
    """
    One source in a profile: a file, or a glob that stands for several (FR-50).

    `path` and `glob` are the same field in two moods and exactly one is set.
    They are kept apart rather than folded into one "target" because they mean
    different things when they fail to resolve: a named file that is not there
    is **missing**, and a glob that matches nothing is **unmatched** -- the
    first is probably a mistake, the second is probably Tuesday (FR-52).
    """

    path: str = ""
    glob: str = ""
    select: str = SELECT_ALL
    """FR-51: `all`, `newest` (with `count`), or `modified_within` (`within`)."""
    count: int = 3
    within: str = "7d"
    alias: str = ""
    colour: str = ""
    follow: bool = True
    enabled: bool = True
    """FR-9: in the merge or hidden. A profile may open a source switched off --
    a noisy sidecar log you want available but not in the way."""
    skew_us: int = 0
    """D2 / FR-18, applied at merge time rather than re-indexing."""
    encoding: str = ""
    """FR-5 override. Empty means detect, which is almost always right."""
    ts_format: str = ""
    """
    FR-13 override: the catalogue format name to read timestamps with.

    For the file whose first thousand lines are a banner and whose format is
    therefore detected wrongly. Empty means detect.
    """

    @property
    def target(self) -> str:
        return self.path or self.glob

    @property
    def is_glob(self) -> bool:
        return bool(self.glob)

    def validate(self) -> str:
        """Empty if this spec is usable, else what is wrong with it."""
        if not self.target:
            return "a source with neither a path nor a glob"
        if self.path and self.glob:
            return f"{self.target}: a source has a path or a glob, not both"
        if not PurePath(self.target).is_absolute():
            return (f"{self.target}: paths must be absolute -- a profile is "
                    "machine-specific by design (D10)")
        if self.select not in SELECTIONS:
            return (f"{self.target}: unknown selection {self.select!r} "
                    f"(expected {', '.join(SELECTIONS)})")
        if self.select == SELECT_NEWEST and self.count < 1:
            return f"{self.target}: newest needs a count of at least one"
        if self.select == SELECT_WITHIN and parse_duration(self.within) is None:
            return (f"{self.target}: {self.within!r} is not a duration "
                    "(try 30m, 12h, 7d)")
        if self.path and self.select != SELECT_ALL:
            return (f"{self.path}: a selection policy applies to a glob, and "
                    "this names one file")
        return ""


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}


def parse_duration(text: str) -> int | None:
    """
    `"7d"` to seconds, or None if it is not a duration.

    Deliberately small: a profile is read by people as well as by this, and a
    full duration grammar would invite `P7DT4H` into a file whose whole appeal
    is that you can see what it says.
    """
    text = (text or "").strip().lower()
    if len(text) < 2 or text[-1] not in _UNITS:
        return None
    try:
        value = float(text[:-1])
    except ValueError:
        return None
    if value <= 0:
        return None
    return int(value * _UNITS[text[-1]])


# ---------------------------------------------------------------------------
# the rest of the session
# ---------------------------------------------------------------------------


@dataclass
class ViewPrefs:
    """
    The display choices a profile may carry (FR-49).

    Every field defaults to `None`, meaning "this profile has no opinion, leave
    the reader's preference alone". That is what keeps opening a colleague's
    profile from resizing your text: they saved a set of files, not a verdict
    on your eyesight.
    """

    timestamp_mode: str | None = None
    timestamp_format: str | None = None
    show_timestamp: bool | None = None
    show_alias: bool | None = None
    show_line_no: bool | None = None
    lane_width: int | None = None
    minimap: bool | None = None
    timeline: bool | None = None
    font_point: int | None = None

    @property
    def empty(self) -> bool:
        return all(getattr(self, f) is None for f in self.__dataclass_fields__)


@dataclass
class SavedFilter:
    """
    A named predicate (FR-45, FR-49), stored by **alias** rather than position.

    A `filters.Filter` names sources by their position in the session, which is
    the right thing in a live session and the wrong thing in a file: positions
    move when a glob resolves to a different number of files. Aliases survive
    that, and a saved filter naming an alias nothing resolved to is reported
    rather than silently widening the filter (FR-52).
    """

    name: str = ""
    severities: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    include: str = ""
    exclude: str = ""
    regex: bool = False
    case_sensitive: bool = False
    since_us: int | None = None
    until_us: int | None = None


@dataclass
class Bookmark:
    """
    A remembered row (FR-37), stored as alias plus timestamp (SPEC 12.2).

    Never a row number. A row moves when a source is hidden, a filter changes
    or a followed file appends in the middle -- the same reason M7's search
    hits are `(source, entry)` pairs with rows derived on demand. A timestamp
    is what the row *is*, so a bookmark survives the file having grown,
    rotated, or been re-indexed.
    """

    source: str = ""
    wall_us: int = 0
    line_no: int = 0
    """
    The line in its own file, which is what tells one entry from another when
    several share a timestamp.

    A correction to SPEC 12.2's "(source alias, timestamp)": a service writing
    four lines in the same millisecond gives them one timestamp, so restoring
    by time alone lands on the first of the run. It is also the part a human
    reading the file can act on -- `gw` line 1423 is somewhere you can go and
    look. The timestamp stays the primary key, because a rewritten file keeps
    its times and renumbers its lines.
    """
    note: str = ""


@dataclass
class HighlightPrefs:
    """
    Rules a profile carries, and whether they add to the reader's own.

    `inherit_global` defaults true: a profile's rules are nearly always *extra*
    -- the three patterns that matter for this subsystem -- and a profile that
    silently switched off a reader's own colours would be a surprise with no
    visible cause.
    """

    inherit_global: bool = True
    rules: tuple[dict, ...] = ()
    """`highlight.store`'s JSON-shaped dicts, so there is one rule schema."""


@dataclass
class Profile:
    """A named session: what to open, how to show it, what to look for."""

    name: str = ""
    format_version: int = FORMAT_VERSION
    sources: list[SourceSpec] = field(default_factory=list)
    view: ViewPrefs = field(default_factory=ViewPrefs)
    copy_preset: str = ""
    copy_naming: str = ""
    filters: list[SavedFilter] = field(default_factory=list)
    bookmarks: list[Bookmark] = field(default_factory=list)
    highlight: HighlightPrefs = field(default_factory=HighlightPrefs)
    notes: tuple[str, ...] = ()
    """
    What was odd about the file this came from, for the UI to report.

    A profile that mentions an SSH source this build cannot open, or a key from
    a later version, still opens everything else -- and says so, rather than
    appearing to have fewer files in it than the reader remembers writing.
    """

    def validate(self) -> list[str]:
        """Everything wrong with this profile, rather than the first thing."""
        problems = []
        if self.format_version > FORMAT_VERSION:
            problems.append(
                f"written by a later version (format {self.format_version}, "
                f"this build reads {FORMAT_VERSION}) -- it may open "
                "incompletely")
        if not self.sources:
            problems.append("the profile names no sources")
        for spec in self.sources:
            if (why := spec.validate()):
                problems.append(why)
        aliases = [s.alias for s in self.sources if s.alias]
        if len(set(aliases)) != len(aliases):
            problems.append("two sources share an alias")
        return problems

    def with_notes(self, notes) -> Profile:
        return replace(self, notes=tuple(notes))
