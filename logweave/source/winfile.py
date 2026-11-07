"""
Opening files without breaking the writer -- SPEC 7.1, 7.1.1 and 7.2.

**The trap this module exists for.** Python's ``open()`` on Windows goes through
the CRT, which requests ``FILE_SHARE_READ | FILE_SHARE_WRITE`` but **not**
``FILE_SHARE_DELETE``. A file held open that way cannot be renamed or deleted by
its own writer, so merely *watching* a log would make the application's log
rotation fail. That is exactly what NFR-20 forbids, and it is invisible until a
user's production rotation starts erroring. Measured on Windows 11:

    builtin open()            rename=WinError 32   delete=WinError 32
    CreateFileW, all 3 shares rename=OK            delete=OK
    the same handle + mmap    rename=OK            delete=OK

The last line matters: a memory mapping over a share-delete handle does *not*
re-acquire the lock, so ``file.py`` may mmap freely (NFR-20 and the no-syscall
read path of SPEC 3.1 are not in conflict).

The second job is the SPEC 7.1.1 escalation ladder for logs whose writer opened
them with no sharing at all. Each rung's outcome is recorded in an
``OpenReport`` because a user must be told whether they are looking at a live
file or a frozen snapshot, and because rungs 3 and 4 may simply be unavailable
to a non-admin user (PRD R9, FR-4a). A partial read is never presented as
complete.

The third job is file identity for rotation and truncation detection (SPEC 7.2):
volume serial plus file index from ``GetFileInformationByHandle`` on Windows,
``(st_dev, st_ino)`` on POSIX. Identity is read from the *handle*, not the path,
so it keeps working after the file has been rotated away or deleted.

No Qt, stdlib only (SPEC 1).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from .base import HEAD_SAMPLE, OpenMethod, OpenReport, SourceError, SourceIdentity

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# CreateFileW (winnt.h). Spelled out rather than imported so this module has no
# dependency on a Windows-only package.
GENERIC_READ = 0x8000_0000
FILE_SHARE_READ = 0x0000_0001
FILE_SHARE_WRITE = 0x0000_0002
FILE_SHARE_DELETE = 0x0000_0004
SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x0000_0080
FILE_FLAG_BACKUP_SEMANTICS = 0x0200_0000

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_NOT_ALL_ASSIGNED = 1300

#: Win32 errors for which escalating up the ladder can plausibly help. 32 and 33
#: are the sharing/lock violations SPEC 7.1.1 names; 5 is included because
#: ``SeBackupPrivilege`` (rung 3) is precisely the fix for a DACL that denies
#: read, and the user gets a truthful report either way.
ESCALATABLE_ERRORS = frozenset({ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION, ERROR_ACCESS_DENIED})

RETRY_ATTEMPTS = 3
"""SPEC 7.1.1 rung 2: 3 attempts, which with RETRY_DELAY_S spans ~300ms."""

RETRY_DELAY_S = 0.1

#: Above this length a Windows path needs the ``\\?\`` prefix to dodge MAX_PATH.
#: Applied well below 260 so a UNC rewrite never pushes a borderline path over.
LONG_PATH_THRESHOLD = 230


class SharingViolation(OSError):
    """
    A rung of the ladder failed in a way a higher rung might fix.

    A distinct type rather than an errno check so the ladder's control flow can
    be exercised on Linux CI, where no ``winerror`` exists.
    """


def _long_path(path: str) -> str:
    r"""
    Prefix a long Windows path so the Win32 layer skips MAX_PATH parsing.

    UNC paths take the ``\\?\UNC\server\share`` spelling; the prefix only works
    on fully-qualified paths, so a relative one is resolved first.
    """
    if len(path) < LONG_PATH_THRESHOLD or path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(path)
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def is_unc(path: Path | str) -> bool:
    r"""
    True for ``\\server\share\...``, which SPEC 3.2 reads buffered, not mmapped.

    Only the UNC spelling is detected, not a drive letter mapped to a share: a
    mapped drive behaves like a local path for mmap purposes far more often than
    not, and guessing wrong costs correctness rather than speed.
    """
    text = str(path)
    return text.startswith(("\\\\", "//")) and not text.startswith(("\\\\?\\", "//?/"))


if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]

    class _TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD),
                    ("Privileges", _LUID_AND_ATTRIBUTES * 1)]

    SE_PRIVILEGE_ENABLED = 0x0000_0002
    TOKEN_QUERY = 0x0008
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    SECURITY_IMPERSONATION = 2

    _advapi32.ImpersonateSelf.argtypes = [ctypes.c_int]
    _advapi32.ImpersonateSelf.restype = wintypes.BOOL
    _advapi32.RevertToSelf.argtypes = []
    _advapi32.RevertToSelf.restype = wintypes.BOOL
    _advapi32.OpenThreadToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenThreadToken.restype = wintypes.BOOL
    _advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_LUID),
    ]
    _advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    _advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(_TOKEN_PRIVILEGES),
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
    ]
    _advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
    _kernel32.GetCurrentThread.argtypes = []
    _kernel32.GetCurrentThread.restype = wintypes.HANDLE


def _raise_win_error(path: Path | str) -> None:
    """Translate the thread's last Win32 error, as SharingViolation if escalatable."""
    err = ctypes.get_last_error()
    exc = ctypes.WinError(err)
    message = f"{exc.strerror} ({err}): {path}"
    if err in ESCALATABLE_ERRORS:
        raise SharingViolation(exc.errno, message, str(path), err) from None
    raise OSError(exc.errno, message, str(path), err) from None


