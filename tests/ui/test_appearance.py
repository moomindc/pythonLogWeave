"""
What M4 added to the window -- FR-28, FR-29, FR-29a, FR-30, FR-38.

Text size, the two copy modes, the frozen right-hand columns, and the rules
reaching the paint path. The rule engine itself is tested headlessly in
`tests/unit/test_highlight.py`; what is tested here is that the view actually
uses it, and that the geometry and the painter agree with it.

The offscreen backend has no font database -- every family resolves empty and
every glyph is 13x13 -- so nothing here asserts a measured text width. Where a
colour has to be shown to reach the painter, it is recorded at `_colour` rather
than looked for in the pixels, because a glyph drawn by a backend with no fonts
is not something to build an assertion on. Backgrounds are `fillRect`, which the
backend does honour, so those are checked as pixels.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QGuiApplication,
    QImage,
    QPainter,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.highlight import (  # noqa: E402
    SEVERITY_SET,
    SHAPES_SET,
    SYSLOG_SET,
    HighlightRule,
    Ruleset,
)
from logweave.ui import settings, theme  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402
from logweave.ui.mergedview import (  # noqa: E402
    FONT_MAX,
    FONT_MIN,
    MARK_GUTTER,
    PAD,
    MergedLogView,
)


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def window(app, corpus: Path):
    w = MainWindow(background=False)
    w.resize(1200, 800)
    w.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert w.session.total_rows > 0
    yield w
    w.session.close()
    w.deleteLater()


@pytest.fixture
def view(window) -> MergedLogView:
    return window.view


def second_window() -> MainWindow:
    """
    Another window against the same preference store.

    `isolated_settings` is per test, so a window built inside a test reads back
    exactly what that test wrote -- which is the only way to check "remembered"
    without touching the real registry.
    """
    w = MainWindow(background=False)
    w.resize(800, 600)
    return w


def rendered(view: MergedLogView) -> QImage:
    img = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
    img.fill(0)
    view.viewport().render(img, QPoint(0, 0))
    return img


def colours_in(img: QImage) -> set[int]:
    return {img.pixel(x, y)
            for y in range(0, img.height(), 2)
            for x in range(0, img.width(), 3)}


def colours_asked_for(view: MergedLogView, monkeypatch) -> list[str]:
    """Every rule colour the painter requested during one frame."""
    seen: list[str] = []
    real = view._colour

    def recording(hex_str: str):
        seen.append(hex_str)
        return real(hex_str)

    monkeypatch.setattr(view, "_colour", recording)
    rendered(view)
    return seen


def argb(hex_str: str) -> int:
    return 0xFF000000 | int(hex_str.lstrip("#"), 16)


# ---------------------------------------------------------------------------
# text size (FR-38)
# ---------------------------------------------------------------------------

def test_the_default_size_is_what_the_application_ships(view):
    """"The default should be where we are today" -- M3's size, unchanged."""
    assert view.font_point == theme.FONT_POINT


def test_a_larger_size_makes_rows_taller_and_fewer(view):
    before_h, before_rows = view.row_height, view.visible_rows()
    view.set_font_point(view.font_point + 8)
    assert view.row_height > before_h
    assert view.visible_rows() < before_rows


def test_a_smaller_size_fits_at_least_as_many_rows(view):
    before = view.visible_rows()
    view.set_font_point(view.font_point - 3)
    assert view.visible_rows() >= before


def test_the_size_is_clamped_to_something_usable(view):
    """
    The store is a registry key or a text file someone may edit. A size of 0 or
    4000 would produce a window that cannot be used to fix itself.
    """
    view.set_font_point(-20)
    assert view.font_point == FONT_MIN
    view.set_font_point(5000)
    assert view.font_point == FONT_MAX


def test_zooming_keeps_the_row_you_were_looking_at(view):
    """
    The row, not the scroll offset: fewer rows fit at a larger size, so keeping
    the pixel offset would carry the content being read off the screen.
    """
    view.scroll_to_row(400)
    anchor = view.top_row()
    view.zoom(4)
    assert view.top_row() == anchor
    view.zoom(-4)
    assert view.top_row() == anchor


def test_the_scrollbar_range_follows_the_size(view):
    """
    The range is in rows, and how many fit changed -- so a zoom that left the
    maximum alone would make the last rows unreachable or scroll past the end.
    """
    view.set_font_point(8)
    small = view.verticalScrollBar().maximum()
    view.set_font_point(24)
    assert view.verticalScrollBar().maximum() > small


