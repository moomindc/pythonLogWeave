"""
The export dialog and its runner -- FR-47, FR-46b.

The writing itself is tested headlessly in `tests/unit/test_export.py`. What is
tested here is the chrome: that the format follows the filename until the user
says otherwise, that Save is refused while the form cannot produce a file, and
that `ExportRun` really does write the whole view a batch at a time without a
thread.

The runner is stepped by hand rather than by spinning the event loop. One
`_step()` is one batch, which is exactly what the zero-delay timer delivers, so
stepping is the same sequence without the timing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from logweave.session.export import ExportSpec  # noqa: E402
from logweave.session.rowformat import Preset, RowFormatter  # noqa: E402
from logweave.ui.exportdialog import ExportDialog, ExportRun  # noqa: E402
from logweave.ui.mainwindow import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    yield existing or QApplication([])


@pytest.fixture
def window(app, corpus: Path):
    w = MainWindow(background=False)
    w.resize(1200, 800)
    w.open_paths(sorted((corpus / "basic").glob("*.log")))
    assert w.session.total_rows > 0
    yield w
    w.close()
    w.deleteLater()


def drain(run: ExportRun) -> object:
    """Step the runner to the end and return the last `Progress`."""
    last = []
    run.finished.connect(last.append)
    for _ in range(10_000):
        run._step()
        if last:
            break
    assert last, "the export never finished"
    return last[0]


# ---------------------------------------------------------------- the form


def test_the_format_follows_the_extension(app, tmp_path):
    dialog = ExportDialog(rows=10)
    dialog._path.setText(str(tmp_path / "rows.csv"))
    assert dialog.preset() is Preset.CSV

    dialog._path.setText(str(tmp_path / "rows.md"))
    assert dialog.preset() is Preset.MARKDOWN

    dialog._path.setText(str(tmp_path / "rows.log"))
    assert dialog.preset() is Preset.ANNOTATED


def test_choosing_a_format_stops_the_extension_overriding_it(app, tmp_path):
    """
    Editing the filename after choosing CSV must not silently undo the choice:
    the extension is a default, not a rule.
    """
    dialog = ExportDialog(rows=10)
    index = dialog._preset.findData(Preset.CSV.value)
    dialog._preset.setCurrentIndex(index)
    dialog._path.setText(str(tmp_path / "rows.md"))
    assert dialog.preset() is Preset.CSV


def test_save_is_refused_without_a_path(app):
    dialog = ExportDialog(rows=10)
    save = dialog._buttons.button(QDialogButtonBox.StandardButton.Save)
    assert not save.isEnabled()
    assert dialog._complaint.text()

    dialog._path.setText("out.log")
    assert save.isEnabled()
    assert not dialog._complaint.text()


def test_a_broken_template_is_reported_rather_than_exported(app, tmp_path):
    dialog = ExportDialog(rows=10)
    dialog._path.setText(str(tmp_path / "out.log"))
    index = dialog._preset.findData(Preset.TEMPLATE.value)
    dialog._preset.setCurrentIndex(index)
    dialog._template.setText("{nonsense}")

    save = dialog._buttons.button(QDialogButtonBox.StandardButton.Save)
    assert not save.isEnabled()
    assert "nonsense" in dialog._complaint.text()


def test_a_selection_is_offered_and_preferred(app, tmp_path):
    dialog = ExportDialog(rows=1000, selection=(10, 19))
    dialog._path.setText(str(tmp_path / "out.log"))
    assert dialog._scope.count() == 2
    assert dialog._scope.currentData() == "selection", \
        "a live selection should be the default scope"

    spec = dialog.spec()
    assert (spec.first_row, spec.rows) == (10, 10)


def test_the_whole_view_is_the_other_scope(app, tmp_path):
    dialog = ExportDialog(rows=1000, selection=(10, 19))
    dialog._path.setText(str(tmp_path / "out.log"))
    dialog._scope.setCurrentIndex(0)
    spec = dialog.spec()
    assert spec.first_row == 0 and spec.rows is None


def test_crlf_is_offered_but_not_the_default(app, tmp_path):
    dialog = ExportDialog(rows=10)
    dialog._path.setText(str(tmp_path / "out.log"))
    assert dialog.spec().newline == "\n"
    dialog._crlf.setChecked(True)
    assert dialog.spec().newline == "\r\n"


# -------------------------------------------------------------- the runner


def test_the_runner_writes_every_row_in_batches(window, tmp_path):
    out = tmp_path / "all.log"
    spec = ExportSpec(path=out, batch_rows=50)
    run = ExportRun(window.session, RowFormatter(window.session), spec)
    last = drain(run)

    assert last.done and not last.cancelled and not last.error
    assert last.written == window.session.total_rows
    assert out.read_text(encoding="utf-8").count("\n") >= last.written


def test_progress_is_reported_as_it_goes(window, tmp_path):
    spec = ExportSpec(path=tmp_path / "p.log", batch_rows=20)
    run = ExportRun(window.session, RowFormatter(window.session), spec)
    seen: list[float] = []
    run.progress.connect(lambda step: seen.append(step.fraction))
    drain(run)

    assert len(seen) > 2, "a batched export reported once"
    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0)


def test_cancelling_keeps_what_was_written(window, tmp_path):
    """
    NFR-21: a cancelled export leaves a usable session and the part-written
    file where it is. Deleting what the user asked for is not the runner's
    call.
    """
    out = tmp_path / "part.log"
    spec = ExportSpec(path=out, batch_rows=5)
    run = ExportRun(window.session, RowFormatter(window.session), spec)
    done: list[object] = []
    run.finished.connect(done.append)

    run._step()          # the header
    run._step()          # one batch
    run.cancel()

    assert done and done[0].cancelled
    assert out.exists() and out.read_text(encoding="utf-8")


def test_an_unwritable_path_is_reported_not_raised(window, tmp_path):
    spec = ExportSpec(path=tmp_path / "no-such-dir" / "x.log")
    run = ExportRun(window.session, RowFormatter(window.session), spec)
    last = drain(run)
    assert last.error and last.done


def test_the_window_refuses_a_second_export_while_one_runs(window, tmp_path):
    spec = ExportSpec(path=tmp_path / "busy.log", batch_rows=10)
    window._export = ExportRun(window.session,
                               RowFormatter(window.session), spec)
    window.export_view()
    assert "already running" in window._status_msg.text()
    window._export = None


def test_the_window_reports_what_it_wrote(window, tmp_path):
    spec = ExportSpec(path=tmp_path / "done.log", batch_rows=100)
    run = ExportRun(window.session, RowFormatter(window.session), spec)
    window._export = run
    run.progress.connect(window._export_progress)
    run.finished.connect(window._export_finished)
    drain(run)

    assert "Exported" in window._status_msg.text()
    assert not window._export_bar.isVisibleTo(window)
    assert window._export is None


def test_a_filtered_view_exports_the_filtered_rows(window, tmp_path):
    from logweave.index.severity import Severity
    from logweave.session.filters import Filter
    from logweave.session.nav import AT_LEAST

    window._filter_changed(Filter(severities=AT_LEAST[int(Severity.WARNING)]))
    kept = window.session.total_rows
    assert 0 < kept < window.session.unfiltered_rows

    out = tmp_path / "filtered.log"
    run = ExportRun(window.session, RowFormatter(window.session),
                    ExportSpec(path=out, batch_rows=50))
    last = drain(run)
    assert last.written == kept
