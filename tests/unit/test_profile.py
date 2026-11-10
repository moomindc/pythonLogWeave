"""
Profiles: the file, and what its globs mean today -- FR-49 to FR-53, SPEC 12.

The promises being held here are the ones a team notices when they break:

1. **A rewrite changes only what the user changed.** The file goes into a
   repository, so a save that reordered keys would turn every diff into noise
   and hide the one line that moved.
2. **Paths are absolute and verbatim** (D10). A relative path is refused rather
   than resolved against whatever directory a shortcut left behind.
3. **Opening collects failures instead of aborting** (FR-52). A profile naming
   eight files with one typo opens seven.
4. **A later version's profile still opens**, minus what this build does not
   understand, and says so.
"""

from __future__ import annotations

import time

import pytest

from logweave import profile as pf
from logweave.profile import model  # noqa: F401


def a_profile(**kw) -> pf.Profile:
    kw.setdefault("name", "Acme")
    kw.setdefault("sources", [pf.SourceSpec(path="C:/logs/app.log", alias="app")])
    return pf.Profile(**kw)


# ---------------------------------------------------------------------------
# the file (FR-53)
# ---------------------------------------------------------------------------

def test_a_profile_round_trips_through_text():
    p = a_profile(
        sources=[pf.SourceSpec(glob="C:/logs/gw-*.log", select="newest",
                               count=3, alias="gw", colour="#4C9AFF",
                               skew_us=-252_000_000, enabled=False),
                 pf.SourceSpec(path="C:/logs/iis.log", alias="iis",
                               encoding="utf-16", ts_format="iso_space_dot")],
        view=pf.ViewPrefs(timestamp_mode="delta", show_line_no=True,
                          lane_width=8),
        filters=[pf.SavedFilter(name="errors only", severities=("WARN",),
                                sources=("gw",), include="timeout")],
        bookmarks=[pf.Bookmark(source="gw", wall_us=1_793_123_456_789_012,
                               note="pool exhausted")],
    )
    back = pf.loads(pf.dump(p))

    assert back.name == "Acme"
    assert [s.target for s in back.sources] == ["C:/logs/gw-*.log",
                                                "C:/logs/iis.log"]
    assert back.sources[0].select == "newest" and back.sources[0].count == 3
    assert back.sources[0].skew_us == -252_000_000
    assert back.sources[0].enabled is False
    assert back.sources[1].encoding == "utf-16"
    assert back.sources[1].ts_format == "iso_space_dot"
    assert back.view.timestamp_mode == "delta"
    assert back.view.lane_width == 8
    assert back.filters[0].sources == ("gw",)
    assert back.bookmarks[0].note == "pool exhausted"


def test_writing_is_stable_so_a_diff_shows_only_what_changed():
    """
    The property that matters for a version-controlled file. If this fails,
    every save produces a diff nobody can review.
    """
    p = a_profile(view=pf.ViewPrefs(show_alias=True))
    once = pf.dump(p)
    assert pf.dump(pf.loads(once)) == once


def test_a_value_with_quotes_and_newlines_survives():
    """
    A filter's regex is user text that lands in a TOML string, so the escaping
    is the one piece of this module that silently corrupts data if it is wrong.
    """
    nasty = 'say "hello"\\n\tnow'
    p = a_profile(filters=[pf.SavedFilter(name='odd "name"', include=nasty)])
    back = pf.loads(pf.dump(p))
    assert back.filters[0].include == nasty
    assert back.filters[0].name == 'odd "name"'


def test_a_file_that_is_not_toml_is_refused_with_a_reason():
    with pytest.raises(pf.ProfileError) as caught:
        pf.loads("this is not = = toml")
    assert "TOML" in str(caught.value)


def test_a_missing_file_is_refused_with_a_reason(tmp_path):
    with pytest.raises(pf.ProfileError):
        pf.load(tmp_path / "nope.lwprofile")


