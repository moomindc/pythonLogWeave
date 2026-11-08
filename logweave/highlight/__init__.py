"""
Highlighting -- FR-30, FR-31, SPEC 10.

Qt-free, and enforced as such by `tests/test_architecture.py`. The rules decide
that a run of text is "error red"; only the view knows what a `QColor` is. That
split is what lets the whole rule engine be tested headlessly, and what will let
the same rules drive an HTML export (FR-47) without the exporter importing a
widget toolkit.
"""

from __future__ import annotations

from .builtins import (
    SEVERITY_SET,
    SHAPES_SET,
    SYSLOG_SET,
    default_group,
    default_ruleset,
)
from .rules import (
    CACHE_ROWS,
    SCAN_LIMIT,
    HighlightRule,
    RowStyle,
    Ruleset,
    RulesetGroup,
    Span,
    Style,
)

__all__ = [
    "CACHE_ROWS",
    "SCAN_LIMIT",
    "SEVERITY_SET",
    "SHAPES_SET",
    "SYSLOG_SET",
    "HighlightRule",
    "RowStyle",
    "Ruleset",
    "RulesetGroup",
    "Span",
    "Style",
    "default_group",
    "default_ruleset",
]
