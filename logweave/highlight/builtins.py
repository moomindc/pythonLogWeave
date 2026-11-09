"""
The rulesets that ship -- FR-30, SPEC 10.1, PRD Q2.

Q2 was answered "defaults are good, but severity rules must be user
extensible", so these are a starting point, not a fixture. Three sets, each one
switch:

* **severity** -- the level words, pointed at the eight fixed classes (D11), so
  they drive the index-time classification as well as the colour.
* **shapes** -- what an exception and a stack frame *look* like, which is how a
  Java trace or a Python traceback gets found in a file whose own lines carry no
  level word at all.
* **syslog** -- `<0>`-`<4>` priorities, for a file written by syslog rather than
  by an application framework.

The colours come from `session.palette`, not from here, so severity has one
palette across the message text, the severity mark, the source dock and later
the minimap. A rule that invented its own red would make the minimap disagree
with the rows it is a map of.

Why the severity rules are `line` scope and the shapes mostly `match`: a line
with ERROR in it *is* an error, and the eye should find the whole row. A stack
frame is a line of ordinary text in which one part -- the file and line -- is
worth picking out.

## Only WARNING and above are coloured

`palette.COLOUR_FROM` is the floor, and it is shared with the severity mark and
the minimap so that all three agree. Colouring INFO, NOTICE, DEBUG and TRACE as
well means colouring *every* row of an ordinary log -- a healthy service writes
almost nothing else -- and a view in which everything is coloured is a view in
which nothing stands out. It also spends the reader's attention on the lines
they are not looking for.

So the quiet classes keep their rule, with its `severity`, and give up only
their paint: the word is still classified, so the left-hand severity mark, the
minimap, the timeline and every filter that names a severity go on working
exactly as before. What changes is the message text, which stays in the
ordinary foreground until there is something worth saying about it.

`SEVERITY_BACKGROUNDS` reached the same conclusion about row tints a milestone
earlier -- "tinting INFO too would mean every row is tinted, which is the same
as none being tinted" -- and this is that argument applied to the foreground.
"""

from __future__ import annotations

from ..index.severity import DEFAULT_WORDS, Severity
from ..session import palette as pal
from .rules import HighlightRule, Ruleset, RulesetGroup

SEVERITY_SET = "severity"
SHAPES_SET = "shapes"
SYSLOG_SET = "syslog"
USER_SET = "user"
"""
The set a user's own rules live in -- FR-31.

A fourth set rather than rules mixed into the other three, so that "my rules"
is one switch, so that a reset of the shipped sets cannot take a user's work
with it, and so that the thing being saved to the settings store is exactly one
list with a name.

Empty when the application ships, and **active**: a set that had to be switched
on before a rule a user had just typed did anything would read as the rule not
working.
"""


def _severity_rules() -> list[HighlightRule]:
    """
    One rule per level word, carrying its class.

    Literals with `word_boundary`, not regexes. The boundary is what stops
    `DEBUGGER` reading as `DEBUG` and `INFORMATIONAL` as `INFO`; staying
    literal is what lets the same words drive the index-time classifier, which
    compares fixed byte columns and has no way to run a regex (SPEC 10.3). A
    regex here would paint correctly and leave the minimap uncoloured.

    Priority rises with severity, so a line reading "WARN: retrying after
    ERROR" comes out as the error it is talking about rather than the warning
    it is logged as. That is a judgement call and it is the useful one: the
    reason to colour a row is to find the worst thing on it.

    Below `pal.COLOUR_FROM` a rule carries no colour at all -- see the module
    docstring. It is still a rule, and it still carries its `severity`, which
    is what the minimap and the severity filter are built on; it simply leaves
    the text alone.
    """
    out: list[HighlightRule] = []
    for level, words in DEFAULT_WORDS.items():
        sev = Severity(level)
        loud = pal.is_loud(sev)
        for word in words:
            out.append(HighlightRule(
                name=f"severity:{word.lower()}",
                pattern=word,
                is_regex=False,
                word_boundary=True,
                case_sensitive=False,
                scope="line",
                fg=pal.severity_colour(sev) if loud else None,
                bg=pal.severity_background(sev) if loud else None,
                bold=sev >= Severity.ERROR,
                priority=int(sev) * 10,
                severity=sev,
                builtin=True,
            ))
    return out


