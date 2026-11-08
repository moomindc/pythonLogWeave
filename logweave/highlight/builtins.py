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
* **syslog** -- `<0>`-`<7>` priorities, for a file written by syslog rather than
  by an application framework.

The colours come from `session.palette`, not from here, so severity has one
palette across the message text, the severity mark, the source dock and later
the minimap. A rule that invented its own red would make the minimap disagree
with the rows it is a map of.

Why the severity rules are `line` scope and the shapes mostly `match`: a line
with ERROR in it *is* an error, and the eye should find the whole row. A stack
frame is a line of ordinary text in which one part -- the file and line -- is
worth picking out.
"""

from __future__ import annotations

from ..index.severity import DEFAULT_WORDS, Severity
from ..session import palette as pal
from .rules import HighlightRule, Ruleset, RulesetGroup

SEVERITY_SET = "severity"
SHAPES_SET = "shapes"
SYSLOG_SET = "syslog"


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
    """
    out: list[HighlightRule] = []
    for level, words in DEFAULT_WORDS.items():
        sev = Severity(level)
        for word in words:
            out.append(HighlightRule(
                name=f"severity:{word.lower()}",
                pattern=word,
                is_regex=False,
                word_boundary=True,
                case_sensitive=False,
                scope="line",
                fg=pal.severity_colour(sev),
                bg=pal.severity_background(sev),
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


def _syslog_rules() -> list[HighlightRule]:
    """
    Syslog priorities, `<0>` to `<7>`.

    RFC 5424 numbers them the opposite way round from the eight classes -- 0 is
    emergency, 7 is debug -- and `NOTICE` exists in the class list precisely so
    this maps without loss (D11).
    """
    mapping = {
        0: Severity.CRITICAL, 1: Severity.CRITICAL, 2: Severity.CRITICAL,
        3: Severity.ERROR, 4: Severity.WARNING, 5: Severity.NOTICE,
        6: Severity.INFO, 7: Severity.DEBUG,
    }
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
    """
    return RulesetGroup(
        sets={
            SEVERITY_SET: Ruleset(_severity_rules()),
            SHAPES_SET: Ruleset(_shape_rules()),
            SYSLOG_SET: Ruleset(_syslog_rules()),
        },
        active={SEVERITY_SET, SHAPES_SET},
    )


def default_ruleset() -> Ruleset:
    """The flattened default, which is what the view paints with."""
    return default_group().flatten()
