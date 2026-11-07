"""
Tests for ``logweave.source.winfile`` -- SPEC 7.1, 7.1.1, 7.2; NFR-20, FR-4a.

``test_shared_handle_does_not_block_rename_or_delete`` is the one that earns this
module's existence: it is the difference between a log viewer that is safe to
leave running against a production log and one that breaks its rotation.
"""

from __future__ import annotations

import mmap
import os
import sys

import pytest

from logweave.source import winfile
from logweave.source.base import OpenMethod, SourceError

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows share-mode behaviour")

BODY = b"2026-10-04 12:00:00.000000 INFO hello\n" * 64


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "app.log"
    path.write_bytes(BODY)
    return path


# -- rung 1: the share modes -----------------------------------------------


def test_open_fd_reads_the_file(log):
    fd = winfile.open_fd(log)
    try:
        assert winfile.pread(fd, 0, len(BODY)) == BODY
    finally:
        os.close(fd)


def test_open_shared_returns_a_file_object(log):
    with winfile.open_shared(log) as handle:
        assert handle.read() == BODY


def test_open_fd_on_a_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        winfile.open_fd(tmp_path / "nope.log")


@pytest.mark.windows
@WINDOWS_ONLY
def test_shared_handle_does_not_block_rename_or_delete(log):
    """
    NFR-20, the whole point of this module.

    A handle opened without FILE_SHARE_DELETE makes the writer's own rotation
    fail with ERROR_SHARING_VIOLATION. This asserts that ours does not: the file
    can be renamed and then deleted while we hold it open, and our handle keeps
    reading the bytes afterwards, which is what SPEC 7.2 relies on for "rotated
    away or deleted -- keep reading the open handle to EOF".
    """
    fd = winfile.open_fd(log)
    try:
        assert winfile.pread(fd, 0, 16) == BODY[:16]

        rotated = log.parent / (log.name + ".1")
        os.rename(log, rotated)          # the writer's rotation step
        assert not log.exists()
        assert rotated.exists()

        os.remove(rotated)               # the writer's retention step

        # The handle outlives both. This is the behaviour SPEC 7.2 depends on.
        assert winfile.pread(fd, 0, len(BODY)) == BODY
        assert os.fstat(fd).st_size == len(BODY)
    finally:
        os.close(fd)


@pytest.mark.windows
@WINDOWS_ONLY
def test_a_memory_mapping_over_the_shared_handle_still_allows_rotation(log):
    """
    NFR-20 against SPEC 3.1's no-syscall read path.

    These could have been in conflict: a mapped section normally pins a file on
    Windows. It does not when the underlying handle granted FILE_SHARE_DELETE,
    which is what lets ``FileSource`` mmap local files at all.
    """
    fd = winfile.open_fd(log)
    mapping = mmap.mmap(fd, len(BODY), access=mmap.ACCESS_READ)
    try:
        rotated = log.parent / (log.name + ".1")
        os.rename(log, rotated)
        os.remove(rotated)
        assert mapping[0:16] == BODY[:16]
    finally:
        mapping.close()
        os.close(fd)


@pytest.mark.windows
@WINDOWS_ONLY
def test_builtin_open_is_the_trap_this_module_avoids(log):
    """
    Documents the bug, so a future "just use open()" simplification fails here.

    Python's ``open()`` goes through the CRT, which omits FILE_SHARE_DELETE.
    """
    with open(log, "rb"), pytest.raises(OSError):
        os.rename(log, log.parent / (log.name + ".1"))


# -- the SPEC 7.1.1 ladder --------------------------------------------------


def _fake_opener(outcomes, log):
    """
    Stand in for ``open_fd``, consuming one outcome per call.

    ``"ok"`` yields a real descriptor; anything else is raised. Driving the
    ladder through the one primitive it uses means the rung logic under test is
    the real thing, and it works identically on Linux CI, where no Win32 error
    exists.
    """
    calls: list[bool] = []

    def opener(path, *, backup_semantics=False):
        calls.append(backup_semantics)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if outcome == "ok":
            return os.open(os.fspath(log), os.O_RDONLY)
        raise outcome
    return opener, calls


def _lock(message="The process cannot access the file"):
    return winfile.SharingViolation(13, message)


def test_ladder_rung1_reports_share_all(log):
    fd, report = winfile.open_escalating(log)
    try:
        assert report.method is OpenMethod.SHARE_ALL
        assert report.attempts == ["share_all: ok"]
        assert report.can_follow is True
    finally:
        os.close(fd)


