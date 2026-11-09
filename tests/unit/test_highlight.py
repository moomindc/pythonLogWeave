"""
The highlight rule engine -- FR-30, FR-31, SPEC 10.1.

Three properties carry the design, and the tests are grouped around them.

**One regex, whatever the rule count.** Fifty rules must cost one scan, not
fifty, or rule count lands in the frame budget.

**The result depends only on the rules, never on draw order.** Two rules
matching the same characters have to resolve by priority and rule order, and a
span must never be painted twice -- otherwise the colour a user sees depends on
something they cannot see or change.

**A bad rule is reported, not raised.** Half-typed regexes are the normal state
of a rule being edited, and one of them must not stop the other forty-nine.
"""

from __future__ import annotations

import pytest

from logweave.highlight import (
    SEVERITY_SET,
    SHAPES_SET,
    SYSLOG_SET,
    USER_SET,
    HighlightRule,
    Ruleset,
    Style,
    default_group,
    default_ruleset,
)
from logweave.highlight.builtins import _syslog_rules
from logweave.highlight.rules import SCAN_LIMIT
from logweave.index.severity import Severity


def rule(name: str, pattern: str, **kw) -> HighlightRule:
    return HighlightRule(name=name, pattern=pattern, **kw)


# ---------------------------------------------------------------------------
# one regex, one pass
# ---------------------------------------------------------------------------

def test_all_rules_compile_into_a_single_pattern():
    """
    SPEC 10.1: a line is scanned once however many rules there are. Tested by
    counting the scans, because that is the property -- not by reading the
    pattern, which would pass just as well with fifty of them.
    """
    rs = Ruleset([rule(f"r{i}", f"word{i}", fg="#111111") for i in range(50)])
    rs.style_of("prime the compiler")

    scans = 0
    compiled = rs._compiled

    class Counting:
        def finditer(self, *args, **kwargs):
            nonlocal scans
            scans += 1
            return compiled.finditer(*args, **kwargs)

    rs._compiled = Counting()
    assert rs.style_of("word7 appears here").line.fg == "#111111"
    assert scans == 1


def test_a_rule_is_matched_without_regard_to_its_position_in_the_line():
    rs = Ruleset([rule("err", "ERROR", fg="#ff0000")])
    assert rs.style_of("ERROR at the front").line.fg == "#ff0000"
    assert rs.style_of("somewhere in the ERROR middle").line.fg == "#ff0000"
    assert rs.style_of("at the end: ERROR").line.fg == "#ff0000"


def test_case_sensitivity_is_per_rule_not_per_ruleset():
    """
    The reason the compiled form uses scoped inline flags. A single global flag
    would make one rule's choice everybody's.
    """
    rs = Ruleset([
        rule("loose", "error", fg="#aa0000"),
        rule("strict", "DEBUG", case_sensitive=True, fg="#00aa00", priority=5),
    ])
    assert rs.style_of("An Error occurred").line.fg == "#aa0000"
    assert rs.style_of("DEBUG probe").line.fg == "#00aa00"
    assert rs.style_of("debug probe").line.fg is None


def test_a_literal_is_matched_literally():
    """A pattern with regex metacharacters must not be read as a regex."""
    rs = Ruleset([rule("lit", "cost=$1.50 (approx)", fg="#ffffff")])
    assert rs.style_of("total cost=$1.50 (approx) today").line.fg == "#ffffff"
    assert rs.style_of("cost=X1Y50 Zapproxc").line.fg is None


def test_a_literal_is_a_substring_unless_boundaries_are_asked_for():
    plain = Ruleset([rule("p", "order=", fg="#ffffff")])
    assert plain.style_of("GET /x?order=12").line.fg == "#ffffff"

    bounded = Ruleset([rule("b", "WARN", word_boundary=True, fg="#ffffff")])
    assert bounded.style_of("a WARN here").line.fg == "#ffffff"
    assert bounded.style_of("WARNING here").line.fg is None


def test_a_boundary_is_ignored_where_it_would_invert_the_request():
    """
    `\\border=\\b` demands a word character after the equals sign, which is the
    opposite of what someone typing `order=` means. The flag is dropped rather
    than silently making the rule match nothing.
    """
    r = rule("b", "order=", word_boundary=True, fg="#ffffff")
    assert r"\b" not in r.regex_source()
    assert Ruleset([r]).style_of("order=99").line.fg == "#ffffff"


