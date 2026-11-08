"""
Enforces SPEC 1's hard rule: layers 1-4 must not import Qt.

That rule is what keeps the engine testable headlessly on Linux CI (NFR-18) and
keeps Qt out of the indexing worker processes (SPEC 5.3). It is easy to break by
accident and invisible until someone tries to run the engine without a display,
so it is a test rather than a convention.

These tests pass trivially until the package exists, and start biting from M1.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "logweave"
ENGINE_LAYERS = ["source", "index", "merge", "session", "highlight"]
"""
`highlight` is here although SPEC 1 numbers it with the UI.

The rules decide that a run of text is "error red"; only the view knows what a
`QColor` is. Keeping the engine side of that line Qt-free is what lets the rule
engine be tested headlessly, and what will let the same rules drive an HTML
export (FR-47) without the exporter importing a widget toolkit.
"""
FORBIDDEN_ROOTS = {"PySide6", "PyQt6", "PyQt5", "shiboken6"}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("layer", ENGINE_LAYERS)
def test_engine_layer_does_not_import_qt(layer):
    directory = PACKAGE / layer
    if not directory.is_dir():
        pytest.skip(f"logweave/{layer} does not exist yet")
    offenders = []
    for path in sorted(directory.rglob("*.py")):
        bad = _imported_roots(path) & FORBIDDEN_ROOTS
        if bad:
            offenders.append(f"{path.relative_to(ROOT)} imports {sorted(bad)}")
    assert not offenders, "SPEC 1: engine layers must stay Qt-free\n" + "\n".join(offenders)


def test_ui_layer_is_the_only_qt_consumer():
    if not PACKAGE.is_dir():
        pytest.skip("logweave package does not exist yet")
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel.parts and rel.parts[0] == "ui":
            continue
        if path.name == "__main__.py":
            continue                      # the launcher may import the UI lazily
        bad = _imported_roots(path) & FORBIDDEN_ROOTS
        if bad:
            offenders.append(f"{path.relative_to(ROOT)} imports {sorted(bad)}")
    assert not offenders, "only logweave/ui may import Qt\n" + "\n".join(offenders)


# The paint benchmark measures Qt, so it cannot avoid importing it. It is named
# explicitly rather than matched by a pattern, so adding a second Qt-dependent
# tool is a deliberate edit to this list and not an accident.
QT_TOOLS = {"bench_ui.py"}


def test_tools_do_not_import_qt():
    """
    The corpus generator and the engine bench must run without Qt installed.

    `tools/bench.py` is the performance gate and runs in the engine CI job, which
    installs numpy and pytest and nothing else. A stray Qt import there would
    make the gate unrunnable in exactly the environment it exists to protect.
    """
    offenders = []
    for path in sorted((ROOT / "tools").glob("*.py")):
        if path.name in QT_TOOLS:
            continue
        bad = _imported_roots(path) & FORBIDDEN_ROOTS
        if bad:
            offenders.append(f"{path.name} imports {sorted(bad)}")
    assert not offenders, "\n".join(offenders)


def test_the_qt_dependent_tools_are_the_ones_we_think():
    """
    Guards the exemption above: a tool that stops needing Qt should leave the
    list, and the list should never quietly grow to cover `bench.py`.
    """
    assert "bench.py" not in QT_TOOLS
    for name in QT_TOOLS:
        path = ROOT / "tools" / name
        assert path.exists(), f"{name} is exempted but does not exist"
        assert _imported_roots(path) & FORBIDDEN_ROOTS, (
            f"{name} no longer imports Qt; remove it from QT_TOOLS"
        )


def test_docs_are_present():
    """The PRD and SPEC are deliverables; requirement IDs are referenced in code."""
    assert (ROOT / "docs" / "PRD.md").exists()
    assert (ROOT / "docs" / "SPEC.md").exists()
    assert (ROOT / "LICENSE").exists()
    assert (ROOT / "NOTICE").exists()


def test_notice_credits_logmerger():
    """D15 / NFR-25: attribution is a licence condition, not a courtesy."""
    text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "logmerger" in text
    assert "Paul McGuire" in text
    assert "MIT" in text