def test_ladder_rung2_retries_and_reports_retry(monkeypatch, log):
    opener, calls = _fake_opener([_lock(), _lock(), "ok"], log)
    monkeypatch.setattr(winfile, "open_fd", opener)

    fd, report = winfile.open_escalating(log, retry_delay_s=0.0)
    try:
        assert report.method is OpenMethod.RETRY
        assert "retry: ok on attempt 2" in report.attempts
        assert report.attempts[0].startswith("share_all: ")
        assert "transient" in report.note
        assert len(calls) == 3
    finally:
        os.close(fd)


def test_ladder_rung3_uses_backup_semantics(monkeypatch, log):
    """Rung 3 must be reached only after rung 2 is exhausted, and must be named."""
    opener, calls = _fake_opener(
        [_lock(), _lock(), _lock(), _lock(), "ok"], log
    )
    monkeypatch.setattr(winfile, "open_fd", opener)
    monkeypatch.setattr(winfile, "IS_WINDOWS", True)
    monkeypatch.setattr(winfile, "enable_backup_privilege", lambda: True)
    dropped = []
    monkeypatch.setattr(winfile, "drop_backup_privilege", lambda: dropped.append(True))

    fd, report = winfile.open_escalating(log, retry_delay_s=0.0)
    try:
        assert report.method is OpenMethod.BACKUP_SEMANTICS
        assert report.attempts[-1] == "backup_semantics: ok"
        assert "retry: still locked after 3 attempts" in report.attempts[1]
        assert calls == [False, False, False, False, True]
        assert dropped == [True], "SPEC 7.1.1: the privilege is dropped immediately after"
    finally:
        os.close(fd)


def test_ladder_reports_a_privilege_the_user_does_not_hold(monkeypatch, log):
    """PRD R9: not holding SeBackupPrivilege is the ordinary non-admin case."""
    opener, _ = _fake_opener([_lock()], log)
    monkeypatch.setattr(winfile, "open_fd", opener)
    monkeypatch.setattr(winfile, "IS_WINDOWS", True)
    monkeypatch.setattr(winfile, "enable_backup_privilege", lambda: False)

    with pytest.raises(SourceError) as excinfo:
        winfile.open_escalating(log, retry_delay_s=0.0)

    message = str(excinfo.value)
    assert "SeBackupPrivilege not held" in message
    assert "Backup Operators" in message
    assert "Administrator" in message, "R9: say what elevation would buy"


def test_ladder_gives_up_naming_every_rung(monkeypatch, log):
    opener, _ = _fake_opener([_lock()], log)
    monkeypatch.setattr(winfile, "open_fd", opener)
    monkeypatch.setattr(winfile, "IS_WINDOWS", True)
    monkeypatch.setattr(winfile, "enable_backup_privilege", lambda: True)
    monkeypatch.setattr(winfile, "drop_backup_privilege", lambda: None)

    with pytest.raises(SourceError) as excinfo:
        winfile.open_escalating(log, retry_delay_s=0.0)

    message = str(excinfo.value)
    for rung in ("share_all", "retry", "backup_semantics", "vss"):
        assert rung in message, f"the {rung} rung is not named in the failure"
    assert "not attempted" in message, "rung 4 is offered, never automatic (SPEC 7.1.1)"


def test_ladder_vss_rung_is_an_honest_stub(monkeypatch, log):
    """
    FR-4a / R9: never present a partial read as complete.

    M1 does not implement VSS. When it is explicitly allowed the report must say
    it is unimplemented, that it needs administrator rights, and that a snapshot
    could not be followed.
    """
    opener, _ = _fake_opener([_lock()], log)
    monkeypatch.setattr(winfile, "open_fd", opener)
    monkeypatch.setattr(winfile, "IS_WINDOWS", True)
    monkeypatch.setattr(winfile, "enable_backup_privilege", lambda: False)

    with pytest.raises(SourceError) as excinfo:
        winfile.open_escalating(log, allow_vss=True, retry_delay_s=0.0)

    message = str(excinfo.value)
    assert "not implemented" in message
    assert "administrator rights" in message
    assert "can_follow=False" in message


def test_ladder_does_not_escalate_a_missing_file(monkeypatch, log):
    """A path that does not exist is not a lock; the ladder must not grind on it."""
    def opener(path, *, backup_semantics=False):
        raise FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(winfile, "open_fd", opener)

    with pytest.raises(SourceError) as excinfo:
        winfile.open_escalating(log, retry_delay_s=0.0)
    assert "retry" not in str(excinfo.value)