def test_a_user_pattern_with_its_own_named_group_still_matches():
    """
    The generated groups are what identify the winning rule, and a user's own
    named group would otherwise be reported instead of ours.
    """
    rs = Ruleset([
        rule("named", r"id=(?P<the_id>\d+)", is_regex=True, scope="match",
             fg="#00ff00"),
    ])
    st = rs.style_of("request id=4213 done")
    assert [(s.start, s.end) for s in st.spans] == [(8, 15)]
    assert st.spans[0].style.fg == "#00ff00"
    assert st.spans[0].rule == "named"


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------

def test_a_line_rule_styles_the_whole_row_and_a_match_rule_only_its_span():
    rs = Ruleset([
        rule("whole", "ERROR", scope="line", fg="#ff0000"),
        rule("part", "req-[0-9]+", is_regex=True, scope="match", fg="#00ff00"),
    ])
    st = rs.style_of("ERROR on req-77 upstream")
    assert st.line.fg == "#ff0000"
    assert [(s.start, s.end, s.style.fg) for s in st.spans] == [(9, 15, "#00ff00")]


def test_a_zero_width_match_produces_no_span():
    """
    A span of no characters would be an invisible style and, at paint time, a
    `drawText` of an empty string per position in the line.
    """
    rs = Ruleset([rule("edge", r"\b(?=req)", is_regex=True, scope="match",
                       fg="#00ff00")])
    assert rs.style_of("req-1 and req-2").spans == ()


def test_a_match_rule_styles_everything_its_pattern_matched():
    """
    The span is the whole match, not a capture group inside it. A rule that
    wants to style only part of what it matched says so with a lookahead --
    which is explicit, where quietly preferring group 1 would make the styled
    region depend on how the pattern happens to be bracketed.
    """
    rs = Ruleset([rule("ip", r"from (\d+\.\d+\.\d+\.\d+)", is_regex=True,
                       scope="match", fg="#00ff00")])
    st = rs.style_of("conn from 10.0.0.7 open")
    assert [(s.start, s.end) for s in st.spans] == [(5, 18)]

    narrow = Ruleset([rule("ip", r"(?<=from )\d+\.\d+\.\d+\.\d+",
                           is_regex=True, scope="match", fg="#00ff00")])
    assert [(s.start, s.end) for s in narrow.style_of(
        "conn from 10.0.0.7 open").spans] == [(10, 18)]


# ---------------------------------------------------------------------------
# overlap: the result must not depend on draw order
# ---------------------------------------------------------------------------

def test_the_higher_priority_line_rule_wins():
    rs = Ruleset([
        rule("low", "WARN", fg="#aaaa00", priority=1),
        rule("high", "ERROR", fg="#ff0000", priority=9),
    ])
    assert rs.style_of("WARN: retrying after ERROR").rule == "high"
    assert rs.style_of("ERROR then WARN").rule == "high"


def test_rule_order_breaks_a_priority_tie_the_same_way_every_time():
    """
    Order is the tiebreak so the same rules always give the same colours. With
    priority alone, equal rules would resolve by whichever matched first in the
    line, which makes a row's colour depend on its content rather than on the
    rules.
    """
    first = Ruleset([
        rule("a", "alpha", fg="#111111"),
        rule("b", "beta", fg="#222222"),
    ])
    assert first.style_of("beta then alpha").rule == "a"
    assert first.style_of("alpha then beta").rule == "a"

    swapped = Ruleset([
        rule("b", "beta", fg="#222222"),
        rule("a", "alpha", fg="#111111"),
    ])
    assert swapped.style_of("beta then alpha").rule == "b"


def test_priority_decides_which_rule_claims_a_region():
    """
    A regex alternation commits to the first branch that matches at a position,
    so the compiled order has to follow priority. Listed first but
    low-priority, `broad` would otherwise beat `narrow` on every line.
    """
    rs = Ruleset([
        rule("broad", "req-[0-9]+", is_regex=True, scope="match",
             fg="#111111", priority=1),
        rule("narrow", "req-7[0-9]", is_regex=True, scope="match",
             fg="#222222", priority=9),
    ])
    st = rs.style_of("GET req-77 ok")
    assert [(s.start, s.end, s.rule) for s in st.spans] == [(4, 10, "narrow")]


