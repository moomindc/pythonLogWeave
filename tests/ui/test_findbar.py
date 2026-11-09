"""
The find bar and the window's find wiring -- FR-44.

The search itself is tested headlessly in `tests/unit/test_find.py`; what is
tested here is that the chrome round it says the right things and that a hit
reaches all three places it has to reach: the view's painted spans, both
overview strips, and the cursor.

The scans are driven synchronously. `FindScanner` runs on a `QThread`, so a
test that asked a question and asserted on the answer in the next line would
be asserting on an empty result -- and one that spun the event loop until the
answer arrived would be a timing test dressed as a behaviour test. So these
call `session.find.search` directly and hand the result to the window the way
the scanner would, which is exactly what `_hits_ready` is for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI layer; the engine job installs no Qt")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from logweave.session.find import Query, search  # noqa: E402
from logweave.ui import theme  # noqa: E402
from logweave.ui.findbar import FindBar  # noqa: E402
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
    # `close()`, not just `session.close()`: the window owns two scan threads
    # now, and a running `QThread` destroyed with its parent aborts the process
    # rather than raising. `closeEvent` cancels and joins both.
    w.close()
    w.deleteLater()


def found(window, text: str, *, regex: bool = False, case: bool = False):
    """
    Run a query to completion and push it in as the scanner would.

    The term goes through the bar's own box rather than straight into
    `_find_query`, because the count label is only shown while the result
    matches what is typed -- so a helper that skipped the box would quietly
    stop the label being tested at all.
    """
    bar = window.findbar
    bar.show()
    bar._regex.setChecked(regex)
    bar._case.setChecked(case)
    bar._box.setText(text)
    query = bar.query()
    assert query == Query(text=text, regex=regex, case_sensitive=case)
    window._find_query(query)          # as the debounce would, minus the thread
    hits = search(window.session, query)
    window.finder.result = hits
    window._hits_ready(hits)
    return hits


# ---------------------------------------------------------------- the bar


def test_the_bar_builds_a_query_from_its_boxes(app):
    bar = FindBar()
    bar._box.setText("timeout")
    assert bar.query() == Query(text="timeout", regex=False,
                                case_sensitive=False)
    bar._case.setChecked(True)
    bar._regex.setChecked(True)
    assert bar.query() == Query(text="timeout", regex=True, case_sensitive=True)


def test_typing_is_debounced_and_enter_is_not(app):
    """
    Every keystroke could start a scan of every byte of every file, so typing
    waits for a pause -- but pressing Enter *is* saying you have finished.
    """
    bar = FindBar()
    seen: list[Query] = []
    bar.queryChanged.connect(seen.append)

    bar._box.setText("tim")
    assert seen == [], "a keystroke searched immediately"
    assert bar._timer.isActive()

    bar._enter()
    assert [q.text for q in seen] == ["tim"]
    assert not bar._timer.isActive()


def test_enter_again_steps_instead_of_searching_again(app):
    bar = FindBar()
    steps: list[int] = []
    searches: list[Query] = []
    bar.stepped.connect(steps.append)
    bar.queryChanged.connect(searches.append)

    bar._box.setText("INFO")
    bar._enter()                       # ends the typing: a search
    assert len(searches) == 1 and steps == []

    bar.set_hits(_hits_for(bar.query()))
    bar._enter()                       # same question again: a step
    assert steps == [1] and len(searches) == 1


def test_escape_closes_and_shift_enter_steps_back(app):
    bar = FindBar()
    closed: list[int] = []
    steps: list[int] = []
    bar.closed.connect(lambda: closed.append(1))
    bar.stepped.connect(steps.append)

    bar.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                Qt.KeyboardModifier.NoModifier))
    assert closed == [1]

    bar.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return,
                                Qt.KeyboardModifier.ShiftModifier))
    assert steps == [-1]


def test_the_count_says_which_hit_and_how_many(app):
    bar = FindBar()
    bar._box.setText("INFO")
    hits = _hits_for(bar.query(), rows=[4, 9, 20])
    bar.set_hits(hits, current_row=9)
    assert bar._count.text() == "2 of 3"

    bar.set_hits(hits, current_row=7)
    assert bar._count.text() == "3", "off a hit, the count alone"


def test_a_bad_regex_is_reported_and_the_arrows_stay_dead(app):
    bar = FindBar()
    bar._regex.setChecked(True)
    bar._box.setText("(unclosed")
    assert bar._count.text()
    assert not bar._next.isEnabled() and not bar._prev.isEnabled()


def _hits_for(query, rows=(1, 2)):
    """A `Hits` standing in for a finished scan."""
    import numpy as np

    from logweave.session.find import Hits

    rows = np.asarray(rows, dtype=np.int64)
    return Hits(query=query, complete=True,
                source=np.zeros(rows.size, dtype=np.int64),
                entry=rows.copy(), times=rows.copy(), rows=rows)


# ------------------------------------------------------- the window wiring


def test_find_marks_reach_the_view_and_both_strips(window):
    hits = found(window, "INFO")
    assert hits.count > 0

    assert window.view._find is not None
    assert window.minimap._find is not None
    assert window.timeline._find is not None
    assert window.minimap._find.colour == theme.FIND_MARK


def test_the_first_result_moves_the_cursor_to_a_hit(window):
    hits = found(window, "INFO")
    assert window.view.current_row() in set(hits.rows.tolist())
    assert window._current_hit == window.view.current_row()


def test_f3_steps_forward_and_shift_f3_back(window):
    hits = found(window, "INFO")
    assert hits.count >= 3
    first = window._current_hit

    window._step_hit(1)
    second = window._current_hit
    assert second > first

    window._step_hit(-1)
    assert window._current_hit == first


def test_stepping_wraps_rather_than_stopping(window):
    """
    `Hits.next_after` wraps, which is right for a find: a search is a set of
    places in the file and going round is how you check you have seen them all.
    Jumping by *severity* deliberately does not -- see `nav.next_row`.
    """
    hits = found(window, "INFO")
    last = int(hits.rows[-1])
    window._goto_hit(last)
    window._step_hit(1)
    assert window._current_hit == int(hits.rows[0])


def test_closing_the_bar_takes_the_marks_away_but_keeps_the_result(window):
    found(window, "INFO")
    window.close_find()

    assert window.view._find is None
    assert window.minimap._find is None
    assert window.timeline._find is None
    assert not window.findbar.isVisibleTo(window)
    assert window.finder.result.count > 0, "the scan was thrown away"


def test_an_empty_query_clears_everything(window):
    found(window, "INFO")
    window._find_query(Query(text=""))
    window._hits_ready(window.finder.result)
    assert window.view._find is None or window.minimap._find is None


def test_the_view_marks_the_hit_text_itself(window):
    """
    FR-44 asks for the hits to be visible in the view, not only on the strips.
    The spans are re-derived from the displayed text (see `set_find`), so this
    is where that is checked: a row with the term in it gains a span the rules
    did not put there.
    """
    found(window, "INFO")
    row = window._current_hit
    rows = window.session.rows(row, 1)
    text = window.view._row_text(rows[0], trim=window.view.trims_timestamp())
    style = window.view.highlights.style_of(text)
    marked = window.view._with_find(style, text, row)
    assert any(sp.rule == "find" for sp in marked.spans)
    assert len(marked.spans) > len(style.spans)


def test_the_bar_reports_the_count_it_was_given(window):
    hits = found(window, "INFO")
    label = window.findbar._count.text()
    assert f"{hits.count:,}" in label
    assert label.startswith(f"{hits.ordinal(window._current_hit)} of ")


def test_the_status_bar_reports_the_search_too(window):
    found(window, "INFO")
    assert "match" in window._status_msg.text()
