"""
Shared fixtures for the Qt shell tests.

The one that matters is `isolated_settings`. Without it the suite reads and
writes the real preference store -- the user's own registry on Windows -- so a
test could be handed a font size left behind by the last run, and running the
tests would silently change the application's appearance for the person who ran
them. It is the same failure the sidecar cache had, in a place that is even
easier to miss because `QSettings` never fails: it just quietly returns what
somebody else put there.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import QEvent, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

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
