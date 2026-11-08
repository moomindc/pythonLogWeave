"""
User highlight rules, as text -- FR-31, SPEC 10.1.

Qt-free on purpose, like the rest of this package. The *store* is a string; who
keeps it is the UI's business (`QSettings` today, a profile at FR-49, a TOML
file when SPEC 10.1's import/export lands). That split is why this module has
no idea what a registry is, and why a round-trip can be tested without a
widget toolkit.

JSON rather than TOML, for now. TOML is the format SPEC 10.1 wants for sharing
a ruleset between people, and it needs a writer -- `tomllib` is read-only --
which is a dependency decision (D8) rather than a formatting one. What is
needed first is that a rule a user typed survives a restart, and `json` is in
the standard library.

## Nothing here trusts what it reads

The store is a registry value or a text file, so it is user-editable by
construction, and a hand-edited rule must not be able to stop the application
opening a log. Every field is read defensively and an unreadable rule is
dropped with a reason rather than raised: `load` returns the rules it could
make and the complaints it had, and the caller reports them. This is the same
posture as `Ruleset.rejected`, which already declines to let one bad regex stop
the other forty-nine.
"""

from __future__ import annotations

import json
from dataclasses import fields

from ..index.severity import Severity
from .rules import HighlightRule

VERSION = 1
"""
Schema version, written and checked.

One field's meaning changing is the realistic failure here -- not a wholesale
format change -- and a version is what lets a later reader migrate instead of
silently applying the old meaning of a flag.
"""

_FIELDS: dict[str, type] = {
    "name": str,
    "pattern": str,
    "is_regex": bool,
    "case_sensitive": bool,
    "word_boundary": bool,
    "scope": str,
    "fg": str,
    "bg": str,
    "bold": bool,
    "italic": bool,
    "priority": int,
    "enabled": bool,
    "on_strips": bool,
}
"""
What is persisted, and deliberately not everything a rule has.

`builtin` is not here: a stored rule is a user rule by definition, and
accepting `builtin: true` from a text file would let a hand edit claim the
protection SPEC 10.1 gives shipped rules. `severity` is handled separately
below because it is an enum and because it is the one field that invalidates a
cached index.
"""


_DEFAULTS = {f.name: f.default for f in fields(HighlightRule)}
"""
Each field's shipped default, which is what `dump` leaves out.

Against the **default**, not against anything falsy. Omitting every falsy value
was the first version and it dropped `enabled=False` -- the one bool here whose
default is True -- so a rule the user had switched off came back on at the next
start, looking for all the world like the switch had not been clicked. A field
is written whenever it differs from what a reader would assume, and `enabled` is
the field that makes the distinction matter.
"""


def dump(rules: list[HighlightRule]) -> str:
    """The user's rules as a JSON string, stable enough to diff."""
    out = []
    for r in rules:
        row: dict[str, object] = {}
        for field in _FIELDS:
            value = getattr(r, field)
            # Absent rather than null: a rule at its default should read as not
            # mentioning the field at all, so a later schema may change a
            # default without every old rule pinning the previous one.
            if value != _DEFAULTS[field]:
                row[field] = value
        # The name is the handle for everything, so it is always written even
        # on the vanishingly unlikely rule named "".
        row["name"] = r.name
        if r.severity is not None:
            row["severity"] = Severity(r.severity).name
        out.append(row)
    return json.dumps({"version": VERSION, "rules": out},
                      indent=1, sort_keys=True)


def load(text: object) -> tuple[list[HighlightRule], list[str]]:
    """
    Rules from a stored string, plus a complaint per rule that was dropped.

    `text` is typed `object` because that is what comes back out of a settings
    store: a string normally, `None` on a first run, and whatever a hand edit
    left behind otherwise. All three are "no rules yet" and none of them is an
    error.
    """
    if not isinstance(text, str) or not text.strip():
        return [], []
    try:
        blob = json.loads(text)
    except (ValueError, TypeError) as exc:
        return [], [f"highlight rules could not be read: {exc}"]
    if not isinstance(blob, dict):
        return [], ["highlight rules are not an object"]

    version = blob.get("version")
    if isinstance(version, int) and version > VERSION:
        # Forward-compatible by refusing, not by guessing: a rule written by a
        # newer version may mean something different by a field this one knows.
        return [], [f"highlight rules were written by a newer version "
                    f"(v{version}; this is v{VERSION})"]

    raw = blob.get("rules")
    if not isinstance(raw, list):
        return [], ["highlight rules contain no rule list"]

    rules: list[HighlightRule] = []
    notes: list[str] = []
    seen: set[str] = set()
    for i, row in enumerate(raw):
        rule, why = _one(row, i)
        if rule is None:
            notes.append(why)
            continue
        if rule.name in seen:
            # Names are the handle `Ruleset.remove`, `replace_rule` and
            # `set_enabled` all take, so a duplicate would make an edit land on
            # whichever copy came first and look like it had done nothing.
            notes.append(f"rule {i + 1}: duplicate name {rule.name!r}, dropped")
            continue
        seen.add(rule.name)
        rules.append(rule)
    return rules, notes


def _one(row: object, i: int) -> tuple[HighlightRule | None, str]:
    if not isinstance(row, dict):
        return None, f"rule {i + 1}: not an object"

    kwargs: dict[str, object] = {}
    for field, want in _FIELDS.items():
        if field not in row:
            continue
        value = row[field]
        if want is bool:
            # Only a real bool. A JSON `1` here means a hand edit, and coercing
            # it would be guessing at intent on a file we have just decided not
            # to trust.
            if not isinstance(value, bool):
                return None, f"rule {i + 1}: {field} is not true or false"
        elif want is int:
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f"rule {i + 1}: {field} is not a whole number"
        elif not isinstance(value, str):
            return None, f"rule {i + 1}: {field} is not text"
        kwargs[field] = value

    name = str(kwargs.get("name", "")).strip()
    if not name:
        return None, f"rule {i + 1}: no name"
    kwargs["name"] = name

    if kwargs.get("scope") not in (None, "line", "match"):
        return None, f"rule {i + 1}: scope must be 'line' or 'match'"

    sev = row.get("severity")
    if sev is not None:
        try:
            kwargs["severity"] = Severity[str(sev).upper()]
        except KeyError:
            return None, f"rule {i + 1}: {sev!r} is not one of the eight classes"

    rule = HighlightRule(**kwargs)           # type: ignore[arg-type]
    why = rule.validate()
    if why:
        # Kept out of the ruleset rather than discarded quietly: the user typed
        # it, so the complaint names the rule they will be looking for.
        return None, f"rule {name!r}: {why}"
    return rule, ""
