"""
Short source names -- FR-29.

The requirement is "the shortest unique distinguishing part of the filename",
and the word doing the work is *distinguishing*. One file can be called by its
stem; open `prod/service.log` beside `staging/service.log` and the stem tells
you nothing, so the alias has to grow -- and only as far as it must, because an
alias appears in a narrow column and in front of every copied line (D9).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import BytesSource

from logweave.session import Session
from logweave.session.aliasing import alias_for, distinguishing_aliases


def names(*paths: str) -> list[str]:
    return distinguishing_aliases([Path(p) for p in paths])


# ---------------------------------------------------------------------------
# the algorithm
# ---------------------------------------------------------------------------

def test_a_single_file_is_called_by_its_stem():
    assert names("C:/logs/app.log") == ["app"]


def test_distinct_stems_need_no_directory():
    assert names("/var/log/app.log", "/var/log/db.log") == ["app", "db"]


def test_a_collision_grows_both_names_by_one_directory():
    assert names("/srv/prod/service.log", "/srv/staging/service.log") == \
        ["prod/service", "staging/service"]


def test_growth_continues_until_the_names_differ():
    assert names("/a/x/one/svc.log", "/a/y/one/svc.log") == \
        ["x/one/svc", "y/one/svc"]


def test_depth_is_the_same_for_every_source():
    """
    Mixed depths would make the column a jumble of `service` and
    `staging/service`, where the eye cannot tell whether the difference is
    meaningful or an artefact. One extra directory on the files that did not
    need it costs far less than that ambiguity.
    """
    got = names("/srv/prod/service.log", "/srv/staging/service.log",
                "/srv/prod/db.log")
    assert got == ["prod/service", "staging/service", "prod/db"]


def test_a_rotated_pair_is_distinguished_without_a_directory():
    """
    `app.log` and `app.1.log` differ in their stems, so the suffix alone does
    it. Growing these to include a directory would widen the column for every
    source to tell apart two files that were already distinct.
    """
    assert names("/var/log/app.log", "/var/log/app.1.log") == ["app", "app.1"]


def test_the_separator_is_always_a_forward_slash():
    """
    An alias is a label, not a path. It ends up in a copied line that someone
    pastes into a ticket read on another machine, and a backslash there tends
    to be eaten by whatever renders it.
    """
    got = names("C:/srv/prod/service.log", "C:/srv/staging/service.log")
    assert all("\\" not in name for name in got)
    assert got == ["prod/service", "staging/service"]


def test_a_drive_or_share_root_is_not_treated_as_a_distinguishing_part():
    got = names("C:/logs/one/svc.log", "C:/logs/two/svc.log")
    assert got == ["one/svc", "two/svc"]


def test_genuinely_identical_paths_get_a_suffix_rather_than_one_name():
    """
    The same file opened twice, or two mounts of one share. A column of
    identical names is worse than an ugly one.
    """
    got = names("/var/log/app.log", "/var/log/app.log")
    assert got[0] == "app"
    assert got[1] != got[0]
    assert len(set(got)) == 2


def test_an_empty_list_is_not_an_error():
    assert names() == []


def test_a_bare_filename_with_no_directory_still_works():
    assert names("syslog") == ["syslog"]
    assert names("syslog", "messages") == ["syslog", "messages"]


def test_a_file_with_no_extension_keeps_its_whole_name():
    """`syslog`, `messages` and `dmesg` have no suffix at all."""
    assert alias_for(Path("/var/log/syslog")) == "syslog"
    assert alias_for(Path("/var/log/syslog"), 2) == "log/syslog"


def test_order_is_preserved():
    got = names("/b/svc.log", "/a/svc.log")
    assert got == ["b/svc", "a/svc"]


# ---------------------------------------------------------------------------
# in the session
# ---------------------------------------------------------------------------

def make(tmp_path: Path, rel: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"2024-03-01 09:15:{i:02d}.000 INFO  line {i}\n" for i in range(5)),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def session(tmp_path):
    s = Session(cache_root=tmp_path / "cache")
    yield s
    s.close()


def test_opening_a_second_file_can_lengthen_the_firsts_alias(session, tmp_path):
    """
    The case that makes this more than a one-shot naming function: what
    distinguishes the files already open changes when another arrives.
    """
    session.add_files([make(tmp_path, "prod/service.log")])
    assert session.sources[0].alias == "service"

    session.add_files([make(tmp_path, "staging/service.log")])
    assert [s.alias for s in session.sources] == ["prod/service", "staging/service"]


def test_a_renamed_source_is_never_recomputed(session, tmp_path):
    """
    The user has said what they want it called. Silently widening it when
    another file arrives would undo that for no reason they could see.
    """
    session.add_files([make(tmp_path, "prod/service.log")])
    assert session.set_alias(0, "prod") == "prod"

    session.add_files([make(tmp_path, "staging/service.log")])
    assert [s.alias for s in session.sources] == ["prod", "staging/service"]
    assert session.sources[0].meta.alias_custom is True


def test_clearing_an_alias_restores_the_computed_one(session, tmp_path):
    session.add_files([make(tmp_path, "prod/service.log"),
                       make(tmp_path, "staging/service.log")])
    session.set_alias(0, "whatever")
    assert session.sources[0].alias == "whatever"

    assert session.set_alias(0, "") == "prod/service"
    assert session.sources[0].meta.alias_custom is False


def test_whitespace_is_not_an_alias(session, tmp_path):
    session.add_files([make(tmp_path, "app.log")])
    assert session.set_alias(0, "   ") == "app"
    assert session.sources[0].meta.alias_custom is False


def test_the_alias_is_what_copy_attributes_to(session, tmp_path):
    """D9: the alias is not only a column, it is what a pasted line is cited by."""
    from logweave.session.rowformat import RowFormatter, SourceNaming

    session.add_files([make(tmp_path, "prod/service.log")])
    session.set_alias(0, "svc")

    text = RowFormatter(session, naming=SourceNaming.ALIAS).format(
        session.rows(0, 1))
    assert text.startswith("svc:1")


def test_an_alias_survives_a_source_being_disabled(session, tmp_path):
    session.add_files([make(tmp_path, "a.log"), make(tmp_path, "b.log")])
    session.set_alias(1, "bee")
    session.set_enabled(1, False)
    assert session.sources[1].alias == "bee"


def test_renaming_an_index_that_does_not_exist_is_refused(session, tmp_path):
    session.add_files([make(tmp_path, "a.log")])
    with pytest.raises(IndexError):
        session.set_alias(9, "nope")


def test_the_stub_source_is_not_required_for_any_of_this():
    """Aliasing is pure path arithmetic -- no file has to exist."""
    assert names("/nowhere/at/all/x.log") == ["x"]
    assert BytesSource is not None