def test_saving_adds_the_extension_and_replaces_atomically(tmp_path):
    path = pf.save(a_profile(), tmp_path / "acme")
    assert path.name == "acme.lwprofile"
    assert not list(tmp_path.glob("*.tmp")), "no half-written file left behind"
    assert pf.load(path).name == "Acme"


def test_an_unknown_key_is_kept_out_of_the_way_rather_than_fatal():
    """
    A team on mixed versions has to be able to share one file, so a key from a
    later build is a note rather than a refusal.
    """
    got = pf.loads("""
        format_version = 1
        name = "Later"
        teleport = true
        [[source]]
        path = "C:/logs/app.log"
        hologram = "on"
    """)
    assert got.name == "Later"
    assert [s.path for s in got.sources] == ["C:/logs/app.log"]
    assert any("teleport" in n for n in got.notes)
    assert any("hologram" in n for n in got.notes)


def test_a_later_format_version_opens_and_warns():
    got = pf.loads('format_version = 99\n[[source]]\npath = "C:/logs/a.log"\n')
    assert any("later version" in why for why in got.validate())


def test_a_remote_source_is_named_rather_than_dropped():
    """
    FR-7a is not built. Five files written and four opened is something the
    reader has to be told about, or they will go looking for the fifth.
    """
    got = pf.loads("""
        [[source]]
        ssh = "deploy@db01:/var/log/postgresql.log"
        [[source]]
        path = "C:/logs/app.log"
    """)
    assert len(got.sources) == 1
    assert any("db01" in n for n in got.notes)


def test_comments_a_user_added_are_counted_before_they_are_lost(tmp_path):
    path = pf.save(a_profile(), tmp_path / "acme.lwprofile")
    assert pf.comments_at_risk(path) == 0, "the generated header is not at risk"
    path.write_text(path.read_text(encoding="utf-8")
                    + "\n# why this profile exists\n# ask the gateway team\n",
                    encoding="utf-8")
    assert pf.comments_at_risk(path) == 2


# ---------------------------------------------------------------------------
# validation (D10, FR-50)
# ---------------------------------------------------------------------------

def test_a_relative_path_is_refused_rather_than_resolved():
    """
    The current directory of a GUI is whatever a shortcut left behind, so
    quietly opening the wrong file is the failure being prevented here.
    """
    why = pf.SourceSpec(path="logs/app.log").validate()
    assert "absolute" in why


@pytest.mark.parametrize("spec,expected", [
    (pf.SourceSpec(), "neither"),
    (pf.SourceSpec(path="C:/a.log", glob="C:/*.log"), "not both"),
    (pf.SourceSpec(glob="C:/*.log", select="sideways"), "unknown selection"),
    (pf.SourceSpec(glob="C:/*.log", select="newest", count=0), "at least one"),
    (pf.SourceSpec(glob="C:/*.log", select="modified_within", within="soon"),
     "not a duration"),
    (pf.SourceSpec(path="C:/a.log", select="newest"), "names one file"),
])
def test_what_a_source_may_not_say(spec, expected):
    assert expected in spec.validate()


def test_two_sources_may_not_share_an_alias():
    p = a_profile(sources=[pf.SourceSpec(path="C:/a.log", alias="x"),
                           pf.SourceSpec(path="C:/b.log", alias="x")])
    assert any("share an alias" in why for why in p.validate())


@pytest.mark.parametrize("text,seconds", [
    ("30m", 1800), ("12h", 43_200), ("7d", 604_800), ("2w", 1_209_600),
    ("1.5h", 5400), ("", None), ("7", None), ("d", None), ("-3d", None),
    ("soon", None),
])
def test_durations_are_the_few_shapes_a_person_writes(text, seconds):
    assert model.parse_duration(text) == seconds


# ---------------------------------------------------------------------------
# resolving against the disk (FR-50, FR-51, FR-52)
# ---------------------------------------------------------------------------

