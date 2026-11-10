"""
Making `.lwprofile` open LogWeave when it is double-clicked -- FR-53.

FR-53 asks for a profile to be "openable by double-click via a file
association". Half of that is the command line, which `ui.app` handles: a
profile named as an argument opens as a profile rather than as a log. The other
half is telling Windows which program that is, and this is it.

## Per user, not per machine

Everything written here goes under `HKEY_CURRENT_USER\\Software\\Classes`, which
is the user's own half of the class store and needs no elevation. The
alternative -- `HKEY_LOCAL_MACHINE` -- would need an installer running as
administrator, and would also decide the question for every account on a shared
build machine, which is not this application's business. A user who wants it
machine-wide has an installer; a user who wants it for themselves has a
checkbox.

## Explicit, and reversible

Nothing here runs on its own. An application that quietly claims a file
extension the first time it starts is the behaviour everybody has learnt to
resent, and a log viewer has no business doing it. The association is written
when someone ticks the box in `Edit > Settings > Session`, and removing the
tick removes the keys again -- including the `ProgID`, so an uninstall leaves
nothing behind but the files the user wrote.

## Not Windows

`register` and `unregister` return a reason rather than raising on any platform
that is not Windows, and `state` reports `UNSUPPORTED`. LogWeave is a Windows
application first (NFR-18) but the engine and the UI both run on Linux, and a
settings page that crashed there would be a worse bug than a missing feature.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..profile import SUFFIX

PROG_ID = "LogWeave.Profile"
"""
The class name the extension points at.

Versionless on purpose. A `ProgID` carrying a version (`LogWeave.Profile.1`) is
the convention for a format that may need two readers installed side by side,
and a profile is a small TOML file with a `format_version` field inside it --
the file says what it is, so the registry does not have to.
"""

FRIENDLY_NAME = "LogWeave session profile"

UNSUPPORTED = "unsupported"
"""`state()` on a platform with no Windows registry."""
ABSENT = "absent"
"""No association: the extension is unclaimed, or claimed by something else."""
OURS = "ours"
"""`.lwprofile` opens through this `ProgID`."""
OTHER = "other"
"""Something else has claimed the extension. Reported, never overwritten quietly."""


def available() -> bool:
    return sys.platform == "win32"


def launcher() -> tuple[str, list[str]]:
    """
    The executable and leading arguments that start this LogWeave.

    Two shapes, because both are real. An installed build is
    `...\\Scripts\\logweave.exe`, which takes the file as its only argument. A
    checkout run as `python -m logweave` has no such launcher, so the command
    has to be the interpreter plus `-m logweave` -- and it must be `python.exe`
    and not `pythonw.exe`'s opposite number: a console window flashing up
    behind the window is ugly but a missing interpreter is fatal, so the
    interpreter actually running is the one recorded.
    """
    exe = Path(sys.executable)
    script = exe.with_name("logweave.exe")
    if script.is_file():
        return str(script), []
    # `pythonw.exe` where it exists, so a double-click does not flash a console.
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.is_file() else exe), ["-m", "logweave"]


def command() -> str:
    """The `shell\\open\\command` string, quoted for `CreateProcess`."""
    exe, args = launcher()
    parts = [f'"{exe}"', *args, '"%1"']
    return " ".join(parts)


def _classes():
    import winreg

    return winreg.HKEY_CURRENT_USER, r"Software\Classes"


def state() -> str:
    """Whether `.lwprofile` currently opens this application."""
    if not available():
        return UNSUPPORTED
    import winreg

    root, base = _classes()
    try:
        with winreg.OpenKey(root, rf"{base}\{SUFFIX}") as key:
            value, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return ABSENT
    if str(value) != PROG_ID:
        return OTHER
    try:
        with winreg.OpenKey(
                root, rf"{base}\{PROG_ID}\shell\open\command") as key:
            winreg.QueryValueEx(key, "")
    except OSError:
        # The extension points at a ProgID with no command behind it, which
        # Windows treats as no association at all -- a half-written register,
        # or a Python that has since been removed.
        return ABSENT
    return OURS


def register() -> str:
    """
    Claim `.lwprofile` for this user. Returns "" on success, or why not.

    The command is rewritten every time rather than only when the key is
    missing: the interpreter moves when a virtual environment is rebuilt, and a
    stale command is an association that opens an error dialog, which is worse
    than none.
    """
    if not available():
        return "file associations are a Windows feature"
    import winreg

    root, base = _classes()
    icon = _icon()
    try:
        with winreg.CreateKey(root, rf"{base}\{SUFFIX}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, PROG_ID)
            # Lets a hand-written profile be previewed and indexed as text.
            winreg.SetValueEx(key, "Content Type", 0, winreg.REG_SZ,
                              "text/plain")
            winreg.SetValueEx(key, "PerceivedType", 0, winreg.REG_SZ, "text")
        with winreg.CreateKey(root, rf"{base}\{PROG_ID}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, FRIENDLY_NAME)
        with winreg.CreateKey(
                root, rf"{base}\{PROG_ID}\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command())
        if icon:
            with winreg.CreateKey(root, rf"{base}\{PROG_ID}\DefaultIcon") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon)
    except OSError as exc:
        return str(exc)
    _notify()
    return ""


def unregister() -> str:
    """Give the extension back. Returns "" on success, or why not."""
    if not available():
        return "file associations are a Windows feature"
    import winreg

    root, base = _classes()
    # Deepest first: `DeleteKey` will not remove a key that still has subkeys.
    for sub in (rf"{base}\{PROG_ID}\shell\open\command",
                rf"{base}\{PROG_ID}\shell\open",
                rf"{base}\{PROG_ID}\shell",
                rf"{base}\{PROG_ID}\DefaultIcon",
                rf"{base}\{PROG_ID}",
                rf"{base}\{SUFFIX}"):
        try:
            winreg.DeleteKey(root, sub)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return str(exc)
    _notify()
    return ""


def _icon() -> str:
    """The icon Explorer shows for a profile, or "" if the build has none."""
    exe, args = launcher()
    if args:
        # `python.exe -m logweave` has no icon of its own worth showing; the
        # interpreter's would tell the reader the file is a Python script.
        return ""
    return f'"{exe}",0'


def _notify() -> None:
    """
    Tell Explorer the class store changed, so the new icon appears at once.

    Without it the association works immediately for a double-click but
    Explorer keeps drawing the old (blank) icon until it is restarted, which
    reads as "the setting did not take".
    """
    try:
        import ctypes

        # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except (AttributeError, OSError):
        # Not fatal: the association is written either way, and this is only
        # about how soon Explorer notices.
        pass


def describe() -> str:
    """One line for the settings page, saying what is true now."""
    got = state()
    if got == UNSUPPORTED:
        return f"{SUFFIX} associations are a Windows feature"
    if got == OURS:
        return f"{SUFFIX} files open in this LogWeave"
    if got == OTHER:
        return f"{SUFFIX} files are currently opened by another program"
    return f"{SUFFIX} files are not associated with anything"