def enable_backup_privilege() -> bool:
    """
    Enable ``SeBackupPrivilege`` on the calling thread. SPEC 7.1.1 rung 3.

    Thread-scoped via ``ImpersonateSelf`` so ``drop_backup_privilege`` can revoke
    it outright rather than merely disabling it on the process token -- SPEC
    7.1.1 requires the privilege to be held only for the open itself. Returns
    False when the account simply does not hold the privilege, which for a
    non-admin user is the ordinary case (PRD R9), not an error.
    """
    if not IS_WINDOWS:
        return False
    if not _advapi32.ImpersonateSelf(SECURITY_IMPERSONATION):
        return False
    token = wintypes.HANDLE()
    try:
        if not _advapi32.OpenThreadToken(
            _kernel32.GetCurrentThread(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, False,
            ctypes.byref(token),
        ):
            _advapi32.RevertToSelf()
            return False
        luid = _LUID()
        if not _advapi32.LookupPrivilegeValueW(None, "SeBackupPrivilege", ctypes.byref(luid)):
            _advapi32.RevertToSelf()
            return False
        privs = _TOKEN_PRIVILEGES(1, (_LUID_AND_ATTRIBUTES * 1)(
            _LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED)))
        ctypes.set_last_error(0)
        ok = _advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privs), 0, None, None)
        # AdjustTokenPrivileges reports success even when it assigned nothing.
        if not ok or ctypes.get_last_error() == ERROR_NOT_ALL_ASSIGNED:
            _advapi32.RevertToSelf()
            return False
        return True
    finally:
        if token.value:
            _kernel32.CloseHandle(token)


def drop_backup_privilege() -> None:
    """Discard the impersonation token from ``enable_backup_privilege``."""
    if IS_WINDOWS:
        _advapi32.RevertToSelf()


def open_fd(path: Path | str, *, backup_semantics: bool = False) -> int:
    """
    Open ``path`` read-only without locking it, returning an OS file descriptor.

    Windows: ``CreateFileW`` with ``GENERIC_READ`` and all three share flags,
    wrapped by ``msvcrt.open_osfhandle`` so the result is an ordinary fd that
    ``os.fstat``, ``os.read`` and ``mmap`` accept. POSIX: ``os.open``, which
    already has these semantics.

    Raises ``SharingViolation`` when a higher rung of the SPEC 7.1.1 ladder might
    help, ``OSError`` otherwise. The ladder in ``open_escalating`` reaches every
    rung through this one function, which is also what makes the ladder testable.
    """
    if not IS_WINDOWS:
        return os.open(os.fspath(path), os.O_RDONLY)

    flags = FILE_ATTRIBUTE_NORMAL
    if backup_semantics:
        flags |= FILE_FLAG_BACKUP_SEMANTICS
    ctypes.set_last_error(0)
    handle = _kernel32.CreateFileW(
        _long_path(os.fspath(path)), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, flags, None,
    )
    if handle is None or handle == INVALID_HANDLE_VALUE:
        _raise_win_error(path)
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        _kernel32.CloseHandle(handle)  # open_osfhandle did not take ownership
        raise


