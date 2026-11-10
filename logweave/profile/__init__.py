"""
Profiles: a named set of logs and how to read them -- FR-49 to FR-53, SPEC 12.

Qt-free, like every layer below the UI (SPEC 1), and with no knowledge of a
`Session` either. The package is three separable things:

* `model` -- what a profile is, as plain data.
* `toml_io` -- the `.lwprofile` file.
* `resolve` -- what its globs mean on the disk today.

Building one from an open window, and opening one into a window, is the UI's
job (`ui.profileio`): that is where the view's columns and the follow engine
live, and a profile that could reach into them would put half the window's
state into a file format.
"""

from .model import (
    FORMAT_VERSION,
    SUFFIX,
    Bookmark,
    HighlightPrefs,
    Profile,
    ProfileError,
    SavedFilter,
    SourceSpec,
    ViewPrefs,
)
from .resolve import Resolution, freeze, resolve
from .toml_io import comments_at_risk, dump, load, loads, save

__all__ = [
    "FORMAT_VERSION",
    "SUFFIX",
    "Bookmark",
    "HighlightPrefs",
    "Profile",
    "ProfileError",
    "Resolution",
    "SavedFilter",
    "SourceSpec",
    "ViewPrefs",
    "comments_at_risk",
    "dump",
    "freeze",
    "load",
    "loads",
    "resolve",
    "save",
]
