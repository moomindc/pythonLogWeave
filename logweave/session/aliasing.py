"""
Short source names -- FR-29.

"The alias defaults to the shortest unique distinguishing part of the filename."
The useful word there is *distinguishing*: with one file open the alias may as
well be its stem, but open `prod/service.log` beside `staging/service.log` and
"service" tells you nothing. The part that distinguishes them is the directory,
so that is what the alias has to grow to include -- and only as far as it must,
because the whole point of an alias is that it is short enough to sit in a
narrow column and in a copied line's attribution.

Qt-free, at layer 4, because the aliases are what copy and export put in front
of every line (D9) as much as they are what the view shows.
"""

from __future__ import annotations

from pathlib import Path

SEPARATOR = "/"
"""
Always a forward slash, whatever the platform.

An alias is a label, not a path: it appears in a copied line's attribution,
which someone may well paste into a ticket read on another machine. A backslash
there also tends to be eaten by whatever markdown renders it.
"""


def _parts(path: Path) -> list[str]:
    """
    The name's components, innermost last, with the suffix off the filename.

    The suffix goes because `.log` distinguishes nothing in a log viewer. The
    *stem* keeps any inner dots, so `app.1.log` and `app.log` differ by their
    stems alone and never need a directory to tell them apart -- which is what
    makes a rotated pair read correctly without widening the column.
    """
    parts = [p for p in path.parent.parts if p not in ("/", "\\")]
    # A drive letter or UNC root distinguishes nothing a user reads by.
    parts = [p.rstrip(":\\/") for p in parts]
    return [p for p in parts if p] + [path.stem or path.name]


def alias_for(path: Path, depth: int = 1) -> str:
    """One name at a given depth: `depth=1` is the stem, 2 adds its directory."""
    parts = _parts(Path(path))
    return SEPARATOR.join(parts[-max(1, depth):])


def distinguishing_aliases(paths: list[Path | str]) -> list[str]:
    """
    The shortest aliases that tell these paths apart, in the order given.

    Depth is global rather than per-file: if two of five files need their
    directory, all five get it. Mixed depths would make the column a jumble of
    `service` and `staging/service` where the eye cannot tell whether the
    difference is meaningful. One extra directory on three files that did not
    need it is a much smaller cost than that.

    Two paths can be genuinely indistinguishable -- the same file opened twice,
    or two different mounts of one share. Those get a numeric suffix, because a
    column of identical names is worse than an ugly one.
    """
    resolved = [Path(p) for p in paths]
    if not resolved:
        return []

    # The shallowest depth that tells as many of them apart as any depth can.
    # "As any depth can" rather than "all of them", because two genuinely
    # identical paths never separate however deep you go, and growing every
    # other alias to full depth chasing them would widen the column for nothing.
    deepest = max(len(_parts(p)) for p in resolved)
    best_depth, best_distinct = 1, 0
    for depth in range(1, deepest + 1):
        distinct = len({alias_for(p, depth) for p in resolved})
        if distinct > best_distinct:
            best_depth, best_distinct = depth, distinct
        if distinct == len(resolved):
            break

    names = [alias_for(p, best_depth) for p in resolved]
    if len(set(names)) == len(names):
        return names

    # What is left is not distinguishable by path at all: the same file opened
    # twice, or two mounts of one share. A numeric suffix is ugly; a column of
    # identical names is worse.
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}~{seen[name]}")
    return out