def open_shared(path: Path | str):
    """
    Rung 1 only, as a buffered binary file object. Convenience for small reads.

    The returned object owns the descriptor, so closing it closes the handle.
    """
    fd = open_fd(path)
    try:
        return open(fd, "rb", closefd=True)
    except OSError:
        os.close(fd)
        raise


def open_escalating(
    path: Path | str,
    *,
    allow_vss: bool = False,
    retry_attempts: int = RETRY_ATTEMPTS,
    retry_delay_s: float = RETRY_DELAY_S,
) -> tuple[int, OpenReport]:
    """
    Open ``path`` by the SPEC 7.1.1 ladder, stopping at the first rung that works.

    Returns ``(fd, report)``. The report names the rung that succeeded and every
    rung that was tried, because the source panel has to show whether the user is
    looking at a live file or a snapshot (FR-4a).

    ``allow_vss`` gates rung 4, which SPEC 7.1.1 says is offered and never
    automatic: it creates a system-wide shadow copy. It is a reporting stub in M1
    (see ``_attempt_vss``).

    Raises ``SourceError`` on the final rung, naming what failed and what
    elevation would buy. Errors that no rung can fix -- a missing file, a
    directory, a dropped share -- are raised immediately rather than dragged
    through the ladder.
    """
    report = OpenReport()
    origin = str(path)

    try:
        fd = open_fd(path)
    except SharingViolation as exc:
        report.attempts.append(f"share_all: {exc.strerror or exc}")
    except OSError as exc:
        raise SourceError(f"Cannot open {path}: {exc.strerror or exc}", origin=origin) from exc
    else:
        report.method = OpenMethod.SHARE_ALL
        report.attempts.append("share_all: ok")
        return fd, report

    # Rung 2: antivirus and search indexers hold brief exclusive opens, and that
    # is the single most common cause of a violation on a file nobody has locked.
    for attempt in range(1, retry_attempts + 1):
        time.sleep(retry_delay_s)
        try:
            fd = open_fd(path)
        except SharingViolation:
            pass
        except OSError as exc:
            raise SourceError(f"Cannot open {path}: {exc.strerror or exc}", origin=origin) from exc
        else:
            report.method = OpenMethod.RETRY
            report.attempts.append(f"retry: ok on attempt {attempt}")
            report.note = "Opened after a transient sharing violation cleared."
            return fd, report
    report.attempts.append(
        f"retry: still locked after {retry_attempts} attempts "
        f"over ~{int(retry_attempts * retry_delay_s * 1000)}ms"
    )

    # Rung 3: what a backup agent does. Needs Backup Operators or Administrators.
    if IS_WINDOWS:
        held = enable_backup_privilege()
        if not held:
            report.attempts.append(
                "backup_semantics: SeBackupPrivilege not held "
                "(needs Backup Operators or Administrators)"
            )
        else:
            try:
                fd = open_fd(path, backup_semantics=True)
            except OSError as exc:
                report.attempts.append(f"backup_semantics: {exc.strerror or exc}")
            else:
                report.method = OpenMethod.BACKUP_SEMANTICS
                report.attempts.append("backup_semantics: ok")
                report.note = "Opened with SeBackupPrivilege; the writer holds an exclusive lock."
                return fd, report
            finally:
                drop_backup_privilege()
    else:
        report.attempts.append("backup_semantics: Windows only")

    # Rung 4: stub (see _attempt_vss).
    report.attempts.append(_attempt_vss(path, allow_vss=allow_vss, report=report))

    # Rung 5: give up, honestly.
    raise SourceError(
        f"Cannot open {path}: it is locked exclusively by its writer. "
        f"The share_all, retry and backup_semantics rungs all failed; "
        f"a VSS snapshot is not implemented in this milestone. "
        f"Running as Administrator (or as a member of Backup Operators) would let the "
        f"backup_semantics rung bypass the lock, and would allow a VSS snapshot, "
        f"which reads a frozen copy and so cannot be followed. "
        f"Attempts: " + "; ".join(report.attempts),
        origin=origin,
    )