def test_no_character_is_ever_styled_twice():
    """
    The property that makes painting deterministic. If two spans covered one
    character, whichever drew last would win -- and draw order is not something
    a user can see or change. One pass guarantees it: matches do not overlap.
    """
    rs = Ruleset([
        rule("outer", "abcdef", scope="match", fg="#111111", priority=1),
        rule("inner", "cd", scope="match", fg="#222222", priority=9),
        rule("words", r"\w+", is_regex=True, scope="match", fg="#333333"),
    ])
    for text in ("xx abcdef yy", "abcdef", "cd abcdef cd", "a b c d e f"):
        spans = rs.style_of(text).spans
        covered = [c for s in spans for c in range(s.start, s.end)]
        assert len(covered) == len(set(covered)), text


def test_a_match_nested_inside_another_is_not_styled_separately():
    """
    The documented cost of scanning once (SPEC 10.1, and the module docstring).
    The earlier match claims the region whatever its priority, because the scan
    is leftmost-first -- so this is a *missing* highlight, never a wrong one.

    Asserted rather than left implicit: it is the kind of thing someone would
    otherwise rediscover as a bug report about a rule that "does not work".
    """
    rs = Ruleset([
        rule("outer", "abcdef", scope="match", fg="#111111", priority=1),
        rule("inner", "cd", scope="match", fg="#222222", priority=9),
    ])
    spans = rs.style_of("xx abcdef yy").spans
    assert [(s.start, s.end, s.rule) for s in spans] == [(3, 9, "outer")]

    # Where they do not nest, both are found.
    both = rs.style_of("cd then abcdef").spans
    assert [s.rule for s in both] == ["inner", "outer"]


def test_spans_come_back_in_reading_order():
    """Paint walks them left to right; out-of-order spans would draw backwards."""
    rs = Ruleset([rule("w", r"\w+", is_regex=True, scope="match", fg="#111111")])
    spans = rs.style_of("one two three four").spans
    assert [s.start for s in spans] == sorted(s.start for s in spans)


# ---------------------------------------------------------------------------
# bad rules are reported, not raised
# ---------------------------------------------------------------------------

def test_an_invalid_regex_is_rejected_and_the_others_still_work():
    rs = Ruleset([
        rule("broken", "([unclosed", is_regex=True, fg="#ff0000"),
        rule("fine", "ERROR", fg="#00ff00"),
    ])
    assert rs.style_of("ERROR here").line.fg == "#00ff00"
    assert [r.name for r, _ in rs.rejected] == ["broken"]
    assert "invalid regex" in rs.rejected[0][1]


def test_a_pattern_matching_the_empty_string_is_rejected():
    """
    It would match at every position in every line: the whole file painted, and
    nothing findable in it.
    """
    rs = Ruleset([rule("empty", "x*", is_regex=True, fg="#ff0000")])
    assert rs.style_of("no x here").is_plain
    assert "empty string" in rs.rejected[0][1]


def test_an_empty_pattern_is_rejected():
    assert "empty" in rule("blank", "").validate()


def test_a_ruleset_with_no_usable_rules_styles_nothing():
    for rs in (Ruleset(), Ruleset([rule("bad", "(", is_regex=True)])):
        assert rs.style_of("ERROR anything").is_plain


def test_a_disabled_rule_does_not_match():
    rs = Ruleset([rule("off", "ERROR", fg="#ff0000", enabled=False)])
    assert rs.style_of("ERROR here").is_plain
    rs.set_enabled("off", True)
    assert rs.style_of("ERROR here").line.fg == "#ff0000"


# ---------------------------------------------------------------------------
# bounded cost
# ---------------------------------------------------------------------------

def test_only_a_bounded_prefix_of_a_line_is_scanned():
    """
    A 1 MB single line is in the corpus. Scanning all of it per visible row
    would put content length back into the frame budget -- the dependency M3
    removed by slicing the message to the window.
    """
    rs = Ruleset([rule("err", "ERROR", fg="#ff0000")])
    inside = "x" * (SCAN_LIMIT - 10) + " ERROR"
    outside = "x" * (SCAN_LIMIT + 10) + " ERROR"
    assert rs.style_of(inside).line.fg == "#ff0000"
    assert rs.style_of(outside).is_plain