def test_ladder_on_posix_says_backup_semantics_is_windows_only(monkeypatch, log):
    opener, _ = _fake_opener([_lock()], log)
    monkeypatch.setattr(winfile, "open_fd", opener)
    monkeypatch.setattr(winfile, "IS_WINDOWS", False)

    with pytest.raises(SourceError) as excinfo:
        winfile.open_escalating(log, retry_delay_s=0.0)
    assert "Windows only" in str(excinfo.value)


# -- identity, SPEC 7.2 -----------------------------------------------------


def test_identity_is_stable_across_reopen(log):
    first = winfile.identity_of_path(log)
    second = winfile.identity_of_path(log)
    assert first == second
    assert first.same_file(second)
    assert first.size == len(BODY)
    assert first.head_sha


def test_identity_distinguishes_two_files(tmp_path, log):
    other = tmp_path / "other.log"
    other.write_bytes(BODY)
    assert not winfile.identity_of_path(log).same_file(winfile.identity_of_path(other))


def test_identity_detects_replacement_in_place(log):
    before = winfile.identity_of_path(log)
    log.write_bytes(b"a completely different first line\n")
    after = winfile.identity_of_path(log)
    assert after.same_file(before), "truncate-and-rewrite keeps the inode"
    # `replaced_in_place` only answers when both hashes cover the same prefix.
    # Here the rewrite changed the file's length, so it declines, and the
    # definitive test is a re-hash over the original coverage.
    if before.head_comparable(after):
        assert before.replaced_in_place(after)
    assert before.rewritten_given(log.read_bytes()[: before.head_len])


def test_identity_ignores_a_pure_append(tmp_path):
    """
    An append must not look like a rewrite.

    The file is deliberately larger than ``HEAD_SAMPLE``: below that size,
    ``SourceIdentity.replaced_in_place`` cannot tell the two apart, which is a
    limitation of the comparison in ``base.py`` rather than of the identity read
    here. ``FileSource.detect_change`` is the check that handles both sizes --
    see ``test_filesource.py``.
    """
    path = tmp_path / "big.log"
    path.write_bytes(b"x" * (80 * 1024))
    before = winfile.identity_of_path(path)
    with open(path, "ab") as handle:
        handle.write(BODY)
    after = winfile.identity_of_path(path)
    assert after.same_file(before)
    assert not before.replaced_in_place(after)
    assert after.size > before.size


def test_file_information_agrees_with_fstat(log):
    """
    Cross-check the ctypes call against the stdlib, which reads the same fields.

    A disagreement means the structure layout or the index assembly is wrong, and
    nothing else in the engine would notice -- identity is only ever compared
    against itself. On Windows, CPython uses the newer ``FILE_ID_INFO``, whose
    volume serial is 64-bit; ``GetFileInformationByHandle`` returns the low 32
    bits of it and the identical file index.
    """
    fd = winfile.open_fd(log)
    try:
        volume, file_id = winfile.file_information(fd)
        stat = os.fstat(fd)
        assert file_id == stat.st_ino
        if sys.platform == "win32":
            assert volume == stat.st_dev & 0xFFFF_FFFF
        else:
            assert volume == stat.st_dev
    finally:
        os.close(fd)


# -- pread ------------------------------------------------------------------


def test_pread_leaves_the_file_pointer_alone(log):
    fd = winfile.open_fd(log)
    try:
        os.lseek(fd, 10, os.SEEK_SET)
        assert winfile.pread(fd, 0, 4) == BODY[:4]
        assert os.lseek(fd, 0, os.SEEK_CUR) == 10
    finally:
        os.close(fd)


def test_pread_clamps_at_end_of_file_and_rejects_nonsense(log):
    fd = winfile.open_fd(log)
    try:
        assert winfile.pread(fd, len(BODY) - 4, 1000) == BODY[-4:]
        assert winfile.pread(fd, len(BODY) + 100, 10) == b""
        assert winfile.pread(fd, -1, 10) == b""
        assert winfile.pread(fd, 0, 0) == b""
    finally:
        os.close(fd)


# -- UNC detection ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"\\server\share\app.log", True),
        ("//server/share/app.log", True),
        (r"C:\logs\app.log", False),
        ("/var/log/app.log", False),
        (r"\\?\C:\logs\app.log", False),
    ],
)
def test_is_unc(path, expected):
    assert winfile.is_unc(path) is expected
