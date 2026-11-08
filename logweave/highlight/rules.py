"""
Highlight rules and the one regex they compile into -- FR-30, FR-31, SPEC 10.1.

A user may have fifty rules. Running fifty regexes over every visible row would
put rule count into the frame budget, so every enabled rule becomes one named
group in a **single** alternation and a line is scanned once however many rules
there are. Per-rule case sensitivity survives that because Python supports
scoped inline flags: `(?i:...)` applies to one group rather than the pattern.

Qt-free, like the palette it draws its colours from. The rules decide *what*
colour a run of text is; only the view knows what a `QColor` is. That is also
what lets the same rules drive an HTML export later, and what lets every one of
these tests run headless.

Two scopes, and the difference matters at paint time:

* `scope="line"` styles the whole row -- the case for ERROR, where the point is
  to find the line from across the room.
* `scope="match"` styles only the matched span -- the case for a request id or
  an IP address, where the point is to pick it out of the line.

Overlaps resolve by priority, then by rule order. Both are needed: priority is
how a user says "my rule beats the built-in", and rule order is a stable
tiebreak, so the same rules always produce the same colours. Priority also
decides the *order of the alternatives* in the compiled pattern, because a
regex alternation commits to the first branch that matches at a position -- so
without that ordering the rule list's order would quietly outrank the
priorities the user set.

## What one pass cannot do

`finditer` returns **non-overlapping** matches, so each region of a line is
claimed by exactly one rule. A rule whose text sits *inside* another rule's
match is therefore not styled separately: `abcdef` and the `cd` within it
cannot both win, and the one that starts earlier takes the region whatever its
priority, because the scan is leftmost-first.

This is a deliberate trade. The alternative is one scan per rule, which puts
rule count into the frame budget -- the thing SPEC 10.1 chose the single
alternation to avoid. In practice nested match rules are rare and the
consequence is a missing highlight rather than a wrong one, while the cost of
the alternative is paid on every row of every file.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Literal

from ..index.severity import Severity

SCAN_LIMIT = 2048
"""
How much of a row's text is scanned for `line`-scope rules.

A log line can be 4000 characters of SQL or a 1 MB single line (the corpus has
one). Scanning all of it per visible row would make the frame budget depend on
content length -- the exact dependency M3 removed by slicing the message to the
visible character window. A rule that only matches past character 2048 will not
colour the row, which is a real limitation and a deliberate one: the severity
class, which is what the eye actually scans for, is computed over the whole
entry at index time and is not subject to this bound.
"""

CACHE_ROWS = 1024
"""
Styled rows kept, keyed by text (SPEC 9.1).

A repaint during selection, resize or a scrollbar drag re-styles the same
screenful, so this turns a 48-row rescan into 48 dictionary hits. Keyed by the
text itself rather than by row, because the same line often repeats in a log
and because a row's index changes when a source is enabled or disabled.
"""

Scope = Literal["line", "match"]

_GROUP = "_lw"
"""
Prefix for the generated group names.