def test_a_styled_row_is_remembered_rather_than_rescanned():
    rs = Ruleset([rule("err", "ERROR", fg="#ff0000")])
    text = "2026-01-01 ERROR something"
    first = rs.style_of(text)
    assert rs.style_of(text) is first


def test_editing_a_rule_drops_the_remembered_styles():
    rs = Ruleset([rule("err", "ERROR", fg="#ff0000")])
    text = "ERROR here"
    assert rs.style_of(text).line.fg == "#ff0000"
    rs.replace_rule("err", fg="#00ff00")
    assert rs.style_of(text).line.fg == "#00ff00"


# ---------------------------------------------------------------------------
# editing (FR-31)
# ---------------------------------------------------------------------------

def test_rules_can_be_added_removed_and_reordered():
    rs = Ruleset([rule("a", "alpha"), rule("b", "beta")])
    rs.add(rule("c", "gamma"))
    assert [r.name for r in rs.rules] == ["a", "b", "c"]

    assert rs.move("c", -2)
    assert [r.name for r in rs.rules] == ["c", "a", "b"]

    assert rs.remove("a")
    assert [r.name for r in rs.rules] == ["c", "b"]
    assert not rs.remove("nope")


def test_moving_past_either_end_is_a_no_op_not_an_error():
    rs = Ruleset([rule("a", "alpha"), rule("b", "beta")])
    assert not rs.move("a", -1)
    assert not rs.move("b", 5) or [r.name for r in rs.rules] == ["a", "b"]


def test_editing_a_builtin_makes_it_the_users_rule():
    """
    SPEC 10.1: built-ins are never silently overwritten. An edit shadows one,
    so "reset to default" has something to restore.
    """
    rs = Ruleset([rule("severity:error", "ERROR", builtin=True, fg="#ff0000")])
    rs.replace_rule("severity:error", fg="#ff8800")
    edited = rs.rules[0]
    assert edited.fg == "#ff8800"
    assert edited.builtin is False


def test_disabling_a_builtin_does_not_make_it_the_users_rule():
    """
    Turning a rule off is a view preference, not an edit. Treating it as one
    would make "reset to default" silently switch everything back on.
    """
    rs = Ruleset([rule("severity:error", "ERROR", builtin=True, fg="#ff0000")])
    rs.set_enabled("severity:error", False)
    assert rs.rules[0].builtin is True
    assert rs.rules[0].enabled is False


# ---------------------------------------------------------------------------
# style composition
# ---------------------------------------------------------------------------

def test_an_unset_colour_means_inherit_rather_than_black():
    """
    A rule that only sets bold must not also decide the colour, or every bold
    rule would silently un-colour the severity it was applied to.
    """
    over = Style(bold=True).over(Style(fg="#112233", bg="#000000"))
    assert over.fg == "#112233"
    assert over.bg == "#000000"
    assert over.bold is True


def test_an_empty_style_is_recognised_as_empty():
    assert Style().is_empty
    assert not Style(bold=True).is_empty
    assert not Style(fg="#000000").is_empty


# ---------------------------------------------------------------------------
# the shipped rules (FR-30)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    ("2026-01-01 00:00:00 ERROR connection reset", Severity.ERROR),
    ("2026-01-01 00:00:00 WARN pool at 85 percent", Severity.WARNING),
    ("2026-01-01 00:00:00 FATAL out of memory", Severity.CRITICAL),
    ("2026-01-01 00:00:00 CRITICAL disk gone", Severity.CRITICAL),
    ("2026-01-01 00:00:00 WARNING pool at 85 percent", Severity.WARNING),
])
def test_the_loud_rules_colour_from_the_shared_palette(text, expected):
    """
    The colours come from `session.palette`, so the message text, the severity
    mark and later the minimap agree. A rule with its own red would make the
    minimap disagree with the rows it is a map of.
    """
    from logweave.session import palette as pal

    st = default_ruleset().style_of(text)
    assert st.line.fg == pal.severity_colour(expected)