def _shape_rules() -> list[HighlightRule]:
    """
    What a failure looks like when nothing says "ERROR".

    Every one of these is a shape rather than a word, which is the point: a Java
    stack trace's frames, a Python traceback's `File "x.py", line 12`, and a
    bare `SomeThingException` all appear in logs whose own lines carry no level
    word. The indexer's severity pass cannot see them -- it compares a fixed
    byte column (SPEC 10.3) -- so they exist only at paint time, and they carry
    no `severity` for exactly that reason.
    """
    err = pal.severity_colour(Severity.ERROR)
    return [
        HighlightRule(
            name="shape:exception-class",
            # A CamelCase identifier ending in Exception/Error/Throwable, with
            # or without a package in front of it.
            pattern=r"\b(?:[A-Za-z_][\w$]*\.)*[A-Z][\w$]*"
                    r"(?:Exception|Error|Throwable)\b",
            is_regex=True, case_sensitive=True, scope="match",
            fg=err, bold=True, priority=60, builtin=True,
        ),
        HighlightRule(
            name="shape:java-frame",
            # "	at com.example.Thing.method(Thing.java:42)"
            pattern=r"^\s*at\s+[\w$.]+\([^)]*\)",
            is_regex=True, case_sensitive=True, scope="line",
            fg=pal.SEVERITY_COLOURS[Severity.TRACE], italic=True,
            priority=20, builtin=True,
        ),
        HighlightRule(
            name="shape:python-frame",
            pattern=r'^\s*File "[^"]+", line \d+',
            is_regex=True, case_sensitive=True, scope="line",
            fg=pal.SEVERITY_COLOURS[Severity.TRACE], italic=True,
            priority=20, builtin=True,
        ),
        HighlightRule(
            name="shape:caused-by",
            pattern=r"^\s*Caused by:",
            is_regex=True, case_sensitive=True, scope="line",
            fg=err, bold=True, priority=65, builtin=True,
        ),
        HighlightRule(
            name="shape:traceback-header",
            pattern=r"^Traceback \(most recent call last\):",
            is_regex=True, case_sensitive=True, scope="line",
            fg=err, bold=True, priority=65, builtin=True,
        ),
    ]


PRIORITY_SEVERITY = {
    0: Severity.CRITICAL,   # emergency: system is unusable
    1: Severity.CRITICAL,   # alert: action must be taken immediately
    2: Severity.CRITICAL,   # critical
    3: Severity.ERROR,
    4: Severity.WARNING,
    5: Severity.NOTICE,
    6: Severity.INFO,
    7: Severity.DEBUG,
}
"""
RFC 5424's eight priorities onto the eight classes (D11).

Numbered the opposite way round -- 0 is emergency, 7 is debug -- and `NOTICE`
exists in the class list precisely so this maps without loss. Emergency, alert
and critical all land on `CRITICAL`, which is the one place the mapping loses
anything: three numbers for "the machine is in trouble" against one class, and
nothing in the view would do differently for them.
"""


def _syslog_rules() -> list[HighlightRule]:
    """
    Syslog priorities, `<0>` to `<4>`.

    Only the loud half, for the reason the module docstring gives: `<5>`,
    `<6>` and `<7>` are notice, informational and debug, which is most of a
    syslog, and a rule that colours most of a file is one that hides the rest.
    They are not written as uncoloured rules either -- unlike the severity
    words they carry no `severity` (a regex cannot reach the index-time
    classifier, see `HighlightRule.severity`), so an uncoloured one would do
    nothing at all while still costing a branch in the regex every row is
    scanned with.
    """
    mapping = {pri: sev for pri, sev in PRIORITY_SEVERITY.items()
               if pal.is_loud(sev)}
    return [
        HighlightRule(
            name=f"syslog:<{pri}>",
            pattern=rf"^<{pri}>",
            is_regex=True, case_sensitive=True, scope="line",
            fg=pal.severity_colour(sev), bg=pal.severity_background(sev),
            bold=sev >= Severity.ERROR,
            priority=int(sev) * 10 + 1,
            builtin=True,
        )
        for pri, sev in mapping.items()
    ]


def default_group() -> RulesetGroup:
    """
    The three sets, with severity and shapes on and syslog off.

    Syslog is off by default because `<3>` at the start of a line is rare
    outside a raw syslog capture, and a rule that almost never fires still
    costs a branch in the one regex every row is scanned with. It is one switch
    away for anyone reading those files.

    The fourth set, `user`, ships empty and on; see `USER_SET`.
    """
    return RulesetGroup(
        sets={
            SEVERITY_SET: Ruleset(_severity_rules()),
            SHAPES_SET: Ruleset(_shape_rules()),
            SYSLOG_SET: Ruleset(_syslog_rules()),
            USER_SET: Ruleset([]),
        },
        active={SEVERITY_SET, SHAPES_SET, USER_SET},
    )


def default_ruleset() -> Ruleset:
    """The flattened default, which is what the view paints with."""
    return default_group().flatten()