Distinctive on purpose: a user pattern may contain its own named groups, and a
collision would be a compile error in the middle of someone's rule list.
"""


@dataclass(frozen=True)
class Style:
    """A resolved appearance. `None` means "inherit what was already there"."""

    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    italic: bool = False

    @property
    def is_empty(self) -> bool:
        return self.fg is None and self.bg is None and not self.bold and not self.italic

    def over(self, base: Style) -> Style:
        """`self` laid over `base`: whatever self does not specify shows through."""
        return Style(
            fg=self.fg if self.fg is not None else base.fg,
            bg=self.bg if self.bg is not None else base.bg,
            bold=self.bold or base.bold,
            italic=self.italic or base.italic,
        )


@dataclass(frozen=True)
class Span:
    """A styled run of characters, in text coordinates."""

    start: int
    end: int
    style: Style
    rule: str = ""


@dataclass(frozen=True)
class RowStyle:
    """
    What a row looks like: a whole-row style plus any styled spans inside it.

    Frozen and cached, so the view may hold on to one without copying.
    """

    line: Style = Style()
    spans: tuple[Span, ...] = ()
    rule: str = ""
    """The `line`-scope rule that won, for a tooltip and for tests."""

    @property
    def is_plain(self) -> bool:
        return self.line.is_empty and not self.spans


@dataclass
class HighlightRule:
    """
    One rule, exactly as SPEC 10.1 specifies it.

    `severity` points a rule at one of the eight fixed classes (D11). That is
    not cosmetic: severity is computed at index time for *every* row because the
    minimap needs it (SPEC 10.3), so a rule carrying a severity contributes to
    the indexing ruleset rather than to painting, and changing it invalidates
    cached indexes.
    """

    name: str
    pattern: str
    is_regex: bool = False
    case_sensitive: bool = False
    scope: Scope = "line"
    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    italic: bool = False
    priority: int = 0
    enabled: bool = True
    severity: Severity | None = None
    word_boundary: bool = False
    """
    Require a word boundary either side (`\b`).

    This exists so a severity word can be a **literal** and still not match
    `DEBUGGER`. The distinction matters well beyond matching: the index-time
    classifier compares fixed byte columns and can only take literals
    (SPEC 10.3), so a severity word expressed as a regex is paintable but
    cannot contribute to the `flags` the minimap reads. Literal plus boundary
    gives both.

    Ignored when the pattern does not begin and end with a word character,
    where `\b` would mean the opposite of what was asked: `\border=\b`
    demands a word character *after* the equals sign.
    """
    builtin: bool = False
    """
    Shipped with the application. Built-ins are never silently overwritten: an
    edit produces a user rule that shadows one by the same name, and "reset"
    drops the shadow (SPEC 10.1).
    """

    @property
    def style(self) -> Style:
        return Style(fg=self.fg, bg=self.bg, bold=self.bold, italic=self.italic)

    def regex_source(self) -> str:
        """
        This rule as a regex fragment, with its own case flag attached.

        A literal is escaped and otherwise left alone: no implied word
        boundaries, because a user typing `order=` means that substring. A rule
        asks for boundaries with `word_boundary`, which is what stops the
        built-in `DEBUG` from matching `DEBUGGER`.
        """
        body = self.pattern if self.is_regex else re.escape(self.pattern)
        if self.word_boundary and self._boundable():
            body = r"\b" + body + r"\b"
        return body if self.case_sensitive else f"(?i:{body})"

    def _boundable(self) -> bool:
        """
        Whether a word boundary either side would mean what was asked.

        It only does when the pattern begins and ends with a word
        character. Wrapping `order=` would demand a word character
        *after* the equals sign, the opposite of what the user typed.
        """
        p = self.pattern
        if not p:
            return False
        return ((p[0].isalnum() or p[0] == "_")
                and (p[-1].isalnum() or p[-1] == "_"))

    def validate(self) -> str:
        """Why this rule cannot be used, or "" if it can."""
        if not self.pattern:
            return "pattern is empty"
        try:
            compiled = re.compile(self.regex_source())
        except re.error as exc:
            return f"invalid regex: {exc}"
        if compiled.match(""):
            # A rule that matches nothing consumes nothing and would match at
            # every position, painting the whole file and finding nothing.
            return "pattern matches the empty string"
        return ""


class Ruleset:
    """
    A list of rules compiled into one scanner.

    Compilation is lazy and cached: a ruleset is edited in a dialog, where it
    changes many times and is painted with once.
    """

    def __init__(self, rules: list[HighlightRule] | None = None):
        self._rules: list[HighlightRule] = list(rules or [])
        self._compiled: re.Pattern | None = None
        self._by_group: dict[str, HighlightRule] = {}
        self._order: dict[str, int] = {}
        self._rejected: list[tuple[HighlightRule, str]] = []
        self._cache: OrderedDict[str, RowStyle] = OrderedDict()

    # -- editing (FR-31) ------------------------------------------------

    @property
    def rules(self) -> list[HighlightRule]:
        return list(self._rules)

    def add(self, rule: HighlightRule) -> None:
        self._rules.append(rule)
        self.invalidate()

    def remove(self, name: str) -> bool:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.name != name]
        self.invalidate()
        return len(self._rules) != before

    def replace_rule(self, name: str, **changes) -> bool:
        """
        Edit a rule in place, keeping its position.

        Editing a built-in marks the result as no longer built-in: it is then
        the user's rule, shadowing the shipped one by name, and `reset` can put
        the original back (SPEC 10.1).
        """
        for i, r in enumerate(self._rules):
            if r.name == name:
                if r.builtin:
                    changes.setdefault("builtin", False)
                self._rules[i] = replace(r, **changes)
                self.invalidate()
                return True
        return False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """
        Enabling or disabling a built-in does *not* make it a user rule.

        It is a view preference, not an edit to the rule, and treating it as an
        edit would mean "reset to default" silently re-enabled everything the
        user had turned off.
        """
        for i, r in enumerate(self._rules):
            if r.name == name:
                self._rules[i] = replace(r, enabled=enabled)
                self.invalidate()
                return True
        return False

    def move(self, name: str, delta: int) -> bool:
        """Reorder, which is the tiebreak when two rules share a priority."""
        for i, r in enumerate(self._rules):
            if r.name == name:
                j = max(0, min(len(self._rules) - 1, i + delta))
                if i == j:
                    return False
                self._rules.insert(j, self._rules.pop(i))
                self.invalidate()
                return True
        return False

    def invalidate(self) -> None:
        self._compiled = None
        self._by_group.clear()
        self._order.clear()
        self._rejected.clear()
        self._cache.clear()

    # -- compilation ----------------------------------------------------

    @property
    def rejected(self) -> list[tuple[HighlightRule, str]]:
        """Rules that would not compile, with the reason. Reported, not raised."""
        self._compile()
        return list(self._rejected)

    def _compile(self) -> None:
        if self._compiled is not None:
            return

        # `(?!)` can never match, which is what an empty or wholly rejected
        # ruleset should do -- and it spares every caller a None check.
        usable: list[tuple[int, HighlightRule]] = []
        for i, rule in enumerate(self._rules):
            if not rule.enabled:
                continue
            why = rule.validate()
            if why:
                # One bad rule must not stop the other forty-nine. A typo in a
                # regex while editing is the normal case, not an exception.
                self._rejected.append((rule, why))
                continue
            usable.append((i, rule))

        # Highest priority first, ties by rule order. A regex alternation takes
        # the first branch that matches at a given position, so this ordering is
        # what makes priority mean something at all -- compiled in list order, a
        # low-priority rule listed first would beat a high-priority one on every
        # line they both match.
        usable.sort(key=lambda pair: (-pair[1].priority, pair[0]))

        parts: list[str] = []
        for i, rule in usable:
            group = f"{_GROUP}{i}"
            self._by_group[group] = rule
            self._order[group] = i
            parts.append(f"(?P<{group}>{rule.regex_source()})")

        self._compiled = re.compile("|".join(parts) if parts else r"(?!)")

    # -- scanning -------------------------------------------------------

    def style_of(self, text: str) -> RowStyle:
        """
        The style for one row's text, memoised.

        One pass of one regex, whatever the rule count. The text is bounded to
        `SCAN_LIMIT` so a megabyte-long line costs the same as an ordinary one.
        """
        cached = self._cache.get(text)
        if cached is not None:
            self._cache.move_to_end(text)
            return cached

        style = self._scan(text)
        self._cache[text] = style
        if len(self._cache) > CACHE_ROWS:
            self._cache.popitem(last=False)
        return style

    def _scan(self, text: str) -> RowStyle:
        self._compile()
        assert self._compiled is not None
        if not self._by_group:
            return RowStyle()

        subject = text[:SCAN_LIMIT]
        line_winner: HighlightRule | None = None
        # The sentinel is the worst key there is, so the first match beats it.
        # Keys sort ascending with the winner first, so "worst" is the largest
        # tuple, not the smallest.
        line_key: tuple[int, int] = (1 << 30, 1 << 30)
        spans: list[Span] = []

        for m in self._compiled.finditer(subject):
            group = self._group_of(m)
            if group is None:
                continue
            rule = self._by_group[group]
            if rule.scope == "line":
                # Several line rules can match in different places, and the
                # whole row can only have one style, so they are compared.
                # Higher priority wins; among equals, the earlier rule wins.
                # Negating the priority makes one tuple comparison do both.
                key = (-rule.priority, self._order[group])
                if key < line_key:
                    line_key, line_winner = key, rule
            else:
                start, end = m.span(group)
                if end > start:
                    # Matches from one pass never overlap, so there is nothing
                    # to resolve here: the region is already this rule's, and
                    # the priority ordering of the alternation decided that.
                    spans.append(Span(start, end, rule.style, rule.name))

        return RowStyle(
            line=line_winner.style if line_winner else Style(),
            spans=tuple(spans),
            rule=line_winner.name if line_winner else "",
        )

    def _group_of(self, m: re.Match) -> str | None:
        """
        Which rule produced this match.

        `lastgroup` is the usual answer and costs nothing. It is the *last*
        matched group, though, so a user pattern containing its own named group
        reports that inner name instead -- hence the fallback, which is O(rules)
        but only runs for a pattern that has one.
        """
        group = m.lastgroup
        if group is not None and group in self._by_group:
            return group
        return next((g for g in self._by_group if m.group(g) is not None), None)

    # -- severity contribution (D11, Q2) --------------------------------

    def severity_words(self) -> dict[int, tuple[str, ...]]:
        """
        The literal rules that name a severity class, as an index-time ruleset.

        Only literals: the vectorised classifier compares fixed byte columns
        (SPEC 10.3), so a regex has no fast path to take. A regex rule carrying
        a severity is honoured for painting and reported as unable to
        contribute, rather than silently dropping the file onto the 2 MB/s
        fallback path.
        """
        out: dict[int, list[str]] = {}
        for rule in self._rules:
            if rule.severity is None or not rule.enabled or rule.is_regex:
                continue
            out.setdefault(int(rule.severity), []).append(rule.pattern.upper())
        return {k: tuple(dict.fromkeys(v)) for k, v in out.items()}

    def fingerprint(self) -> str:
        """
        A stable digest of everything that affects *indexing*, not painting.

        A cached index was built with a particular set of severity words. Change
        them and the saved `flags` disagree with the rules now in force -- the
        kind of staleness no size or mtime check can see -- so this goes in the
        sidecar header and a mismatch invalidates it (SPEC 5.4).
        """
        import hashlib

        words = self.severity_words()
        blob = ";".join(
            f"{level}:{','.join(sorted(ws))}" for level, ws in sorted(words.items())
        )
        return hashlib.blake2b(blob.encode("utf-8"), digest_size=8).hexdigest()


@dataclass
class RulesetGroup:
    """
    Several named rulesets, of which some are on (SPEC 10.1).

    Three ship -- severity words, exception shapes, syslog priorities -- and
    only the ones a user has enabled contribute. Kept as a group rather than one
    flat list so "turn off the syslog set" is one switch instead of eleven.
    """

    sets: dict[str, Ruleset] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)

    def flatten(self) -> Ruleset:
        rules: list[HighlightRule] = []
        for name, rs in self.sets.items():
            if name in self.active:
                rules.extend(rs.rules)
        return Ruleset(rules)