@pytest.mark.parametrize(("text", "expected"), [
    ("2026-01-01 00:00:00 NOTICE config reloaded", Severity.NOTICE),
    ("2026-01-01 00:00:00 INFO started", Severity.INFO),
    ("2026-01-01 00:00:00 DEBUG cache probe", Severity.DEBUG),
    ("2026-01-01 00:00:00 TRACE entering", Severity.TRACE),
])
def test_below_warning_a_shipped_rule_classifies_without_colouring(text, expected):
    """
    The floor, and what it does *not* cost. A healthy service writes almost
    nothing but INFO, so colouring that class colours every row -- which is the
    same as colouring none (`builtins.COLOUR_FROM`).

    The rule is still there and still carries its severity, because that is
    what the index-time classifier, the gutter mark, the minimap bands and the
    severity filter are built on. Only the paint went.
    """
    word = text.split()[2]

    st = default_ruleset().style_of(text)
    assert st.rule == f"severity:{word.lower()}", "the word still matches"
    assert st.is_plain, "and that rule leaves the text alone"

    words = default_ruleset().severity_words()
    assert word in words[int(expected)], "and still classifies the row"


def test_the_floor_is_one_constant_and_it_is_warning():
    """
    One decision, in one place -- the palette, because the message text, the
    gutter mark and the minimap all read it and a floor that disagreed between
    them would be worse than no floor. This test fails loudly if someone moves
    it rather than silently disagreeing with the SPEC.
    """
    from logweave.session import palette as pal

    assert pal.COLOUR_FROM is Severity.WARNING
    assert pal.is_loud(Severity.WARNING) and not pal.is_loud(Severity.INFO)
    coloured = {r.severity for r in default_ruleset().rules
                if r.severity is not None and not Style(fg=r.fg, bg=r.bg).is_empty}
    assert coloured == {Severity.WARNING, Severity.ERROR, Severity.CRITICAL}


@pytest.mark.parametrize("text", [
    "DEBUGGER attached to pid 9",
    "INFORMATIONAL message only",
    "ERRORS total: 0",
])
def test_a_longer_word_containing_a_level_word_is_not_matched(text):
    assert default_ruleset().style_of(text).is_plain


def test_the_shipped_rules_find_a_stack_trace_with_no_level_word_on_it():
    """
    The case the indexer's severity pass cannot see: it compares a fixed byte
    column, and these lines have no level word at all.
    """
    rs = default_ruleset()
    assert rs.style_of("  at com.example.Pool.borrow(Pool.java:142)").rule \
        == "shape:java-frame"
    assert rs.style_of('  File "app.py", line 12, in handle').rule \
        == "shape:python-frame"
    assert rs.style_of("Caused by: java.io.IOException: broken pipe").rule \
        == "shape:caused-by"
    assert rs.style_of("Traceback (most recent call last):").rule \
        == "shape:traceback-header"


def test_an_exception_class_name_is_marked_inside_the_line():
    st = default_ruleset().style_of(
        "request failed: com.example.svc.PoolTimeoutException: no capacity")
    assert [st.spans[0].start, st.spans[0].end] == [16, 52]
    assert st.spans[0].rule == "shape:exception-class"


def test_the_shipped_rules_all_compile():
    group = default_group()
    for name, rs in group.sets.items():
        assert rs.rejected == [], (name, rs.rejected)
        if name == USER_SET:
            # The fourth set is where a user's own rules go (FR-31). It ships
            # empty, which is a set with nothing in it rather than a set that
            # failed to compile -- `rejected` above is what tells those apart.
            assert rs.rules == []
            continue
        assert rs.rules, name


def test_the_user_set_ships_empty_and_already_on():
    """
    FR-31: a rule a user has just typed must colour something without their
    having to find a switch for it first. The set it lands in is therefore
    active from the start, and being active while empty contributes nothing --
    `(?!)` can never match.
    """
    group = default_group()
    assert group.sets[USER_SET].rules == []
    assert USER_SET in group.active
    assert group.flatten().style_of("anything at all").is_plain


def test_syslog_priorities_are_available_but_off_by_default():
    group = default_group()
    assert SYSLOG_SET in group.sets
    assert SYSLOG_SET not in group.active
    assert group.active == {SEVERITY_SET, SHAPES_SET, USER_SET}

    assert group.flatten().style_of("<3>kernel: oops").is_plain
    group.active.add(SYSLOG_SET)
    assert group.flatten().style_of("<3>kernel: oops").rule == "syslog:<3>"


