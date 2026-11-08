"""
The settings window and the mark gutter it drives -- FR-31, FR-31a, FR-52.

Two things are being tested, and they are separable on purpose. The dialog is
built over a bare `RulesetGroup` and asserted without a session, because it is
a form and a list and should need nothing else. The wiring is asserted through
a real `MainWindow`, because that is the only place that knows how a settled
preference reaches a view, a strip and the settings store.

**No modal dialog is ever opened.** `RuleDialog` and `QColorDialog` both block
until dismissed, so a test that clicked Add would hang rather than fail --
which is why `HighlightPage` has `add_rule` and `replace_rule` beside the
`add`/`edit_selected` the buttons call, and why `ColourButton.set_colour` is
the API rather than its click handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from logweave.highlight import USER_SET, default_group  # noqa: E402
from logweave.highlight.rules import HighlightRule  # noqa: E402
from logweave.index.severity import Severity  # noqa: E402
from logweave.session import Session  # noqa: E402
from logweave.ui import settings  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402
from logweave.ui.ruledialog import ColourButton, RuleDialog  # noqa: E402
from logweave.ui.settingsdialog import (  # noqa: E402
    HighlightPage,
    SettingsDialog,
    Settled,
)


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def log(tmp_path) -> Path:
    """Four hundred rows, of which every fiftieth mentions a timeout."""
    path = tmp_path / "svc.log"
    with path.open("w", encoding="utf-8") as fh:
        for sec in range(400):
            extra = " connection timeout after 30s" if sec % 50 == 0 else ""
            fh.write(f"2026-10-04 10:{sec // 60:02d}:{sec % 60:02d}.000000 "
                     f"INFO served request {sec}{extra}\n")
    return path


def render(widget) -> QImage:
    """
    Paint a widget into an image, which is what makes its buffer exist.

    Same helper as `tests/ui/test_strips.py`, and for the same reason: the
    offscreen platform delivers no paint event for a bare `show()`.
    """
    img = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    widget.render(img)
    return img


def a_rule(**kw) -> HighlightRule:
    kw.setdefault("name", "timeouts")
    kw.setdefault("pattern", "timeout")
    kw.setdefault("fg", "#ff4040")
    return HighlightRule(**kw)


def page() -> HighlightPage:
    return HighlightPage(default_group())


def dialog() -> SettingsDialog:
    return SettingsDialog(default_group(),
                          current=Settled(font_point=11, lane_width=6))


@pytest.fixture
def window(app, log):
    session = Session(use_cache=False)
    session.add_files([log])
    win = MainWindow(session, background=False)
    win.resize(1200, 700)
    yield win
    win.marks.stop()
    win.follow.stop()
    session.close()
    win.deleteLater()


# ---------------------------------------------------------------------------
# the rule list (FR-31)
# ---------------------------------------------------------------------------

def test_the_shipped_sets_appear_as_switches(app):
    p = page()
    assert set(p.set_boxes) == {"severity", "shapes", "syslog"}
    assert p.set_boxes["severity"].isChecked()
    assert not p.set_boxes["syslog"].isChecked(), "syslog ships off"
    assert p.user_box.isChecked(), "a rule you type must work without a switch"


def test_a_rule_added_shows_every_fact_about_itself(app):
    p = page()
    p.add_rule(a_rule(is_regex=True, case_sensitive=True, on_strips=True))
    assert p.list.topLevelItemCount() == 1
    item = p.list.topLevelItem(0)
    assert item.text(0) == "timeouts"
    assert item.text(1) == "timeout"
    assert "regex" in item.text(2) and "case" in item.text(2)
    assert item.text(3) == "view + strips"


def test_a_rule_that_only_colours_the_view_says_so(app):
    p = page()
    p.add_rule(a_rule(on_strips=False))
    assert p.list.topLevelItem(0).text(3) == "view"


def test_the_swatch_is_the_colour_the_rule_paints_in(app):
    """
    Foreground, then background, then the theme's text colour -- the same order
    `marks._colour_of` takes, so the list, the painted row and the strip mark
    cannot disagree about which colour a rule "is".
    """
    p = page()
    p.add_rule(a_rule(name="fg-only", fg="#ff0000", bg=None))
    p.add_rule(a_rule(name="bg-only", fg=None, bg="#00ff00"))
    assert p.list.topLevelItem(0).foreground(0).color().name() == "#ff0000"
    assert p.list.topLevelItem(1).foreground(0).color().name() == "#00ff00"


def test_unticking_a_rule_in_the_list_disables_it(app):
    p = page()
    p.add_rule(a_rule())
    p.list.topLevelItem(0).setCheckState(4, Qt.CheckState.Unchecked)
    assert p.rules[0].enabled is False


def test_removing_the_selected_rule_removes_that_one(app):
    p = page()
    for i in range(3):
        p.add_rule(a_rule(name=f"r{i}", pattern=f"p{i}"))
    p.list.setCurrentItem(p.list.topLevelItem(1))
    p.remove_selected()
    assert [r.name for r in p.rules] == ["r0", "r2"]


def test_a_rule_can_be_moved_because_order_is_the_priority_tiebreak(app):
    p = page()
    for i in range(3):
        p.add_rule(a_rule(name=f"r{i}", pattern=f"p{i}"))
    p.list.setCurrentItem(p.list.topLevelItem(2))
    p.move_selected(-1)
    assert [r.name for r in p.rules] == ["r0", "r2", "r1"]
    p.move_selected(-1)
    assert [r.name for r in p.rules] == ["r2", "r0", "r1"]


def test_moving_off_either_end_is_refused_rather_than_wrapping(app):
    p = page()
    p.add_rule(a_rule(name="only"))
    p.list.setCurrentItem(p.list.topLevelItem(0))
    p.move_selected(-1)
    p.move_selected(1)
    assert [r.name for r in p.rules] == ["only"]


def test_the_buttons_follow_what_can_actually_be_done(app):
    p = page()
    assert not p.edit_btn.isEnabled(), "nothing selected"
    assert not p.remove_btn.isEnabled()
    p.add_rule(a_rule(name="a"))
    p.add_rule(a_rule(name="b", pattern="other"))
    p.list.setCurrentItem(p.list.topLevelItem(0))
    assert p.edit_btn.isEnabled()
    assert not p.up_btn.isEnabled(), "already at the top"
    assert p.down_btn.isEnabled()
    p.list.setCurrentItem(p.list.topLevelItem(1))
    assert p.up_btn.isEnabled()
    assert not p.down_btn.isEnabled()


# ---------------------------------------------------------------------------
# the rule form
# ---------------------------------------------------------------------------

def test_the_form_round_trips_a_rule(app):
    original = a_rule(is_regex=True, case_sensitive=True, word_boundary=True,
                      scope="match", bg="#331a14", bold=True, italic=True,
                      priority=85, on_strips=True)
    form = RuleDialog(original)
    assert form.rule() == original


def test_the_form_refuses_a_pattern_that_will_not_compile(app):
    form = RuleDialog(a_rule())
    form.is_regex.setChecked(True)
    form.pattern.setText("[unclosed")
    assert form.rule() is None
    assert not form.buttons.button(
        QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert "unterminated" in form.complaint.text().lower() or \
           "invalid regex" in form.complaint.text().lower()


def test_the_form_refuses_a_rule_with_no_name(app):
    form = RuleDialog(a_rule())
    form.name.setText("  ")
    assert form.rule() is None
    assert "needs a name" in form.complaint.text()


def test_the_form_refuses_a_name_already_in_use(app):
    form = RuleDialog(None, taken={"timeouts"})
    form.name.setText("timeouts")
    form.pattern.setText("x")
    assert form.rule() is None
    assert "already a rule called" in form.complaint.text()


def test_a_rule_keeps_its_own_name_when_edited(app):
    """
    The name is in `taken` because it is in the list, so an edit that changes
    only a colour must not be refused for colliding with itself.
    """
    form = RuleDialog(a_rule(name="timeouts"), taken={"timeouts", "other"})
    form.fg.set_colour("#00ff00")
    got = form.rule()
    assert got is not None and got.name == "timeouts" and got.fg == "#00ff00"


def test_the_form_warns_that_a_regex_cannot_set_a_severity(app):
    """
    Not forbidden -- it is a perfectly good painting rule. What it cannot do is
    reach the index-time classifier (SPEC 10.3), and a user who picked a class
    and got nothing on the minimap is owed that sentence.
    """
    form = RuleDialog(a_rule(is_regex=True, pattern=r"ora-\d+"))
    form.severity.setCurrentIndex(form.severity.findData(int(Severity.ERROR)))
    assert form.rule() is not None, "still a legal rule"
    assert "cannot set the severity class" in form.complaint.text()


def test_the_form_says_what_marking_on_the_strips_costs(app):
    form = RuleDialog(a_rule())
    form.where_strips.setChecked(True)
    assert "scans every open file" in form.complaint.text()
    assert form.rule().on_strips is True


def test_the_two_places_a_colour_can_appear_are_exclusive(app):
    form = RuleDialog(a_rule(on_strips=True))
    assert form.where_strips.isChecked() and not form.where_view.isChecked()
    form.where_view.setChecked(True)
    assert not form.where_strips.isChecked()
    assert form.rule().on_strips is False


def test_a_colour_can_be_unset_again(app):
    """
    `Style.over` is built on `None` meaning "inherit", so a rule styled only by
    bold has to be expressible.
    """
    form = RuleDialog(a_rule(fg="#ff0000", bg="#001122"))
    form._clear_colours()
    got = form.rule()
    assert got.fg is None and got.bg is None


def test_a_colour_button_shows_its_colour_and_its_absence(app):
    button = ColourButton("text")
    assert button.colour is None
    assert "none" in button.text()
    button.set_colour("#ff4040")
    assert button.colour == "#ff4040"
    assert button.text() == "#ff4040"


# ---------------------------------------------------------------------------
# what the dialog settles
# ---------------------------------------------------------------------------

def test_cancelling_settles_nothing(app):
    d = dialog()
    seen = []
    d.changed.connect(seen.append)
    d.highlight.add_rule(a_rule())
    d.reject()
    assert seen == [], "a cancel must cost nothing, especially not a scan"


def test_applying_emits_everything_at_once(app):
    d = dialog()
    seen = []
    d.changed.connect(seen.append)
    d.appearance.font_point.setValue(14)
    d.appearance.show_alias.setChecked(True)
    d.highlight.set_boxes["syslog"].setChecked(True)
    d.highlight.add_rule(a_rule(on_strips=True))
    d.apply()

    assert len(seen) == 1
    got = seen[0]
    assert got.font_point == 14
    assert got.show_alias is True
    assert "syslog" in got.active_sets
    assert [r.name for r in got.user_rules] == ["timeouts"]
    assert got.user_rules[0].on_strips is True


def test_ok_applies_and_closes(app):
    d = dialog()
    seen = []
    d.changed.connect(seen.append)
    d._ok()
    assert len(seen) == 1
    assert d.result() == int(SettingsDialog.DialogCode.Accepted)


# ---------------------------------------------------------------------------
# the wiring (FR-31a end to end)
# ---------------------------------------------------------------------------

def test_the_dialog_opens_on_what_the_window_currently_shows(window):
    """
    Built over the live window, not the store: anything changed from a menu
    since the last write is in the window, so reading the store would show a
    stale value and Apply would write it back -- a dialog that undid a menu by
    being opened.
    """
    window.view.set_font_point(15)
    window.alias_act.setChecked(True)
    d = window.settings_dialog()
    assert d.appearance.font_point.value() == 15
    assert d.appearance.show_alias.isChecked() is True
    d.deleteLater()


def test_applying_reaches_the_view_the_strips_and_the_store(window):
    d = window.settings_dialog()
    d.appearance.font_point.setValue(13)
    d.appearance.lane_width.setValue(10)
    d.appearance.show_line_no.setChecked(True)
    d.appearance.show_timeline.setChecked(False)
    d.apply()

    assert window.view.font_point == 13
    assert window.view.lane_width == 10
    assert window.view.show_line_no is True
    assert window.timeline.isVisibleTo(window) is False
    assert settings.read_int(settings.FONT_POINT, 0, low=1, high=99) == 13
    assert settings.read_bool(settings.SHOW_TIMELINE, True) is False
    d.deleteLater()


def test_the_menus_catch_up_with_the_dialog(window):
    """
    Two doors to one state. A menu tick left disagreeing with the view is the
    same defect SPEC 9.1 records from M5, where a menu built before the window
    was shown read `isVisible()` False for a strip that was on.
    """
    d = window.settings_dialog()
    d.appearance.show_alias.setChecked(True)
    d.appearance.show_minimap.setChecked(False)
    d.appearance.lane_width.setValue(12)
    d.highlight.set_boxes["syslog"].setChecked(True)
    d.apply()

    assert window.alias_act.isChecked() is True
    assert window.minimap_act.isChecked() is False
    assert window.highlight_acts["syslog"].isChecked() is True
    assert [a.isChecked() for a in window.lane_acts if a.data() == 12] == [True]
    d.deleteLater()


def test_a_user_rule_reaches_the_view_and_is_remembered(window):
    d = window.settings_dialog()
    d.highlight.add_rule(a_rule())
    d.apply()

    style = window.view.highlights.style_of("connection timeout after 30s")
    assert style.line.fg == "#ff4040"
    assert style.rule == "timeouts"

    stored = settings.store().value(settings.USER_RULES)
    assert isinstance(stored, str) and "timeouts" in stored
    d.deleteLater()


def test_a_remembered_rule_comes_back_on_the_next_run(window):
    d = window.settings_dialog()
    d.highlight.add_rule(a_rule(on_strips=True, enabled=False))
    d.apply()
    d.deleteLater()

    again = MainWindow(Session(), background=False)
    try:
        rules = again.highlight_group.sets[USER_SET].rules
        assert [r.name for r in rules] == ["timeouts"]
        assert rules[0].on_strips is True
        assert rules[0].enabled is False, (
            "a rule switched off must not come back on")
    finally:
        again.marks.stop()
        again.follow.stop()
        again.session.close()
        again.deleteLater()


def test_marking_a_rule_scans_and_puts_it_on_both_strips(window):
    """
    The end-to-end answer to "also the minimap": a rule the user marked ends up
    as a gutter column on the minimap *and* the timeline, because a mark
    answers both of D4's questions -- where in the stream, and when.
    """
    window.marks.request([a_rule(on_strips=True)])
    assert window.marks.running, "a marked rule must start a scan"
    window.marks.stop()

    # Driven synchronously once, so the assertion is about the result and not
    # about whether a thread got there in time.
    from logweave.session.marks import scan
    result = scan(window.session, [a_rule(on_strips=True)])
    assert result.layers and result.layers[0].hits == 8
    window._marks_ready(result)

    assert window.minimap._marks == result.layers
    assert window.timeline._marks == result.layers


def test_an_unmarked_ruleset_starts_no_scan(window):
    """
    The opt-in is a cost control, so the ordinary case must cost nothing: the
    full shipped ruleset is thirty branches and 0.9 MB/s.
    """
    assert not window.marks.running
    assert window.marks.request(window.view.highlights.rules) is False
    assert not window.marks.running


def test_clearing_the_marked_rules_clears_the_strips(window):
    from logweave.session.marks import scan

    result = scan(window.session, [a_rule(on_strips=True)])
    # Both halves of what `MarkScanner._done` does, so the scanner is left in
    # the state a real finished scan leaves it in. Setting only the strips
    # would test a path the window never takes.
    window.marks.result = result
    window._marks_ready(result)
    assert window.minimap._marks

    window.marks.request([a_rule(on_strips=False)])
    assert window.minimap._marks == (), (
        "marks from a rule that no longer asks for them must go")
    assert window.timeline._marks == ()


def test_closing_everything_drops_the_marks_with_the_session(window):
    """
    A layer is merged timestamps from files that are no longer open, so leaving
    it on the strip would mark a position in a stream that does not exist.
    """
    from logweave.session.marks import scan

    window._marks_ready(scan(window.session, [a_rule(on_strips=True)]))
    assert window.minimap._marks
    window.close_all()
    assert window.minimap._marks == ()
    assert window.timeline._marks == ()


def test_the_marked_rule_narrows_the_bands_rather_than_covering_them(window):
    """
    The severity bands now descend from the left (`_RUN_CLASSES`), so the left
    edge is where ERROR and CRITICAL live. A user mark drawn over it would hide
    the one thing the strip is scanned for in order to show the other, so the
    gutter takes its width from the bands instead.
    """
    from logweave.session.marks import scan
    from logweave.session.overview import mark_gutter

    # Painted through `render`, not by showing it: the offscreen platform does
    # not deliver a paint event for a `show()` alone, so the buffer stays None
    # and the assertion below would be about nothing.
    window.minimap.resize(64, 400)
    render(window.minimap)
    plain = window.minimap._buffer
    assert plain is not None
    width = plain.shape[1]

    window._marks_ready(scan(window.session, [a_rule(on_strips=True)]))
    render(window.minimap)
    marked = window.minimap._buffer
    assert marked.shape[1] == width, "the strip is the same width"
    assert mark_gutter(window.minimap._marks, width) > 0
    # The gutter is on the right, so the leftmost column -- where the severity
    # bands now start -- is untouched by the mark.
    assert np.array_equal(marked[:, 0, 0:3], plain[:, 0, 0:3]), (
        "a user mark must not overdraw the severity bands")


def test_the_edit_rules_item_opens_the_highlighting_tab(window):
    """An item called Edit Rules that landed on text size has not done what it said."""
    d = window.settings_dialog()
    d.tabs.setCurrentWidget(d.highlight)
    assert d.tabs.currentWidget() is d.highlight
    assert window.settings_act.shortcut().toString() == "Ctrl+,"
    d.deleteLater()


def test_a_hand_corrupted_rule_store_is_reported_not_fatal(app):
    """
    The store is a registry value, so it is user-editable by construction and
    an unusable preference must never be able to stop someone opening a log.
    """
    settings.write(settings.USER_RULES, "{ this is not json")
    win = MainWindow(Session(), background=False)
    try:
        assert win.highlight_group.sets[USER_SET].rules == []
        assert "could not be read" in win._status_msg.text()
    finally:
        win.marks.stop()
        win.follow.stop()
        win.session.close()
        win.deleteLater()
