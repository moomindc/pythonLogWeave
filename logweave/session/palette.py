"""
Colours for source lanes and severity -- SPEC 9.2, 10.2, FR-28.

Plain hex strings, no Qt, so the assignment rules are testable headlessly and
the same palette can later drive an export to HTML.

Two separate jobs, deliberately not sharing a palette:

* **Severity** is ordered and must read as a temperature scale -- grey through
  blue to red -- so a user scanning a minimap sees severity, not identity.
* **Source lanes** are categorical. A qualitative palette, assigned by source
  order so a given file keeps its colour across a session, and chosen to stay
  distinguishable in a 6px lane.
"""

from __future__ import annotations

from ..index.severity import Severity

SEVERITY_COLOURS: dict[int, str] = {
    Severity.NONE: "#8a8f98",
    Severity.TRACE: "#6b7280",
    Severity.DEBUG: "#7c8da6",
    Severity.INFO: "#4a9fd4",
    Severity.NOTICE: "#3fb27f",
    Severity.WARNING: "#d9a33c",
    Severity.ERROR: "#e06c4f",
    Severity.CRITICAL: "#d6365a",
}
"""
Ordered low to high as a temperature ramp.

Severity is the one thing a user scans for, so these are the only colours in the
view allowed to be saturated. WARNING through CRITICAL are separated in
lightness as well as hue, so they remain distinct under the common red-green
deficiencies and in a 2px minimap row where hue alone is unreliable.
"""

SEVERITY_BACKGROUNDS: dict[int, str] = {
    Severity.WARNING: "#2e2410",
    Severity.ERROR: "#331a14",
    Severity.CRITICAL: "#3a1220",
}
"""
Row tints, for the three classes worth finding by eye alone (FR-11).

Only the top three get a background. Tinting INFO too would mean every row is
tinted, which is the same as none being tinted.
"""

LANE_COLOURS: list[str] = [
    "#5aa9e6", "#f2a541", "#7fc29b", "#d98098",
    "#9b8ec4", "#58b8b0", "#c9a227", "#e0725c",
    "#6f9bd1", "#b5a06b", "#8fbf7f", "#cf8fb5",
    "#7d8fb3", "#4fa3a3", "#d4b483", "#b07d62",
]
"""
Qualitative, 16 entries. Beyond that the lanes compress and the alias column
(FR-29) becomes the primary cue, so cycling is acceptable -- a user with more
than 16 sources is not identifying them by colour anyway.
"""


def lane_colour(source_order: int) -> str:
    """
    The lane colour for a source, stable for the life of a session.

    Assigned by source order rather than by name hash: a user who opens the same
    three files in the same order expects the same three colours, and a hash
    would also make the palette non-reproducible in screenshots and bug reports.
    """
    return LANE_COLOURS[source_order % len(LANE_COLOURS)]


def severity_colour(level: int) -> str:
    return SEVERITY_COLOURS.get(int(level), SEVERITY_COLOURS[Severity.NONE])


def severity_background(level: int) -> str | None:
    return SEVERITY_BACKGROUNDS.get(int(level))