@pytest.fixture
def logs(tmp_path):
    """
    Four rotated gateway logs and one IIS log, with staggered mtimes.

    The newest is a day old rather than new, so a window shorter than a day
    genuinely excludes everything -- a fixture whose newest file was written
    "now" could not express that case at all.
    """
    now = time.time()
    made = {}
    for i, age_days in enumerate((1, 2, 9, 30)):
        path = tmp_path / f"gw-{i}.log"
        path.write_text(f"2026-10-04 10:00:0{i}.000000 INFO row\n",
                        encoding="utf-8")
        import os
        os.utime(path, (now - age_days * 86_400, now - age_days * 86_400))
        made[path.name] = path
    iis = tmp_path / "iis.log"
    iis.write_text("2026-10-04 10:00:00.000000 INFO row\n", encoding="utf-8")
    made[iis.name] = iis
    return tmp_path, made, now


def test_a_glob_resolves_to_what_exists_today(logs):
    root, _, _ = logs
    got = pf.resolve(a_profile(sources=[
        pf.SourceSpec(glob=str(root / "gw-*.log"))]))
    assert [p.name for p in got.paths] == ["gw-0.log", "gw-1.log",
                                           "gw-2.log", "gw-3.log"]
    assert got.complete


def test_newest_takes_the_newest_and_then_sorts_by_name(logs):
    """
    "The newest three" is a question about *which* files. Reading them in
    mtime order as well would give every source a different lane colour each
    morning.
    """
    root, _, _ = logs
    got = pf.resolve(a_profile(sources=[
        pf.SourceSpec(glob=str(root / "gw-*.log"), select="newest", count=2)]))
    assert [p.name for p in got.paths] == ["gw-0.log", "gw-1.log"]


def test_modified_within_keeps_the_recent_ones(logs):
    root, _, now = logs
    got = pf.resolve(a_profile(sources=[
        pf.SourceSpec(glob=str(root / "gw-*.log"), select="modified_within",
                      within="7d")]), now=now)
    assert [p.name for p in got.paths] == ["gw-0.log", "gw-1.log"]


def test_a_file_named_twice_is_opened_once(logs):
    """Opening it twice duplicates every row, which reads as the log repeating
    itself rather than as a profile mistake."""
    root, made, _ = logs
    got = pf.resolve(a_profile(sources=[
        pf.SourceSpec(glob=str(root / "gw-*.log")),
        pf.SourceSpec(path=str(made["gw-1.log"]))]))
    assert len(got.paths) == 4


def test_the_rest_opens_when_one_source_does_not(logs):
    root, made, _ = logs
    got = pf.resolve(a_profile(sources=[
        pf.SourceSpec(path=str(made["iis.log"])),
        pf.SourceSpec(path=str(root / "typo.log")),
        pf.SourceSpec(glob=str(root / "nothing-*.log")),
        pf.SourceSpec(path="logs/relative.log"),
    ]))
    assert [p.name for p in got.paths] == ["iis.log"]
    assert got.missing == [str(root / "typo.log")]
    assert got.unmatched == [str(root / "nothing-*.log")]
    assert len(got.invalid) == 1 and "absolute" in got.invalid[0]
    assert got.ok and not got.complete
    assert "1 file" in got.summary() and "1 missing" in got.summary()


def test_a_policy_that_excludes_everything_says_so(logs):
    root, _, now = logs
    got = pf.resolve(a_profile(sources=[
        pf.SourceSpec(glob=str(root / "gw-*.log"), select="modified_within",
                      within="1h")]), now=now)
    assert not got.paths
    assert "matched 4" in got.unmatched[0]


def test_freezing_replaces_globs_with_what_they_found(logs):
    """
    FR-49 saves the session that is open, and what is open is files. Writing
    the glob back would save something the reader never checked.
    """
    root, _, _ = logs
    p = a_profile(sources=[pf.SourceSpec(glob=str(root / "gw-*.log"),
                                         select="newest", count=2, alias="gw")])
    got = pf.resolve(p)
    frozen = pf.freeze(p, got)
    assert [s.path for s in frozen.sources] == [str(path) for path in got.paths]
    assert all(not s.is_glob and s.select == "all" for s in frozen.sources)
    assert all(s.alias == "gw" for s in frozen.sources)
    assert p.sources[0].is_glob, "the original profile is left alone"