@pytest.mark.parametrize(("line", "expected"), [
    ("<0>kernel: machine check", Severity.CRITICAL),
    ("<1>kernel: act now", Severity.CRITICAL),
    ("<2>kernel: disk failing", Severity.CRITICAL),
    ("<3>kernel: oops", Severity.ERROR),
    ("<4>kernel: temperature high", Severity.WARNING),
])
def test_the_syslog_set_colours_emergency_through_warning(line, expected):
    """
    RFC 5424 counts down, so the loud priorities are the low numbers: 0
    emergency, 1 alert, 2 critical, 3 error, 4 warning. All five are coloured,
    from the same palette as everything else.
    """
    from logweave.session import palette as pal

    rs = Ruleset(_syslog_rules())
    st = rs.style_of(line)
    assert st.line.fg == pal.severity_colour(expected)


@pytest.mark.parametrize("line", [
    "<5>kernel: config reloaded",
    "<6>kernel: started",
    "<7>kernel: probing",
])
def test_the_syslog_set_leaves_notice_and_below_out_altogether(line):
    """
    Not shipped uncoloured but *absent*, which is the difference between these
    and the quiet severity words. A syslog rule is a regex and so cannot carry
    a `severity` (SPEC 10.3), so an uncoloured one would do nothing at all
    while still costing a branch in the one regex every row is scanned with.
    """
    rs = Ruleset(_syslog_rules())
    assert rs.style_of(line).is_plain
    assert rs.style_of(line).rule == "", "no rule matched at all"
    assert {r.name for r in rs.rules} == {f"syslog:<{p}>" for p in range(5)}


def test_turning_a_set_off_removes_exactly_its_rules():
    group = default_group()
    with_shapes = len(group.flatten().rules)
    group.active.discard(SHAPES_SET)
    without = len(group.flatten().rules)
    assert without < with_shapes
    assert group.flatten().style_of("Traceback (most recent call last):").is_plain


# ---------------------------------------------------------------------------
# the severity contribution (D11, Q2)
# ---------------------------------------------------------------------------

def test_literal_severity_rules_become_an_index_time_word_list():
    """
    SPEC 10.3: severity is computed for every row at index time because the
    minimap needs it. These words are what that pass compares.
    """
    words = default_ruleset().severity_words()
    assert "ERROR" in words[int(Severity.ERROR)]
    assert "FATAL" in words[int(Severity.CRITICAL)]
    assert int(Severity.NONE) not in words


def test_a_regex_severity_rule_does_not_contribute_words():
    """
    The classifier compares fixed byte columns and has no way to run a regex.
    Such a rule still paints; it simply cannot colour the minimap, and
    pretending otherwise would put the file on the 2 MB/s fallback path.
    """
    rs = Ruleset([
        rule("re", "ERR.*", is_regex=True, severity=Severity.ERROR, fg="#ff0000"),
        rule("lit", "BOOM", severity=Severity.CRITICAL, fg="#ff0000"),
    ])
    words = rs.severity_words()
    assert words == {int(Severity.CRITICAL): ("BOOM",)}
    assert rs.style_of("ERR something").line.fg == "#ff0000"


def test_the_fingerprint_changes_with_the_words_and_not_with_the_colours():
    """
    It guards a cached index, whose `flags` depend on the words alone. Making
    it move when a colour changes would throw away every sidecar over a
    repaint.
    """
    rs = Ruleset([rule("lit", "BOOM", severity=Severity.CRITICAL, fg="#ff0000")])
    before = rs.fingerprint()

    rs.replace_rule("lit", fg="#00ff00")
    assert rs.fingerprint() == before

    rs.replace_rule("lit", pattern="KABOOM")
    assert rs.fingerprint() != before


def test_a_disabled_severity_rule_leaves_the_fingerprint_alone_only_if_unused():
    rs = Ruleset([rule("lit", "BOOM", severity=Severity.CRITICAL)])
    enabled = rs.fingerprint()
    rs.set_enabled("lit", False)
    assert rs.fingerprint() != enabled
