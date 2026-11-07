"""The palette -- SPEC 9.2, 10.2. Qt-free, so it is checked headlessly."""

from __future__ import annotations

import pytest

from logweave.index.severity import Severity
from logweave.session import palette


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(hex_colour: str) -> float:
    r, g, b = _rgb(hex_colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_every_severity_class_has_a_colour():
    """A missing one would paint an entry invisible, not merely wrong."""
    for level in Severity:
        assert level in palette.SEVERITY_COLOURS


def test_severity_colours_are_well_formed_hex():
    for colour in list(palette.SEVERITY_COLOURS.values()) + palette.LANE_COLOURS:
        assert colour.startswith("#") and len(colour) == 7
        int(colour[1:], 16)


def test_the_three_loud_classes_differ_in_lightness_as_well_as_hue():
    """
    SPEC 9.2 claims WARNING/ERROR/CRITICAL stay distinct under red-green
    deficiency and in a 2px minimap row. Hue alone would not do that, so the
    claim is only true if their luminances are separated -- assert the claim.
    """
    loud = [Severity.WARNING, Severity.ERROR, Severity.CRITICAL]
    lums = sorted(_luminance(palette.SEVERITY_COLOURS[s]) for s in loud)
    for a, b in zip(lums[:-1], lums[1:], strict=True):
        assert b - a > 10, f"luminances too close: {lums}"


def test_only_the_top_three_classes_get_a_row_tint():
    """Tinting INFO too would mean every row is tinted, which is no tinting."""
    assert set(palette.SEVERITY_BACKGROUNDS) == {
        Severity.WARNING, Severity.ERROR, Severity.CRITICAL
    }


def test_row_tints_are_dark_enough_to_read_text_over():
    for colour in palette.SEVERITY_BACKGROUNDS.values():
        assert _luminance(colour) < 60, f"{colour} is too bright for a row tint"


def test_lane_colour_is_stable_for_a_given_position():
    """
    Assigned by source order, not by name hash (SPEC 9.2): reopening the same
    files in the same order must give the same colours, in a screenshot too.
    """
    assert palette.lane_colour(0) == palette.lane_colour(0)
    assert palette.lane_colour(0) != palette.lane_colour(1)


def test_lane_colours_cycle_rather_than_running_out():
    n = len(palette.LANE_COLOURS)
    assert palette.lane_colour(n) == palette.lane_colour(0)
    assert palette.lane_colour(n * 3 + 5) == palette.lane_colour(5)


def test_the_first_lane_colours_are_mutually_distinguishable():
    """
    The common case is a handful of files, so the first few must not collide.

    A crude distance in RGB rather than a perceptual model -- enough to catch
    two near-identical blues being adjacent in the list, which is the mistake
    that actually happens when hand-picking a palette.
    """
    first = [_rgb(c) for c in palette.LANE_COLOURS[:8]]
    for i, a in enumerate(first):
        for b in first[i + 1:]:
            dist = sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5
            assert dist > 40, f"{a} and {b} are too close"


@pytest.mark.parametrize("level", list(Severity))
def test_severity_colour_accepts_an_int_as_well_as_the_enum(level):
    assert palette.severity_colour(int(level)) == palette.SEVERITY_COLOURS[level]


def test_an_unknown_severity_falls_back_rather_than_raising():
    """`severity` comes out of a uint8 array; a bad value must not kill a frame."""
    assert palette.severity_colour(99) == palette.SEVERITY_COLOURS[Severity.NONE]
    assert palette.severity_background(99) is None
