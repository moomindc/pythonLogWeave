"""
Serialising a proven `Layout` -- SPEC 5.4.

A sidecar that covers only a prefix of a grown file has to index the appended
tail *with the same layout the prefix was indexed with*. Re-detecting on the
tail alone is not good enough: detection samples what it is given, and a tail
of a few hundred bytes can plausibly resolve to a different member of the ISO
family than the whole file did. The result would be one index whose first half
and second half disagree about where the timestamp is -- which produces wrong
times rather than an error.

So the layout travels in the sidecar header. It is a frozen dataclass of ints,
strings and small tuples, which is exactly what JSON is for.
"""

from __future__ import annotations

from .layouts import Layout, Semantics, Span

_SPANS = ("year", "month", "day", "hour", "minute", "second", "frac", "tz", "epoch")
_SCALARS = ("fmt_name", "ts_width", "ts_offset", "anchor_byte", "anchor_skip",
            "month_name", "infer_year", "infer_date")


def to_json(layout: Layout) -> dict:
    out: dict = {k: getattr(layout, k) for k in _SCALARS}
    out["semantics"] = str(layout.semantics)
    out["seps"] = [list(pair) for pair in layout.seps]
    for name in _SPANS:
        span = getattr(layout, name)
        out[name] = None if span is None else [span.pos, span.width, span.space_padded]
    return out


def from_json(data: dict) -> Layout:
    kwargs: dict = {k: data[k] for k in _SCALARS}
    kwargs["semantics"] = Semantics(data["semantics"])
    kwargs["seps"] = tuple(tuple(pair) for pair in data["seps"])
    for name in _SPANS:
        raw = data.get(name)
        kwargs[name] = None if raw is None else Span(raw[0], raw[1], bool(raw[2]))
    return Layout(**kwargs)
