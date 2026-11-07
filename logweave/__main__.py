"""
`python -m logweave`, and the target of the `logweave` console script.

`main` is imported rather than called at module level so that
`logweave.__main__:main` in pyproject.toml resolves to something callable --
a module that launched the GUI on import would start an app during
entry-point resolution.
"""

from .ui.app import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
