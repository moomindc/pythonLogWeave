"""
Persisting a user's highlight rules -- FR-31, SPEC 10.1.

Two properties, and the second is the one worth the tests. A rule a user typed
must come back exactly as typed, including the fields that decide where its
colour appears. And the store is a registry value or a text file, so it is
user-editable by construction: nothing in it may be able to stop the
application opening a log.
"""

from __future__ import annotations

import json

from logweave.highlight import store
from logweave.highlight.rules import HighlightRule, Ruleset
from logweave.index.severity import Severity


def a_rule(**kw) -> HighlightRule:
    kw.setdefault("name", "timeouts")
    kw.setdefault("pattern", "timeout")
    return HighlightRule(**kw)


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------

def test_a_rule_survives_the_round_trip_in_every_field():
    original = a_rule(
        pattern=r"id=(?P<id>[0-9a-f]{8})",
        is_regex=True,
        case_sensitive=True,
        word_boundary=True,
        scope="match",
        fg="#ff4040",
        bg="#331a14",
        bold=True,
        italic=True,
        priority=85,
        enabled=False,
        on_strips=True,
        severity=Severity.ERROR,
    )
    back, notes = store.load(store.dump([original]))
    assert notes == []
    assert back == [original]


def test_the_defaults_round_trip_too():
    """
    The fields left out of the JSON are the ones at their default, so the
    reader has to put them back -- not leave a rule half-built.
    """
    back, notes = store.load(store.dump([a_rule()]))
    assert notes == []
    assert back == [a_rule()]


def test_order_is_preserved():
    """FR-31: list order is the tiebreak when two rules share a priority."""
    rules = [a_rule(name=f"r{i}", pattern=f"p{i}") for i in range(5)]
    back, _ = store.load(store.dump(rules))
    assert [r.name for r in back] == [f"r{i}" for i in range(5)]


def test_stored_rules_go_straight_into_a_ruleset():
    """The end the whole module exists for: loaded rules must actually paint."""
    rules, notes = store.load(store.dump([a_rule(fg="#ff4040")]))
    assert notes == []
    rs = Ruleset(rules)
    assert rs.rejected == []
    assert rs.style_of("connection timeout after 30s").line.fg == "#ff4040"


def test_a_stored_rule_is_never_a_builtin():
    """
    `builtin` buys a rule protection from being overwritten (SPEC 10.1).
    Accepting it from a text file would let a hand edit claim that protection.
    """
    text = json.dumps({"version": 1, "rules": [
        {"name": "sneaky", "pattern": "x", "builtin": True}]})
    rules, _ = store.load(text)
    assert rules and rules[0].builtin is False


# ---------------------------------------------------------------------------
# nothing here is trusted
# ---------------------------------------------------------------------------

def test_an_empty_store_is_not_an_error():
    """A first run, which is the commonest case of all."""
    for empty in (None, "", "   ", 0, [], object()):
        assert store.load(empty) == ([], [])


def test_unreadable_text_is_reported_not_raised():
    rules, notes = store.load("{not json at all")
    assert rules == []
    assert len(notes) == 1
    assert "could not be read" in notes[0]


def test_a_newer_schema_is_refused_rather_than_guessed_at():
    """
    A field this version knows may mean something different in the next one, so
    the only safe reading of a future file is not to read it.
    """
    rules, notes = store.load(json.dumps(
        {"version": store.VERSION + 1, "rules": [{"name": "a", "pattern": "b"}]}))
    assert rules == []
    assert "newer version" in notes[0]


def test_one_bad_rule_does_not_take_the_good_ones_with_it():
    """
    The posture `Ruleset.rejected` already takes: a typo in rule three must not
    cost a user rules one, two, four and five.
    """
    text = json.dumps({"version": 1, "rules": [
        {"name": "good-1", "pattern": "a"},
        {"name": "broken", "pattern": "[unclosed", "is_regex": True},
        {"name": "good-2", "pattern": "b"},
    ]})
    rules, notes = store.load(text)
    assert [r.name for r in rules] == ["good-1", "good-2"]
    assert len(notes) == 1
    assert "broken" in notes[0]