def test_reset_returns_to_the_shipped_size(view):
    view.set_font_point(20)
    view.reset_font()
    assert view.font_point == theme.FONT_POINT


def test_a_size_change_is_announced_once_and_only_when_it_changed(view):
    seen: list[int] = []
    view.fontPointChanged.connect(seen.append)

    view.set_font_point(view.font_point)
    assert seen == []

    view.set_font_point(14)
    view.set_font_point(14)
    assert seen == [14]


def test_the_size_is_remembered(view):
    """
    The point of FR-38 for someone who moves a window between a laptop and a
    desk monitor: set it once, not once a session.
    """
    view.set_font_point(15)
    assert settings.font_point() == 15

    again = second_window()
    try:
        assert again.view.font_point == 15
    finally:
        again.session.close()
        again.deleteLater()


def test_a_remembered_size_outside_the_range_is_ignored(view):
    """A hand-edited store must not be able to produce an unusable window."""
    settings.write(settings.FONT_POINT, 900)
    again = second_window()
    try:
        assert FONT_MIN <= again.view.font_point <= FONT_MAX
    finally:
        again.session.close()
        again.deleteLater()


def test_ctrl_wheel_zooms_and_a_plain_wheel_scrolls(view):
    """
    Ctrl+wheel because changing text size is something people do while reading,
    not from a menu -- and it must not also scroll, or the row being read moves
    as it resizes.
    """
    view.scroll_to_row(100)
    before_point, before_top = view.font_point, view.top_row()

    view.wheelEvent(QWheelEvent(
        QPoint(10, 10), view.mapToGlobal(QPoint(10, 10)), QPoint(0, 0),
        QPoint(0, 120), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False))
    assert view.font_point == before_point + 1
    assert view.top_row() == before_top

    view.wheelEvent(QWheelEvent(
        QPoint(10, 10), view.mapToGlobal(QPoint(10, 10)), QPoint(0, 0),
        QPoint(0, -120), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
    assert view.top_row() > before_top
    assert view.font_point == before_point + 1


def test_weight_is_dropped_rather_than_alignment(view):
    """
    Spans are positioned arithmetically from the character advance -- the one
    reason painting is independent of line length. A bold face a fraction wider
    would displace every span after the first by a growing fraction of a
    character, so the face is only used when the advance is unchanged.
    """
    base = view._faces[(False, False)]
    assert (view._face(True, False) is not base) == view._can_bold
    assert (view._face(False, True) is not base) == view._can_italic


def test_the_painter_is_handed_the_font_before_anything_is_drawn(view):
    """
    **A `QPainter` on a widget does not inherit that widget's font.** It starts
    at the application default, which on Windows is a 9pt proportional UI face.
    Until `paintEvent` set it explicitly, every message was drawn in that face
    while every width in the same method was computed from the monospace
    metrics -- so a zoom grew the row height and the character cell and did not
    change one glyph, which is how it was reported.

    Asserted by spying on the painter rather than by measuring pixels, because
    the offscreen backend has no font database and renders every glyph 13x13
    whatever the point size: the only backend that could show this in pixels is
    the one the bench uses.
    """
    view.set_font_point(19)

    calls: list[tuple[str, int]] = []
    real_font, real_text = QPainter.setFont, QPainter.drawText

    def spy_font(self, font):
        calls.append(("font", font.pointSize()))
        return real_font(self, font)

    def spy_text(self, *args):
        calls.append(("text", -1))
        return real_text(self, *args)

    try:
        QPainter.setFont, QPainter.drawText = spy_font, spy_text
        rendered(view)
    finally:
        QPainter.setFont, QPainter.drawText = real_font, real_text

    assert calls, "nothing was painted"
    assert calls[0] == ("font", 19)


def test_the_viewport_font_follows_a_zoom(view):
    """
    The other half of the same bug. A scroll area's viewport inherits the font
    when it is created and then stops -- with a style sheet on the window Qt
    treats the viewport's font as its own, so later propagation is ignored and
    `self.setFont` reached nothing that paints.
    """
    for point in (10, 18, 7):
        view.set_font_point(point)
        assert view.font().pointSize() == point
        assert view.viewport().font().pointSize() == point


def test_the_menu_has_the_three_zoom_actions(window):
    assert window.zoom_in_act.shortcuts()
    assert window.zoom_out_act.shortcuts()
    assert window.zoom_reset_act.shortcut().toString() == "Ctrl+0"

    window.zoom_in_act.trigger()
    assert window.view.font_point == theme.FONT_POINT + 1
    window.zoom_out_act.trigger()
    assert window.view.font_point == theme.FONT_POINT
    window.zoom_in_act.trigger()
    window.zoom_reset_act.trigger()
    assert window.view.font_point == theme.FONT_POINT


# ---------------------------------------------------------------------------
# copy, with and without attribution (D9, FR-46a)
# ---------------------------------------------------------------------------

def selected(view, lo: int, hi: int) -> None:
    view.set_current_row(lo)
    view.set_current_row(hi, extend=True)


def test_copy_with_attribution_names_the_file_and_line(window, view):
    """
    D9, in the user's own wording: "line 1 app.log line 7 SOA.log". The whole
    reason to merge files is to be able to say which one a row came from.
    """
    selected(view, 0, 3)
    view.copy_selection(annotated=True)
    text = QGuiApplication.clipboard().text()

    for r in window.session.rows(0, 4):
        name = Path(window.session.sources[r.source].meta.origin).name
        assert f"{name}:{r.line_no}" in text


def test_copy_plain_gives_the_lines_as_they_appear_in_their_own_files(window, view):
    """
    What you want when pasting into a parser, a diff, or a search box that
    would choke on an attribution prefix. Compared as one string rather than
    line by line, because an entry's own text may span several lines.
    """
    selected(view, 0, 3)
    view.copy_selection_plain()
    rows = window.session.rows(0, 4)
    assert QGuiApplication.clipboard().text() == "\n".join(r.text for r in rows)


def test_plain_text_carries_no_attribution(window, view):
    selected(view, 0, 2)
    view.copy_selection_plain()
    plain = QGuiApplication.clipboard().text()
    for s in window.session.sources:
        assert Path(s.meta.origin).name not in plain


def test_the_two_modes_differ_by_what_is_put_in_front(window, view):
    selected(view, 2, 2)
    view.copy_selection(annotated=False)
    plain = QGuiApplication.clipboard().text()
    view.copy_selection(annotated=True)
    annotated = QGuiApplication.clipboard().text()

    assert plain != annotated
    assert len(annotated) > len(plain)
    # The message survives into the annotated form, whatever precedes it.
    row = window.session.rows(2, 1)[0]
    assert row.message.splitlines()[0] in annotated


def test_both_copy_modes_report_how_many_rows_went(view):
    counts: list[int] = []
    view.copied.connect(counts.append)
    selected(view, 0, 2)

    view.copy_selection(annotated=True)
    view.copy_selection_plain()
    assert counts == [3, 3]


def test_both_copy_modes_are_in_the_edit_menu_with_their_own_shortcut(window):
    assert window.copy_plain_act.shortcut().toString() == "Ctrl+Shift+C"
    assert window.copy_act.shortcut() != window.copy_plain_act.shortcut()

    selected(window.view, 1, 1)
    window.copy_plain_act.trigger()
    row = window.session.rows(1, 1)[0]
    assert QGuiApplication.clipboard().text() == row.text


def test_the_plain_mode_is_capped_too(view, monkeypatch):
    """
    The cap is about the clipboard, not about the prefix, so dropping the
    attribution must not become a way around it.
    """
    import logweave.ui.mergedview as mv

    monkeypatch.setattr(mv, "COPY_ROW_LIMIT", 2)
    refused: list[int] = []
    view.copyRefused.connect(refused.append)
    selected(view, 0, 9)
    view.copy_selection_plain()
    assert refused == [10]


# ---------------------------------------------------------------------------
# the frozen right-hand columns (FR-28, FR-29, FR-29a)
# ---------------------------------------------------------------------------

def test_the_alias_and_line_number_columns_are_off_by_default(view):
    """
    The lanes already answer "which file" in less space. These are the fallback
    for too many sources, or for a screenshot that has to survive being printed
    in grey (FR-29, FR-29a).
    """
    assert view.show_alias is False
    assert view.show_line_no is False


def test_turning_a_column_on_takes_width_from_the_message(view):
    """
    They are frozen columns, so the room has to come from the part that
    scrolls -- which is the message (FR-36a).
    """
    before = view.message_width()

    view.set_show_alias(True)
    after_alias = view.message_width()
    assert after_alias < before

    view.set_show_line_no(True)
    assert view.message_width() < after_alias

    view.set_show_alias(False)
    view.set_show_line_no(False)
    assert view.message_width() == before


def test_the_columns_do_not_scroll_with_the_message(view):
    """
    FR-36a: the message's left edge and the width right of it are unmoved by a
    horizontal scroll, so a row's source is still readable at column 300 of a
    JSON record.
    """
    view.set_show_alias(True)
    view.set_show_line_no(True)
    rendered(view)

    left, width = view.message_left, view.message_width()
    bar = view.horizontalScrollBar()
    bar.setValue(bar.maximum())
    assert view.message_left == left
    assert view.message_width() == width


def test_the_alias_column_is_sized_from_the_sources_not_the_visible_rows(view):
    """
    A column that resized as you scrolled past a file with a longer name would
    reflow every row on a wheel step.
    """
    view.set_show_alias(True)
    rendered(view)                 # settles the layout, so the widths are real
    alias_w, width = view._alias_w, view.message_width()
    assert alias_w > 0

    view.scroll_to_row(view.session.total_rows - 1)
    rendered(view)
    assert view._alias_w == alias_w
    assert view.message_width() == width


def test_a_longer_alias_widens_the_column(window, view):
    """The column is measured from the aliases, so a rename has to remeasure."""
    view.set_show_alias(True)
    before = view.message_width()
    window.rename_source(0, "a-considerably-longer-name")
    assert view.message_width() < before


def test_the_line_number_column_is_on_the_left(view):
    """
    Where every editor gutter, every `grep -n` and every compiler error puts
    it. It was built on the right next to the alias, which is wrong: the alias
    answers "which file", the line number is a coordinate within the row and
    belongs at the start of it.
    """
    right_before = view._right_area()
    left_before = view.message_left

    view.set_show_line_no(True)
    assert view._line_no_w > 0
    assert view.message_left > left_before      # it took width from the left
    assert view._right_area() == right_before   # and none from the right


def test_the_line_number_sits_before_the_timestamp(view):
    view.set_show_line_no(True)
    assert view._left_gutter == MARK_GUTTER + PAD + view._line_no_w
    assert view.message_left == view._left_gutter + view._ts_w


def test_the_line_number_column_keeps_its_place_when_the_timestamp_goes(view):
    """Hiding the timestamp must close the gap, not leave the numbers floating."""
    view.set_show_line_no(True)
    view.set_show_timestamp(False)
    assert view.message_left == view._left_gutter
    assert view._left_gutter == MARK_GUTTER + PAD + view._line_no_w


def test_the_line_number_column_is_painted_in_the_left_gutter(view):
    """
    The geometry above would also be satisfied by a column that reserved the
    width and drew nothing, so this looks for ink in exactly that band.
    """
    view.set_show_line_no(True)
    lo, hi = MARK_GUTTER + PAD, view._left_gutter - PAD
    assert hi > lo

    img = rendered(view)
    dim = argb(theme.DIM)
    assert any(img.pixel(x, y) == dim
               for x in range(lo, hi)
               for y in range(0, min(120, img.height())))


def test_lane_width_is_configurable_within_the_specified_range(view):
    view.set_lane_width(12)
    assert view.lane_width == 12
    view.set_lane_width(4)
    assert view.lane_width == 4

    view.set_lane_width(1)
    assert view.lane_width == theme.LANE_WIDTH_MIN
    view.set_lane_width(99)
    assert view.lane_width == theme.LANE_WIDTH_MAX


def test_a_wider_lane_leaves_less_room_for_the_message(view):
    view.set_lane_width(4)
    narrow = view.message_width()
    view.set_lane_width(12)
    assert view.message_width() < narrow


def test_the_lanes_compress_once_there_are_too_many_sources(view, monkeypatch):
    """
    SPEC 9.2: past about twenty sources the gutter would take more width than
    the messages, and the colours stop being distinguishable anyway. The alias
    column is then the way to tell files apart, which is what it is for.
    """
    view.set_lane_width(12)
    assert view.lane_pitch == 12

    monkeypatch.setattr(theme, "LANE_COMPRESS_ABOVE", 1)
    assert view.lane_pitch == theme.LANE_WIDTH_COMPRESSED


def test_the_lane_width_is_remembered(window):
    window._set_lane_width(10)
    assert settings.lane_width() == 10

    again = second_window()
    try:
        assert again.view.lane_width == 10
    finally:
        again.session.close()
        again.deleteLater()


def test_the_column_toggles_are_remembered(window):
    window.alias_act.setChecked(True)
    window.line_no_act.setChecked(True)

    again = second_window()
    try:
        assert again.view.show_alias is True
        assert again.view.show_line_no is True
        assert again.alias_act.isChecked() is True
        assert again.line_no_act.isChecked() is True
    finally:
        again.session.close()
        again.deleteLater()


def test_the_menu_and_the_view_never_disagree(window):
    """
    The actions read their checked state from the view after the preferences
    are applied, one direction only -- so there is no moment where the tick and
    the column are different.
    """
    assert window.timestamp_act.isChecked() == window.view.show_timestamp
    assert window.alias_act.isChecked() == window.view.show_alias
    assert window.line_no_act.isChecked() == window.view.show_line_no
    assert [a.isChecked() for a in window.lane_acts].count(True) == 1


# ---------------------------------------------------------------------------
# the rules reaching the paint path (FR-30)
# ---------------------------------------------------------------------------

def test_the_view_ships_with_the_default_rules_and_none_rejected(view):
    assert view.highlights.rules
    assert view.highlights.rejected == []


def test_a_rule_colour_reaches_the_painter(view, monkeypatch):
    """
    The wire between the engine and the screen. Recorded at `_colour` rather
    than read out of the pixels because the offscreen backend has no fonts;
    everything else about the rules is tested headlessly.
    """
    view.highlights = Ruleset([
        HighlightRule(name="everything", pattern=" ", fg="#ff00ff"),
    ])
    assert "#ff00ff" in colours_asked_for(view, monkeypatch)


def test_with_no_rules_no_rule_colour_is_requested(view, monkeypatch):
    """
    The other half of the previous test: without it, a painter that asked for
    some colour unconditionally would pass.
    """
    view.highlights = Ruleset([])
    assert colours_asked_for(view, monkeypatch) == []


def test_a_rule_background_tints_the_row(view):
    """`fillRect`, which the offscreen backend does honour -- so this is pixels."""
    view.highlights = Ruleset([
        HighlightRule(name="tint", pattern=" ", bg="#00ff00"),
    ])
    assert argb("#00ff00") in colours_in(rendered(view))


def test_a_rule_background_beats_the_severity_tint(view):
    """
    The user wrote the rule to make these rows stand out; the severity tint is
    a default they are overriding.
    """
    from logweave.index.severity import Severity
    from logweave.session import palette as pal

    view.highlights = Ruleset([
        HighlightRule(name="tint", pattern="ERROR", bg="#00ff00"),
    ])
    found = colours_in(rendered(view))
    assert argb("#00ff00") in found
    error_tint = pal.severity_background(int(Severity.ERROR))
    if error_tint:
        assert argb(error_tint) not in found


def test_with_highlighting_off_the_view_looks_as_it_did_at_m3(view):
    """
    Highlighting is additive. Turning every rule off has to leave severity
    still deciding the colours, not leave the view uncoloured.
    """
    from logweave.index.severity import Severity
    from logweave.session import palette as pal

    view.highlights = Ruleset([])
    found = colours_in(rendered(view))
    marks = {argb(pal.severity_colour(int(s)))
             for s in (Severity.ERROR, Severity.WARNING, Severity.INFO)}
    assert found & marks


def test_both_a_span_and_the_rest_of_its_line_are_painted(view, monkeypatch):
    """
    The segmented draw. Overdrawing a styled row measured +10.60 ms a frame on
    a span-heavy file; drawing each character exactly once brought it to
    +2.55 ms. What is asserted here is the visible consequence -- the span's
    colour and the line's both reach the painter in the same frame.
    """
    view.highlights = Ruleset([
        HighlightRule(name="line", pattern="INFO", fg="#2222ff"),
        HighlightRule(name="span", pattern="worker", scope="match",
                      fg="#ffff22", priority=5),
    ])
    asked = colours_asked_for(view, monkeypatch)
    assert "#2222ff" in asked
    assert "#ffff22" in asked


def test_a_span_rule_alone_leaves_the_line_in_its_severity_colour(view, monkeypatch):
    view.highlights = Ruleset([
        HighlightRule(name="span", pattern="worker", scope="match", fg="#ffff22"),
    ])
    asked = colours_asked_for(view, monkeypatch)
    assert asked.count("#ffff22") >= 1
    assert set(asked) == {"#ffff22"}


def test_each_visible_row_is_styled_once_a_frame(view, monkeypatch):
    """
    SPEC 10: the background pass and the text pass both need the style, and
    styling twice would double the regex work for nothing. The scan is also
    memoised by text, so repeated lines cost one scan between them.
    """
    calls: list[str] = []
    real = view.highlights.style_of

    def counting(text: str):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(view.highlights, "style_of", counting)
    rendered(view)
    assert view._last_painted > 0
    assert len(calls) == view._last_painted


def test_switching_a_shipped_ruleset_off_changes_the_active_rules(window):
    before = len(window.view.highlights.rules)
    window.highlight_acts[SHAPES_SET].setChecked(False)
    assert len(window.view.highlights.rules) < before
    assert "off" in window._status_msg.text()

    window.highlight_acts[SHAPES_SET].setChecked(True)
    assert len(window.view.highlights.rules) == before


def test_the_syslog_set_ships_off(window):
    """
    `<3>` at the start of a line is rare outside a raw syslog capture, and a
    rule that almost never fires still costs a branch in the one regex every
    row is scanned with.
    """
    assert window.highlight_acts[SYSLOG_SET].isChecked() is False
    assert SYSLOG_SET not in window.highlight_group.active

    window.highlight_acts[SYSLOG_SET].setChecked(True)
    assert SYSLOG_SET in window.highlight_group.active


def test_the_active_rulesets_are_remembered(window):
    window.highlight_acts[SHAPES_SET].setChecked(False)

    again = second_window()
    try:
        assert again.highlight_group.active == {SEVERITY_SET}
        assert again.highlight_acts[SHAPES_SET].isChecked() is False
    finally:
        again.session.close()
        again.deleteLater()


def test_an_unknown_remembered_ruleset_is_ignored(window):
    """A set dropped in a later version must not come back as a name nothing answers to."""
    settings.write(settings.HIGHLIGHTS, "severity,a-set-that-never-existed")

    again = second_window()
    try:
        assert again.highlight_group.active == {SEVERITY_SET}
        assert again.view.highlights.rejected == []
    finally:
        again.session.close()
        again.deleteLater()


def test_every_set_off_is_a_legitimate_state(window):
    for act in window.highlight_acts.values():
        act.setChecked(False)
    assert window.view.highlights.rules == []
    rendered(window.view)      # must not raise

    again = second_window()
    try:
        assert again.highlight_group.active == set()
        assert again.view.highlights.rules == []
    finally:
        again.session.close()
        again.deleteLater()


# ---------------------------------------------------------------------------
# renaming a source from the dock (FR-29)
# ---------------------------------------------------------------------------

def test_a_source_can_be_renamed_from_the_dock(window):
    window.rename_source(0, "front-end")
    assert window.session.sources[0].alias == "front-end"
    assert "front-end" in window.source_list.item(0).text()
    assert "renamed" in window._status_msg.text()


def test_the_dock_row_still_shows_the_entry_count_after_a_rename(window):
    """
    `QListWidgetItem` keeps DisplayRole and EditRole in one place, so a naive
    rename would offer "app   504" to the editor and take back whatever was
    left behind. The delegate is what separates them.
    """
    count = f"{window.session.sources[0].index.n:,}"
    window.rename_source(0, "fe")
    text = window.source_list.item(0).text()
    assert "fe" in text
    assert count in text


def test_the_delegate_edits_the_alias_alone(window):
    from PySide6.QtWidgets import QLineEdit

    delegate = window.source_list.itemDelegate()
    editor = QLineEdit()
    index = window.source_list.model().index(0, 0)
    delegate.setEditorData(editor, index)
    assert editor.text() == window.session.sources[0].alias

    editor.setText("renamed-via-delegate")
    delegate.setModelData(editor, window.source_list.model(), index)
    assert window.session.sources[0].alias == "renamed-via-delegate"


def test_clearing_the_name_in_the_dock_restores_the_default(window):
    original = window.session.sources[0].alias
    window.rename_source(0, "temporary")
    window.rename_source(0, "")
    assert window.session.sources[0].alias == original
    assert "reset" in window._status_msg.text()


def test_renaming_does_not_disturb_the_tick_boxes(window):
    window.session.set_enabled(1, False)
    window.refresh()
    window.rename_source(0, "renamed")

    states = [window.source_list.item(i).checkState()
              for i in range(window.source_list.count())]
    assert states[0] == Qt.CheckState.Checked
    assert states[1] == Qt.CheckState.Unchecked


def test_renaming_a_source_that_does_not_exist_is_ignored(window):
    window.rename_source(99, "nowhere")
    assert window.source_list.count() == len(window.session.sources)


def test_a_rename_shows_up_in_the_rows_tooltip(window):
    window.rename_source(0, "the-front-end")
    assert "the-front-end" in window.source_list.item(0).toolTip()