def _attempt_vss(path: Path | str, *, allow_vss: bool, report: OpenReport) -> str:
    """
    SPEC 7.1.1 rung 4 -- **deliberately a stub in M1**.

    A shadow copy is a system-wide, administrator-only operation, and the
    resulting snapshot does not grow, so a source read this way can never be
    followed. Recording that here keeps the eventual implementation honest: the
    rung must set ``can_follow=False`` and the UI must say "snapshot", not show a
    frozen tail as if it were live.
    """
    if not allow_vss:
        return "vss_snapshot: not attempted (SPEC 7.1.1 requires explicit confirmation)"
    report.can_follow = False
    report.note = (
        "A VSS snapshot would be needed to read this file. It requires administrator "
        "rights and yields a frozen point-in-time copy, so following would be disabled."
    )
    return "vss_snapshot: not implemented in M1 (needs administrator rights; can_follow=False)"


def file_information(fd: int) -> tuple[int, int]:
    """
    ``(volume_serial, file_index)`` for an open descriptor -- SPEC 7.2.

    Windows: ``GetFileInformationByHandle``. The 64-bit file index it returns is
    unique per volume on NTFS; on ReFS the full identifier is 128 bits and this
    is its low half, which is a theoretical collision risk only.
    POSIX: ``(st_dev, st_ino)``.

    Read from the handle rather than the path, so it still answers after the file
    has been rotated away or deleted.
    """
    if not IS_WINDOWS:
        st = os.fstat(fd)
        return st.st_dev, st.st_ino
    info = _BY_HANDLE_FILE_INFORMATION()
    ctypes.set_last_error(0)
    if not _kernel32.GetFileInformationByHandle(msvcrt.get_osfhandle(fd), ctypes.byref(info)):
        raise OSError(ctypes.WinError(ctypes.get_last_error()))
    return info.dwVolumeSerialNumber, (info.nFileIndexHigh << 32) | info.nFileIndexLow


def pread(fd: int, start: int, length: int) -> bytes:
    """
    Positional read that leaves the file pointer where it found it.

    ``os.pread`` is POSIX-only, and the buffered source path (SPEC 3.2, UNC) and
    the head hash both need position-independent reads. Short reads are retried
    to end-of-file, so the result is as many bytes as exist in the range.

    Not safe against concurrent use of the same ``fd`` from another thread; the
    sources that use it own their descriptor.
    """
    if start < 0 or length <= 0:
        return b""
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            block = os.read(fd, remaining)
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    finally:
        os.lseek(fd, saved, os.SEEK_SET)


def identity_from_fd(fd: int, *, head: bytes | None = None) -> SourceIdentity:
    """
    Full ``SourceIdentity`` for an open descriptor -- SPEC 7.2.

    ``head`` lets a caller that already has the first bytes mapped supply them
    instead of paying for a 64KB read; otherwise they are read positionally.
    """
    volume, file_id = file_information(fd)
    size = os.fstat(fd).st_size
    if head is None:
        head = pread(fd, 0, HEAD_SAMPLE)
    covered = min(len(head), HEAD_SAMPLE)
    return SourceIdentity(
        volume=volume,
        file_id=file_id,
        size=size,
        head_sha=SourceIdentity.hash_head(head, covered),
        # Recording the coverage is what lets the hash be compared at all: two
        # hashes over different prefix lengths carry no information, so without
        # this every append to a log under 64KB reads as a truncate-and-rewrite.
        head_len=covered,
    )


def identity_of_path(path: Path | str) -> SourceIdentity:
    """
    Identity of whatever ``path`` resolves to *now* -- the rotation check.

    SPEC 7.2: a path resolving to a different identity than the open handle is
    the definition of a rotation. Opens and closes a handle, so it costs more
    than ``identity_from_fd``; the follow engine calls it on the poll interval,
    not on the paint path.
    """
    fd = open_fd(path)
    try:
        return identity_from_fd(fd)
    finally:
        os.close(fd)