def test_a_rule_matching_the_empty_string_is_refused():
    """It would match at every position and paint the whole file."""
    text = json.dumps({"version": 1, "rules": [
        {"name": "everything", "pattern": "x*", "is_regex": True}]})
    rules, notes = store.load(text)
    assert rules == []
    assert "empty string" in notes[0]


def test_a_duplicate_name_is_dropped_with_a_reason():
    """
    Names are the handle `remove`, `replace_rule` and `set_enabled` all take,
    so two rules called the same thing make an edit land on one of them and
    look like it did nothing.
    """
    text = json.dumps({"version": 1, "rules": [
        {"name": "same", "pattern": "a"},
        {"name": "same", "pattern": "b"},
    ]})
    rules, notes = store.load(text)
    assert len(rules) == 1
    assert rules[0].pattern == "a", "the first one keeps the name"
    assert "duplicate" in notes[0]


def test_a_rule_with_no_name_is_refused():
    text = json.dumps({"version": 1, "rules": [{"pattern": "a"},
                                               {"name": "   ", "pattern": "b"}]})
    rules, notes = store.load(text)
    assert rules == []
    assert len(notes) == 2


def test_a_field_of_the_wrong_type_is_refused_rather_than_coerced():
    """
    A JSON `1` where a bool belongs means a hand edit, and guessing at intent
    in a file we have just decided not to trust is how a rule ends up doing
    something nobody asked for.
    """
    for bad in ({"on_strips": 1}, {"priority": "high"}, {"pattern": 7},
                {"bold": "yes"}):
        row = {"name": "r", "pattern": "a", **bad}
        rules, notes = store.load(json.dumps({"version": 1, "rules": [row]}))
        assert rules == [], bad
        assert notes, bad


def test_an_unknown_scope_is_refused():
    rules, notes = store.load(json.dumps({"version": 1, "rules": [
        {"name": "r", "pattern": "a", "scope": "paragraph"}]}))
    assert rules == []
    assert "scope" in notes[0]


def test_an_unknown_severity_name_is_refused():
    rules, notes = store.load(json.dumps({"version": 1, "rules": [
        {"name": "r", "pattern": "a", "severity": "APOCALYPTIC"}]}))
    assert rules == []
    assert "eight classes" in notes[0]


def test_a_severity_name_is_read_case_insensitively():
    """It is written as a name for a human to read, so it is read like one."""
    for spelling in ("ERROR", "error", "Error"):
        rules, notes = store.load(json.dumps({"version": 1, "rules": [
            {"name": "r", "pattern": "a", "severity": spelling}]}))
        assert notes == []
        assert rules[0].severity is Severity.ERROR


def test_a_rule_list_that_is_not_a_list_is_reported():
    for blob in ({"version": 1, "rules": "nope"}, {"version": 1}, [1, 2, 3]):
        rules, notes = store.load(json.dumps(blob))
        assert rules == []
        assert notes


def test_a_rule_that_is_not_an_object_is_reported():
    rules, notes = store.load(json.dumps(
        {"version": 1, "rules": ["just a string"]}))
    assert rules == []
    assert "not an object" in notes[0]


# ---------------------------------------------------------------------------
# the text itself
# ---------------------------------------------------------------------------

def test_the_dump_is_stable_so_it_can_be_diffed():
    rules = [a_rule(name="b"), a_rule(name="a", pattern="other")]
    assert store.dump(rules) == store.dump(rules)
    assert store.dump(rules) != store.dump(list(reversed(rules))), (
        "order is meaningful, so it must be visible in the text")


def test_an_empty_list_round_trips():
    assert store.load(store.dump([])) == ([], [])
