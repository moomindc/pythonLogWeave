"""
Turning a profile's sources into files that exist -- FR-50 to FR-52. Qt-free.

A profile is written once and opened on many days, against a directory whose
contents rotate. `C:/Acme/logs/gateway-*.log` is not a list of files; it is a
question asked again every time the profile opens (G8, S5), and the answer is
different on Tuesday.

## Collect, never abort (FR-52)

Every failure here is recorded and the rest goes on. A profile naming eight
files with one typo must open seven, because the reader is usually trying to
look at an incident and the missing file is usually not the one it is in. The
UI reports what happened in one panel and lets them retry.

Three outcomes are kept apart because they mean different things:

* **missing** -- a named path that is not there. Probably a mistake.
* **unmatched** -- a glob that matched nothing. Probably Tuesday.
* **invalid** -- a spec that cannot be resolved at all, such as a relative
  path (D10). Always a mistake, and always in the file rather than on the disk.

## Order is the profile's, not the disk's

Resolved files keep the order their specs appear in, and within one glob they
are sorted: by name for `all`, and by modification time for the policies that
are about time. A profile that opens its files in a different order on
different days would give every source a different lane colour each morning.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .model import (
    SELECT_NEWEST,
    SELECT_WITHIN,
    Profile,
    SourceSpec,
    parse_duration,
)


@dataclass
class Resolution:
    """What a profile's sources turned out to be, including what did not."""

    opened: list[tuple[Path, SourceSpec]] = field(default_factory=list)
    """Files to open, in profile order, each with the spec that produced it."""
    missing: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        return [p for p, _ in self.opened]

    @property
    def ok(self) -> bool:
        return bool(self.opened)

    @property
    def complete(self) -> bool:
        """Whether every spec in the profile produced at least one file."""
        return not (self.missing or self.unmatched or self.invalid)

    def summary(self) -> str:
        """One line for the status bar; the panel shows the detail."""
        bits = [f"{len(self.opened)} file{'' if len(self.opened) == 1 else 's'}"]
        for label, items in (("missing", self.missing),
                             ("unmatched", self.unmatched),
                             ("invalid", self.invalid)):
            if items:
                bits.append(f"{len(items)} {label}")
        return ", ".join(bits)


def resolve(profile: Profile, *, now: float | None = None) -> Resolution:
    """
    Resolve every source in `profile` against the disk as it is right now.

    `now` is injectable for the `modified_within` policy, so a test can state
    what "within 7 days" means instead of writing files with doctored mtimes
    and hoping.
    """
    out = Resolution()
    seen: set[str] = set()
    for spec in profile.sources:
        if (why := spec.validate()):
            out.invalid.append(why)
            continue
        for path in _resolve_one(spec, out, now=now):
            # A file named twice -- by a glob and by a path, or by two
            # overlapping globs -- is opened once. Opening it twice would
            # duplicate every one of its rows in the merge, which looks like
            # the log repeating itself rather than like a profile mistake.
            key = _key(path)
            if key in seen:
                continue
            seen.add(key)
            out.opened.append((path, spec))
    return out


def _key(path: Path) -> str:
    """
    One comparable name for a file, for the dedupe above.

    `Path.resolve()` would be the obvious call and is the wrong one: it asks
    the filesystem, and on Windows that is roughly 0.25 ms a path -- 0.55 s to
    resolve one glob over 2000 files, against 15 ms for the whole resolve
    without it (SPEC 14.1i). A profile pointed at a log directory nobody prunes
    is exactly the case this has to be quick for.

    `normcase(normpath(abspath))` is string work and catches what actually
    happens: the same file reached as `C:/logs/a.log` and `C:/logs/../logs/a.log`,
    or spelled with different case. It does **not** see through a junction or a
    symlink, so a profile that names one directory twice under two different
    mount points would open those files twice. That is a rare way to write a
    profile and a visible one when it happens -- the source list shows the file
    twice, under two names -- whereas half a second of lag on every open is
    neither rare nor visible.
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _resolve_one(spec: SourceSpec, out: Resolution, *,
                 now: float | None) -> list[Path]:
    if not spec.is_glob:
        path = Path(spec.path)
        if path.is_file():
            return [path]
        out.missing.append(spec.path)
        return []

    matches = _expand(spec.glob)
    if not matches:
        out.unmatched.append(spec.glob)
        return []
    chosen = _select(matches, spec, now=now)
    if not chosen:
        # The glob matched, and the policy then excluded everything -- a
        # different thing from a glob that matched nothing, and reported
        # differently, because the fix is a different one.
        out.unmatched.append(
            f"{spec.glob} (matched {len(matches)}, none within "
            f"{spec.within})" if spec.select == SELECT_WITHIN else spec.glob)
    return chosen


def _expand(pattern: str) -> list[Path]:
    """
    Every existing file matching an absolute glob, sorted by name.

    Split by hand rather than `Path.glob` on the whole string, because that
    method wants a root and a relative pattern, and a Windows profile's root is
    `C:/` or a UNC share. The first component with a wildcard in it is where
    the pattern starts; everything before it is the root.
    """
    path = Path(pattern)
    parts = path.parts
    for i, part in enumerate(parts):
        if any(ch in part for ch in "*?["):
            root = Path(*parts[:i]) if i else Path(path.anchor)
            rest = str(Path(*parts[i:]))
            try:
                return sorted((p for p in root.glob(rest) if p.is_file()),
                              key=lambda p: str(p).lower())
            except (OSError, ValueError):
                # A pattern the platform refuses -- an invalid drive, a
                # recursion the OS rejects. Treated as "matched nothing",
                # which is what the caller then reports.
                return []
    return [path] if path.is_file() else []


def _select(matches: list[Path], spec: SourceSpec, *,
            now: float | None) -> list[Path]:
    """FR-51: all of them, the newest few, or the recently written ones."""
    if spec.select == SELECT_NEWEST:
        newest = sorted(matches, key=_mtime, reverse=True)[:max(1, spec.count)]
        # Sorted back into name order afterwards: "the newest three" is a
        # question about which files, not about what order to read them in,
        # and a stable order is what keeps the lane colours still.
        return sorted(newest, key=lambda p: str(p).lower())
    if spec.select == SELECT_WITHIN:
        window = parse_duration(spec.within) or 0
        cutoff = (time.time() if now is None else now) - window
        return [p for p in matches if _mtime(p) >= cutoff]
    return matches


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        # A file that vanished between the glob and the stat sorts oldest, so
        # a rotation mid-resolve cannot promote a file that is no longer there.
        return 0.0


def freeze(profile: Profile, resolution: Resolution) -> Profile:
    """
    A copy of `profile` with its globs replaced by what they resolved to.

    For "save the session I am looking at" (FR-49): the files that are open
    are paths, and writing them back as a glob would save something the reader
    did not check. The original glob is a different profile, deliberately
    kept -- which is why this returns a copy rather than editing in place.
    """
    specs = [replace(spec, path=str(path), glob="", select="all")
             for path, spec in resolution.opened]
    return replace(profile, sources=specs)
