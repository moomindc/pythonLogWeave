"""
Shared fixtures for the Qt shell tests.

The one that matters is `isolated_settings`. Without it the suite reads and
writes the real preference store -- the user's own registry on Windows -- so a
test could be handed a font size left behind by the last run, and running the
tests would silently change the application's appearance for the person who ran
them. It is the same failure the sidecar cache had, in a place that is even
easier to miss because `QSettings` never fails: it just quietly returns what
somebody else put there.

The other autouse fixture, `no_native_dialogs`, is there to stop a test opening
a modal file or colour dialog: those wait for a human click, so without it one
code path with a `QFileDialog` on it hangs the whole run behind a window that
nobody expected to see.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QEvent, QSettings  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QColorDialog,
    QFileDialog,
)

from logweave.ui import settings as ui_settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point `ui.settings` at a fresh ini file for every test."""
    path = tmp_path / "logweave-test.ini"
    monkeypatch.setattr(
        ui_settings, "store",
        lambda: QSettings(str(path), QSettings.Format.IniFormat),
    )
    return path


@pytest.fixture(autouse=True)
def drain_deferred_deletes():
    """
    Destroy the windows a test asked to delete, before the next one runs.

    `deleteLater` only *posts* the destruction: it happens on the next turn of
    an event loop, and these tests rarely run one. So a window a test dropped
    is destroyed somewhere in the middle of a later test -- and that is not
    harmless, because on Windows destroying the window that owns the clipboard
    empties it (`WM_DESTROYCLIPBOARD`). The symptom was two clipboard tests
    that passed alone and failed in the suite, with the *second* copy in the
    process reading back as empty: the first window was being torn down
    between the `setText` and the assertion.

    Draining here rather than in each fixture's teardown, because the bug is
    not any one fixture's -- it is the whole suite's assumption that a window
    is gone once it has been asked to go.
    """
    yield
    app = QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@dataclass
class ChosenPaths:
    """
    What the user "picks" when the window opens a file or colour dialog.

    Cancelled by default -- an empty path, an invalid colour -- because that is
    the answer a test wants when it is asserting that the window *asked*. A
    test that needs the other branch sets the field it cares about before
    calling, and reads `asked` afterwards to prove the dialog was the thing
    that supplied it.
    """

    save: str = ""
    open_one: str = ""
    open_many: list[str] = field(default_factory=list)
    colour: str = ""
    asked: list[str] = field(default_factory=list)


@pytest.fixture(autouse=True)
def no_native_dialogs(monkeypatch) -> ChosenPaths:
    """
    Answer every modal file or colour dialog instead of showing one.

    `QFileDialog.getSaveFileName` and its siblings run their own event loop and
    do not return until a human clicks, so one test that reaches a code path
    with a dialog on it stops the whole suite dead on a developer's machine --
    a window appears, pytest looks hung, and whoever is watching has to guess
    what it wants. The offscreen platform hides the window but not the wait.

    Patched on the Qt classes rather than on each module's imported name, so a
    dialog opened from anywhere in `logweave.ui` is covered -- including one
    added after this fixture was written, which is the case that matters:
    nobody remembers to add a stub for a dialog they did not know was there.

    Autouse, for the same reason `isolated_settings` is. A test that wants a
    real dialog is not a test, and there is nothing to opt out of.
    """
    chosen = ChosenPaths()

    def save(*_args, **_kwargs):
        chosen.asked.append("save")
        return chosen.save, ""

    def open_one(*_args, **_kwargs):
        chosen.asked.append("open")
        return chosen.open_one, ""

    def open_many(*_args, **_kwargs):
        chosen.asked.append("open_many")
        return list(chosen.open_many), ""

    def colour(*_args, **_kwargs):
        chosen.asked.append("colour")
        # An invalid colour is how `QColorDialog` reports a cancel, and what
        # `ruledialog` tests with `isValid()`.
        return QColor(chosen.colour) if chosen.colour else QColor()

    monkeypatch.setattr(QFileDialog, "getSaveFileName", save)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", open_one)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", open_many)
    monkeypatch.setattr(QColorDialog, "getColor", colour)
    return chosen
