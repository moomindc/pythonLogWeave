"""
Serialising a proven layout -- SPEC 5.4.

A sidecar covering a prefix of a grown file must index the appended tail with
*the layout the prefix was indexed with*. If the layout does not survive the
round trip exactly, the two halves of one index disagree about where the
timestamp sits, and the result is wrong times rather than an error. So the test
that matters is not "it round-trips", it is "it round-trips every field, for
every format in the corpus, and extracts identically afterwards".
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from logweave.index import extract as ex
from logweave.index.detect import detect
from logweave.index.layoutio import from_json, to_json
from logweave.index.layouts import Layout, Span
from logweave.index.lines import split_lines

SAMPLE = (
    "2024-03-01 09:15:01.123 INFO  worker started\n"
    "2024-03-01 09:15:02.456 WARN  queue depth 1200\n"
    "2024-03-01 09:15:03.789 ERROR upstream timeout\n"
) * 20


def parts(data: bytes):
    buf = np.frombuffer(data, dtype=np.uint8)
    starts, lens = split_lines(buf)
    return buf, starts, lens


def layout_of(data: bytes) -> Layout:
    buf, starts, lens = parts(data)
    det = detect(data, buf, starts, lens)
    assert det.layout is not None, "sample was not detected"
    return det.layout


@pytest.fixture
def layout() -> Layout:
    return layout_of(SAMPLE.encode("utf-8"))


def test_a_round_trip_restores_an_identical_layout(layout):
    assert from_json(to_json(layout)) == layout


def test_the_serialised_form_really_is_json(layout):
    """A dict of Python objects would round-trip here and fail in a sidecar."""
    assert from_json(json.loads(json.dumps(to_json(layout)))) == layout


def test_every_field_of_the_dataclass_is_carried(layout):
    """
    The one failure this file exists to prevent: a field added to `Layout` and
    not to the serialiser. It would come back as its default, silently, so the
    field list is asserted rather than trusted.
    """
    restored = from_json(to_json(layout))
    for field in dataclasses.fields(Layout):
        assert getattr(restored, field.name) == getattr(layout, field.name), \
            field.name


def test_an_absent_span_stays_absent(layout):
    """
    A format with no fractional part and no zone must not come back with a
    zero-width span at offset 0, which would read the wrong bytes rather than
    read nothing.
    """
    bare = dataclasses.replace(layout, frac=None, tz=None, epoch=None)
    restored = from_json(to_json(bare))
    assert restored.frac is None
    assert restored.tz is None
    assert restored.epoch is None


def test_a_present_span_keeps_its_padding_flag(layout):
    """
    `space_padded` is the single bool that decides whether " 1" reads as 1 or
    as garbage -- a syslog day field. Easy to drop, and invisible when wrong.
    """
    padded = dataclasses.replace(layout, day=Span(4, 2, True))
    restored = from_json(to_json(padded))
    assert restored.day == Span(4, 2, True)
    assert restored.day.space_padded is True


def test_the_separators_survive_as_a_tuple_of_pairs(layout):
    """
    JSON has no tuples, so `seps` goes out as lists. Coming back as lists would
    make the frozen dataclass unhashable and stop it comparing equal.
    """
    restored = from_json(to_json(layout))
    assert isinstance(restored.seps, tuple)
    assert all(isinstance(pair, tuple) for pair in restored.seps)
    assert restored.seps == layout.seps


def test_a_restored_layout_is_still_frozen(layout):
    restored = from_json(to_json(layout))
    with pytest.raises(dataclasses.FrozenInstanceError):
        restored.ts_width = 99


def test_semantics_comes_back_as_the_enum_not_a_string(layout):
    """
    `extract` branches on `semantics` against enum members. A plain string
    would compare unequal to every branch and route an epoch format down the
    civil path.
    """
    restored = from_json(to_json(layout))
    assert restored.semantics == layout.semantics
    assert type(restored.semantics) is type(layout.semantics)


def test_every_format_in_the_corpus_round_trips(corpus, manifests):
    """
    Not one layout -- every one the corpus can produce. Layouts are synthesised
    per file rather than declared, so each has its own arrangement of spans and
    the serialiser has to carry whatever synthesis came up with.
    """
    seen = 0
    for name in sorted(p.name for p in (corpus / "formats").glob("*.log")):
        data = (corpus / "formats" / name).read_bytes()
        buf, starts, lens = parts(data)
        det = detect(data, buf, starts, lens)
        if det.layout is None:
            continue
        assert from_json(to_json(det.layout)) == det.layout, name
        seen += 1
    assert seen >= 5, f"only {seen} formats exercised"


def test_a_restored_layout_extracts_the_same_timestamps(corpus):
    """
    Dataclass equality is the proof on paper. This is the proof that matters:
    the restored layout, run over the same bytes, yields the same timestamp for
    every line -- which is the only thing the append path actually relies on.
    """
    for path in sorted((corpus / "formats").glob("*.log")):
        data = path.read_bytes()
        buf, starts, lens = parts(data)
        det = detect(data, buf, starts, lens)
        if det.layout is None:
            continue
        before = ex.extract(buf, starts, lens, det.layout)
        after = ex.extract(buf, starts, lens, from_json(to_json(det.layout)))
        assert np.array_equal(before.ts, after.ts), path.name
        assert np.array_equal(before.valid, after.valid), path.name


def test_a_header_missing_a_field_is_an_error_not_a_default(layout):
    """
    A truncated or hand-edited header must fail where `cached._detection_for`
    catches it and re-indexes, rather than producing a plausible-looking layout
    with a zeroed field.
    """
    broken = to_json(layout)
    del broken["ts_offset"]
    with pytest.raises(KeyError):
        from_json(broken)
