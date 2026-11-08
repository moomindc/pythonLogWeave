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

from PySide6.QtCore import QSettings  # noqa: E402

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
