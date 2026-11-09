"""
The main window -- FR-1, FR-19, FR-28, FR-29.

Thin by design. The window owns a `Session`, a `MergedLogView` and a source
list; it holds no index, no offset and no merge state. Dropping files is one
call into the session and one refresh of the view, which is the whole point of
the layer-4 boundary (SPEC 1).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ..highlight import default_group
from ..highlight import store as rulestore
from ..highlight.builtins import USER_SET
from ..highlight.rules import Ruleset
from ..index import sidecar
from ..index.civil import format_wall_us
from ..index.severity import Severity
from ..session import Session, nav
from ..session import filters as filtering
from ..session.follow import FollowEngine
from ..session.nav import AT_LEAST
from ..session.rowformat import PRESET_LABELS, Preset
from . import settings, theme
from .exportdialog import ExportDialog, ExportRun
from .filterbar import FilterBar
from .findbar import FindBar
from .findscan import FilterScanner, FindScanner
from .followui import FollowNotifier, NewLinesButton
from .gotodialog import GoToTimeDialog
from .indexing import IndexWorker, start
from .markscan import MarkScanner
from .mergedview import MergedLogView
from .settingsdialog import SettingsDialog, Settled
from .strips import MinimapStrip, TimelineStrip

SWATCH = 10

LOG_SUFFIXES = frozenset({".log", ".txt", ".out", ".err", ".gz", ".jsonl", ".csv", ""})
"""
What a *directory* drop picks up, and what the open dialog offers first.

Includes the empty suffix, because `syslog`, `messages` and `dmesg` have no
extension at all and are exactly the files someone drops a /var/log at.
"""


def expand(paths: list[Path]) -> list[Path]:
    """
    Turn whatever was handed to us into a list of files to open.

    A directory becomes the log-looking files inside it; a named file is always
    taken as named. Dragging or typing one file is a decision about that file,
    and second-guessing it would make an unusual extension impossible to open.
    A directory is a bulk action, where pulling in every README and .json
    sitting beside the logs is noise.

    One level deep only: recursing a log directory tree can mean thousands of
    files, with no way to take it back once they are indexing.
    """
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(
                c for c in p.iterdir()
                if c.is_file() and c.suffix.lower() in LOG_SUFFIXES
            ))
        else:
            out.append(p)
    return out


class AliasDelegate(QStyledItemDelegate):
    """
    Lets a dock row be renamed without offering its entry count for editing.

    `QListWidgetItem` stores `DisplayRole` and `EditRole` in the same place, so
    a row reading "app   1,125" hands all of that to the editor and whatever
    the user leaves behind becomes the name. A delegate is the supported way to
    separate the two: it fills the editor from the alias and sends what comes
    back to the session, leaving the model's display string alone -- the window
    rebuilds the row afterwards, so the count reappears with the new name.
    """

    def __init__(self, parent, *, alias_of, rename):
        super().__init__(parent)
        self._alias_of = alias_of
        self._rename = rename

    def setEditorData(self, editor, index) -> None:  # noqa: N802  (Qt naming)
        editor.setText(self._alias_of(index.row()))

    def setModelData(self, editor, model, index) -> None:  # noqa: N802
        self._rename(index.row(), editor.text())


class MainWindow(QMainWindow):
    """One window, one session. Multiple windows are independent (FR-33)."""

    def __init__(self, session: Session | None = None, *,
                 background: bool = True, poller=None):
        super().__init__()
        self._poller = poller
        """
        A `source.poller.Poller` to watch through, or None for the shared one.

        Injectable so a follow test can step the watcher with `poll_once`
        instead of sleeping past an interval and hoping. A test that slept
        would be slow when it passed and intermittent when it failed, which is
        the worst available combination for the one feature whose whole job is
        to happen on its own.
        """
        self.session = session or Session()
        self.background_indexing = background
        """
        Whether `open_paths` warms the cache on a worker thread (NFR-5).

        Off makes opening synchronous, which is what a test wants -- and what a
        future headless export mode would want -- without having to drive an
        event loop to find out whether a file opened.
        """

        self.setWindowTitle("LogWeave")
        self.resize(1280, 800)
        self.setStyleSheet(theme.QSS)
        self.setAcceptDrops(True)

        self.highlight_group = default_group()
        """
        The three shipped rulesets and which are on (SPEC 10.1).

        Held here rather than in the view because it is a preference, not a
        drawing detail: the view is handed the flattened result and knows
        nothing about where the rules came from.
        """

        self.view = MergedLogView(self.session)
        self.minimap = MinimapStrip(self.session)
        self.timeline = TimelineStrip(self.session)
        self.finder = FindScanner(self.session, self)
        self.filterer = FilterScanner(self.session, self)
        """
        The two background scans M7 adds (FR-44, FR-45).

        Separate runners rather than one queue, because a find and a filter are
        not alternatives: a reader filters to the subsystem they care about and
        *then* searches inside it, and a shared runner would make the second
        request cancel the first.
        """
        self.finder.updated.connect(self._hits_ready)
        self.finder.partial.connect(self._hits_partial)
        self.finder.progress.connect(self._find_progress)
        self.filterer.updated.connect(self._filter_ready)
        self.filterer.partial.connect(self._filter_partial)
        self.filterer.progress.connect(self._filter_progress)
        self._export: ExportRun | None = None
        self._current_hit = -1
        """Row of the hit the reader is standing on, or -1. See `_step_hit`."""
        self._query = None
        """The last query handed to the finder, for spotting a new question."""
        self.setCentralWidget(self._build_centre())
        self.view.currentRowChanged.connect(self._update_position)
        self.view.copied.connect(self._report_copied)
        self.view.copyRefused.connect(self._report_copy_refused)
        self.view.fontPointChanged.connect(
            lambda pt: settings.write(settings.FONT_POINT, pt))
        self.marks = MarkScanner(self.session, self)
        """
        The background scan behind a rule's "also on the strips" (FR-31a).

        Owned here for the same reason the follow engine is: it is a property
        of how this window is being read, and the session knows nothing about
        it. Idle unless a rule asks for marks.
        """
        self.marks.updated.connect(self._marks_ready)
        self.marks.progress.connect(self._marks_progress)
        self.marks.started.connect(self._marks_started)

        self._restore_preferences()

        self._worker: IndexWorker | None = None
        self._thread = None
        self._pending: list[Path] = []
        self._queued: list[Path] = []

        self._notifier = FollowNotifier(self)
        self.follow = FollowEngine(self.session, poller=poller,
                                   notify=self._notifier.wake)
        """
        The follow engine (M6). Constructed always, started only on request.

        Owned by the window rather than the session because following is a
        choice about how this window is being used, and FR-33 allows several
        windows over the same files: one of them watching the tail should not
        drag the others to the bottom.
        """
        # Auto-connection, which is what makes this safe: the notifier lives on
        # this (the UI) thread and the signal is emitted from the poller's, so
        # Qt queues the call onto the event loop instead of running it there.
        self._notifier.woken.connect(self._follow_woken)
        self.view.tailingChanged.connect(self._tailing_changed)

        self._build_sources_dock()
        self._build_menus()
        self._build_status()
        self._restore_follow()
        if self._rule_notes:
            # Read before the status bar existed (`_restore_preferences` runs
            # ahead of the chrome), so it is reported here rather than lost.
            self._status_msg.setText("   ".join(self._rule_notes))
        self.refresh()

    # ------------------------------------------------------------------
    # the central layout (D4)
    # ------------------------------------------------------------------

    def _build_centre(self) -> QWidget:
        """
        The merged view with both overview strips beside it (D4, FR-32, FR-33).

        Order, left to right: view, minimap, timeline. The minimap is next to
        the view because it is the thing you scroll with -- it is a readable
        scrollbar, and a scrollbar belongs against the content it moves. The
        timeline goes outside it, where its blank stretches read as the edge of
        the window rather than as a gap between the view and its scrollbar.

        A plain `QHBoxLayout` and not a splitter: the strips are a fixed 64
        pixels and there is nothing to negotiate. A splitter would offer a drag
        handle for a width that is not meant to vary and would need its state
        remembered alongside the two booleans that actually matter (FR-34).
        """
        rows = QWidget(self)
        layout = QHBoxLayout(rows)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.minimap)
        layout.addWidget(self.timeline)

        # The two bars go *under* the strips and across the full width, not
        # beside the view: both of them talk about the whole merged stream,
        # and a filter count tucked into the view's width would sit above the
        # strips that the same filter has just redrawn.
        self.findbar = FindBar(self)
        self.findbar.hide()
        self.findbar.queryChanged.connect(self._find_query)
        self.findbar.stepped.connect(self._step_hit)
        self.findbar.closed.connect(self.close_find)
        self.findbar.cancelRequested.connect(self.finder.cancel)

        self.filterbar = FilterBar(self)
        self.filterbar.hide()
        self.filterbar.filterChanged.connect(self._filter_changed)
        self.filterbar.closed.connect(self.close_filter)
        self.filterbar.cancelRequested.connect(self.filterer.cancel)

        centre = QWidget(self)
        stack = QVBoxLayout(centre)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        stack.addWidget(rows, 1)
        stack.addWidget(self.filterbar)
        stack.addWidget(self.findbar)

        self.minimap.rowSought.connect(self._seek_row)
        self.timeline.timeSought.connect(self._seek_time)
        self.timeline.rangeSelected.connect(self._time_range_selected)
        # The strips need the viewport box kept in step with the view, and the
        # view does not know they exist. Both signals matter: `rowsChanged`
        # covers a resize or a file opening, `currentRowChanged` a keyboard
        # move, and the scrollbar covers the wheel -- which emits neither.
        self.view.rowsChanged.connect(self._sync_strips)
        self.view.currentRowChanged.connect(lambda _row: self._sync_strips())
        self.view.verticalScrollBar().valueChanged.connect(
            lambda _value: self._sync_strips())
        return centre

    def _seek_row(self, row: int) -> None:
        """A click on the minimap (FR-32)."""
        self.view.scroll_to_row(row)
        self.view.set_current_row(row)

    def _seek_time(self, wall_us: int) -> None:
        """
        A click on the timeline (FR-33).

        `scroll_to_time` is `time_to_row`, measured at 27 us with no block
        replay (NFR-6), which is why a drag down the timeline can seek on every
        mouse-move event rather than only on release.
        """
        self.view.scroll_to_time(wall_us)
        self.view.set_current_row(self.session.time_to_row(wall_us))

    def _sync_strips(self) -> None:
        """
        Tell the strips where the view is.

        Cheap on purpose -- it runs on every wheel step. `set_viewport` repaints
        without re-aggregating, and the timeline's marker is one row lookup, so
        a scroll costs a blit and not a strip rebuild.
        """
        top = self.view.top_row()
        page = self.view.visible_rows()
        self.minimap.set_viewport(top, page)
        self.timeline.set_viewport(top, page)
        self.timeline.set_marker_time(
            self.session.row_to_time(top) if self.session.total_rows else None)

    def _set_strips_visible(self, *, minimap: bool | None = None,
                            timeline: bool | None = None) -> None:
        """FR-34. Hidden rather than removed, so the layout keeps its order."""
        if minimap is not None:
            self.minimap.setVisible(bool(minimap))
            settings.write(settings.SHOW_MINIMAP, bool(minimap))
        if timeline is not None:
            self.timeline.setVisible(bool(timeline))
            settings.write(settings.SHOW_TIMELINE, bool(timeline))

    # ------------------------------------------------------------------
    # following (FR-21 to FR-26)
    # ------------------------------------------------------------------

    def _restore_follow(self) -> None:
        """FR-24 and FR-38: the global toggle survives a restart, defaulting off."""
        on = settings.read_bool(settings.FOLLOW, False)
        self.follow_act.setChecked(on)
        self._follow_toggled(on)

    def _follow_toggled(self, on: bool) -> None:
        """
        The global switch (FR-24).

        Turning it on starts tailing as well as watching, because the two
        together are what "follow" means to a reader; turning it off stops
        both and leaves everything already read exactly where it is.
        """
        on = bool(on)
        self.follow.set_global_follow(on)
        settings.write(settings.FOLLOW, on)
        self.view.set_tailing(on)
        if not on:
            self._new_lines.clear()
        self._reload_source_list()
        self._update_follow_status()

    def _follow_woken(self) -> None:
        """
        A watcher saw something. Runs on the UI thread (see `followui`).

        Guarded on `has_pending` rather than applying unconditionally: several
        sources changing between two trips round the event loop produce several
        wake-ups for one batch of work, and `apply` has already taken it.
        """
        if not self.follow.has_pending:
            return
        self._after_tick(self.follow.apply())

    def _after_tick(self, tick) -> None:
        """Put one `FollowTick` on the screen."""
        if not tick.anything:
            return
        if tick.rebuilt or tick.truncated or tick.rotated:
            # A re-index or a new source changes the row count, the aliases and
            # the lane colours, so this is the full refresh and not the cheap one.
            self.refresh()
        else:
            self.view.refresh()
            self.minimap.refresh()
            self.timeline.refresh()
            self._sync_strips()
            self._update_span()
            self._reload_source_list()

        if self.view.tailing:
            self.view.scroll_to_tail()
            self._new_lines.clear()
        elif tick.added_rows:
            # FR-26: counted since the pause began, so it keeps accumulating
            # across ticks until the reader goes and looks.
            self._new_lines.set_count(self._new_lines.count + tick.added_rows)

        self._report_follow_events(tick)
        self._update_follow_status()

    def _report_follow_events(self, tick) -> None:
        """FR-23 and FR-25: say what happened to a file, rather than just doing it."""
        notes: list[str] = []
        for pos in tick.truncated:
            notes.append(f"{self._alias_at(pos)} was truncated by its writer "
                         f"and has been re-read")
        for pos in tick.rotated:
            notes.append(f"{self._alias_at(pos)} rotated; the new file is open beside it")
        for pos in tick.went_stale:
            if pos in tick.rotated:
                continue
            state = self._follow_state(pos)
            why = state.stale_reason if state else "no longer updating"
            notes.append(f"{self._alias_at(pos)} is stale: {why}")
        if notes:
            self._status_msg.setText("; ".join(notes))

    def _alias_at(self, pos: int) -> str:
        if 0 <= pos < len(self.session.sources):
            return self.session.sources[pos].alias
        return f"source {pos}"

    def _follow_state(self, pos: int):
        if not 0 <= pos < len(self.session.sources):
            return None
        return self.follow.states.get(str(self.session.sources[pos].source.id))

    def _tailing_changed(self, on: bool) -> None:
        """FR-22: scrolling up pauses, arriving back at the bottom resumes."""
        if on:
            self._new_lines.clear()
        self._update_follow_status()

    def _resume_tailing(self) -> None:
        """The new-lines indicator was pressed -- the same thing `End` does."""
        self.view.set_tailing(True)
        self.view.set_current_row(max(0, self.session.total_rows - 1))
        self._new_lines.clear()

    def _update_follow_status(self) -> None:
        if not self.follow.global_follow:
            self._status_follow.setText("")
            return
        live = sum(1 for st in self.follow.states.values() if st.live)
        stale = sum(1 for st in self.follow.states.values() if st.stale)
        bits = [f"following {live}"]
        if stale:
            bits.append(f"{stale} stale")
        if not self.view.tailing:
            bits.append("paused")
        self._status_follow.setText("  ".join(bits))

    def _source_follow_toggled(self, pos: int, on: bool) -> None:
        """FR-24: follow this one file or not, independently of the others."""
        self.follow.set_follow(pos, on)
        self._reload_source_list()
        self._update_follow_status()

    # ------------------------------------------------------------------
    # preferences (FR-38)
    # ------------------------------------------------------------------

    def _restore_preferences(self) -> None:
        """
        Put the view back the way it was last time.

        Applied straight to the view rather than through the menu actions,
        because the menus are built afterwards and read their checked state
        from here -- one direction only, so there is no moment where the menu
        and the view disagree.
        """
        self.view.set_font_point(settings.font_point())
        self.view.set_lane_width(settings.lane_width())
        self.view.set_show_timestamp(settings.read_bool(settings.SHOW_TIMESTAMP, True))
        self.view.set_show_alias(settings.read_bool(settings.SHOW_ALIAS, False))
        self.view.set_show_line_no(settings.read_bool(settings.SHOW_LINE_NO, False))
        # Both strips on by default: D4's whole claim is that you want the two
        # of them, and a feature nobody finds in a menu is a feature that was
        # not built.
        self.minimap.setVisible(settings.read_bool(settings.SHOW_MINIMAP, True))
        self.timeline.setVisible(settings.read_bool(settings.SHOW_TIMELINE, True))

        rules, notes = rulestore.load(settings.store().value(settings.USER_RULES))
        self.highlight_group.sets[USER_SET] = Ruleset(rules)
        self._rule_notes = tuple(notes)
        """
        Complaints from reading the stored rules, reported once the status bar
        exists. A hand-edited rule that will not compile is the user's to fix,
        and silence would leave them hunting for a colour that never comes.
        """

        remembered = settings.store().value(settings.HIGHLIGHTS)
        if isinstance(remembered, str):
            wanted = {name for name in remembered.split(",") if name}
            # Intersected with what exists: a set removed in a later version
            # must not come back as a name nothing answers to.
            self.highlight_group.active = wanted & set(self.highlight_group.sets)
        self._apply_highlights()

    def _apply_highlights(self) -> None:
        """
        Flatten the active rulesets into the view, repaint, and re-mark.

        The mark request is here rather than at each call site because every
        one of them -- a menu toggle, the settings dialog, a restore at
        startup -- changes the same thing, and `MarkScanner.request` already
        decides for itself whether the answer can have moved.
        """
        self.view.highlights = self.highlight_group.flatten()
        settings.write(settings.HIGHLIGHTS, ",".join(sorted(self.highlight_group.active)))
        self.view.viewport().update()
        self.marks.request(self.view.highlights.rules)

    # ------------------------------------------------------------------
    # settings (FR-31, FR-52)
    # ------------------------------------------------------------------

    def settings_dialog(self) -> SettingsDialog:
        """
        Built over the *live* window, not over the settings store.

        Anything changed from a menu since the last write is in the window and
        may not yet be in the store, so reading the store here would show a
        stale value and Apply would then write it back -- a dialog that undid a
        menu by being opened.
        """
        current = Settled(
            font_point=self.view.font_point,
            lane_width=self.view.lane_width,
            show_timestamp=self.view.show_timestamp,
            show_alias=self.view.show_alias,
            show_line_no=self.view.show_line_no,
            show_minimap=self.minimap.isVisibleTo(self),
            show_timeline=self.timeline.isVisibleTo(self),
        )
        dialog = SettingsDialog(self.highlight_group, current=current, parent=self)
        dialog.changed.connect(self._settings_changed)
        return dialog

    def open_settings(self) -> None:
        self.settings_dialog().exec()

    def _settings_changed(self, settled: Settled) -> None:
        """
        Install everything the dialog settled, then let the menus catch up.

        Order matters once: the rules go in before `_apply_highlights`, because
        that is what flattens them into the view and asks for a mark scan.
        """
        self.view.set_font_point(settled.font_point)
        settings.write(settings.FONT_POINT, settled.font_point)
        self._set_lane_width(settled.lane_width)
        self._timestamp_toggled(settled.show_timestamp)
        self._alias_toggled(settled.show_alias)
        self._line_no_toggled(settled.show_line_no)
        self._set_strips_visible(minimap=settled.show_minimap,
                                 timeline=settled.show_timeline)

        self.highlight_group.sets[USER_SET] = Ruleset(settled.user_rules)
        self.highlight_group.active = (
            settled.active_sets & set(self.highlight_group.sets))
        settings.write(settings.USER_RULES, rulestore.dump(settled.user_rules))
        self._apply_highlights()
        self._sync_setting_menus()

        marked = sum(1 for r in settled.user_rules if r.enabled and r.on_strips)
        rules = len(self.view.highlights.rules)
        extra = f", {marked} marked on the strips" if marked else ""
        self._status_msg.setText(f"{rules} highlight rules active{extra}")

    def _sync_setting_menus(self) -> None:
        """
        Put the menu ticks back in step with what was just applied.

        One direction only, and guarded, because every one of these actions has
        a `toggled` handler that writes a setting: re-checking them without the
        guard would re-enter the handlers and, for the strips, fight the
        visibility that was just set.
        """
        pairs = [
            (self.timestamp_act, self.view.show_timestamp),
            (self.alias_act, self.view.show_alias),
            (self.line_no_act, self.view.show_line_no),
            (self.minimap_act, self.minimap.isVisibleTo(self)),
            (self.timeline_act, self.timeline.isVisibleTo(self)),
        ]
        for name, act in self.highlight_acts.items():
            pairs.append((act, name in self.highlight_group.active))
        width = self.view.lane_width
        for act in self.lane_acts:
            pairs.append((act, act.data() == width))
        for act, on in pairs:
            if act.isChecked() != on:
                act.blockSignals(True)
                act.setChecked(on)
                act.blockSignals(False)

    # ------------------------------------------------------------------
    # mark scanning (FR-31a)
    # ------------------------------------------------------------------

    def _marks_started(self) -> None:
        self._status_msg.setText("Scanning for marked highlight rules...")

    def _marks_progress(self, done: int, total: int, rule: str) -> None:
        if total > 0:
            self._status_msg.setText(
                f"Scanning for {rule}... {done * 100 // total}%")

    def _marks_ready(self, result) -> None:
        """
        Push the finished layers onto both strips and say what was found.

        Both strips, not just the minimap: a mark answers two questions --
        which part of the stream, and which part of the day -- and those are
        exactly the two the strips answer, so a hit visible on one of them only
        would be the odd thing out in an overview built on D4's premise that a
        reader wants both.
        """
        self.minimap.set_marks(result.layers)
        self.timeline.set_marks(result.layers)
        if not result.layers:
            return
        hits = result.hits
        rules = len(result.layers)
        plural_h = "s" if hits != 1 else ""
        plural_r = "s" if rules != 1 else ""
        parts = [f"{hits:,} hit{plural_h} from {rules} marked rule{plural_r} "
                 f"in {result.elapsed_ms / 1000:.1f}s"]
        parts.extend(result.notes)
        self._status_msg.setText("   ".join(parts))

    def _toggle_highlight_set(self, name: str, on: bool) -> None:
        if on:
            self.highlight_group.active.add(name)
        else:
            self.highlight_group.active.discard(name)
        self._apply_highlights()
        rules = len(self.view.highlights.rules)
        self._status_msg.setText(
            f"{name} highlighting {'on' if on else 'off'}, {rules} rules active")

    # ------------------------------------------------------------------
    # find (FR-44)
    # ------------------------------------------------------------------

    def open_find(self) -> None:
        """
        Show the find bar and put the cursor in it (Ctrl+F).

        Ctrl+F is the find key everywhere else, which is why following gave it
        up for Ctrl+Shift+F in M7: a reader who hits Ctrl+F expecting a search
        box and gets the view scrolled to the tail has lost their place in
        exchange for something they did not ask for.
        """
        self.findbar.show()
        self.findbar.focus()

    def close_find(self) -> None:
        """
        Esc or the bar's ✕: stop searching and take the marks away.

        The result is *kept* -- `FindScanner` caches it against the shape of
        the session, so re-opening the bar on the same term costs nothing and
        reads no bytes.
        """
        self.finder.cancel()
        self.findbar.hide()
        self.findbar.set_scanning(False)
        self._current_hit = -1
        self.view.set_find(None)
        for strip in (self.minimap, self.timeline):
            strip.set_find_marks(None)
            strip.set_find_current(None)
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _find_query(self, query) -> None:
        """A new (debounced) query from the bar."""
        if query != self._query:
            # A different question: the hit the reader was standing on is not a
            # hit any more, so stepping must start from the cursor again.
            self._query = query
            self._current_hit = -1
        self.view.set_find(query, self._current_hit)
        started = self.finder.request(query)
        self.findbar.set_scanning(started)
        if started:
            self._status_msg.setText(f"Searching for {query.describe()}...")

    def _find_progress(self, done: int, total: int) -> None:
        self.findbar.set_progress(done, total)

    def _hits_partial(self, hits) -> None:
        """A snapshot from a scan still running -- NFR-10's "incremental"."""
        self._hits_ready(hits, final=False)

    def _hits_ready(self, hits, *, final: bool = True) -> None:
        self.findbar.set_scanning(not final or self.finder.running)
        self.findbar.set_hits(hits, self._current_hit)
        layer = hits.layer(theme.FIND_MARK) if hits.count else None
        for strip in (self.minimap, self.timeline):
            strip.set_find_marks(layer)
        self.view.set_find(hits.query, self._current_hit)
        if hits.count and self._current_hit < 0:
            # The first result moves the cursor to the first match at or after
            # where the reader already is -- that is what makes an incremental
            # find incremental. Later snapshots do not, because `_current_hit`
            # is then set and a view that re-jumped every 120 ms would be
            # unusable while a 10 GB scan finished.
            self._step_hit(1)
        if final:
            self._report_hits(hits)

    def _report_hits(self, hits) -> None:
        if hits.query.empty:
            return
        if why := hits.query.validate():
            self._status_msg.setText(why)
            return
        bits = [f"{hits.count:,} match{'' if hits.count == 1 else 'es'} "
                f"for {hits.query.describe()}"]
        if hits.hidden:
            bits.append(f"{hits.hidden:,} in rows the view is not showing")
        if hits.truncated:
            bits.append("stopped at the hit limit")
        if hits.bytes_scanned:
            bits.append(f"{hits.bytes_scanned / 1e6:,.0f} MB in "
                        f"{hits.elapsed_ms / 1000:.1f}s")
        bits.extend(hits.notes)
        self._status_msg.setText("   ".join(bits))

    def _step_hit(self, delta: int) -> None:
        """
        Next or previous match (F3 / Shift+F3, and the bar's arrows).

        Stepping is relative to the hit the reader is standing on when there is
        one, and to the cursor when there is not -- so F3 after scrolling
        somewhere else carries on from where they are looking rather than from
        where they last pressed it.
        """
        hits = self.finder.result
        if not hits.count:
            if not hits.query.empty and hits.complete:
                self._status_msg.setText(
                    f"No matches for {hits.query.describe()}")
            return
        if self._current_hit >= 0:
            row = (hits.next_after(self._current_hit) if delta >= 0
                   else hits.prev_before(self._current_hit))
        else:
            # Inclusive of the cursor's own row, which `next_after` and
            # `prev_before` are not: a match on the row being looked at is the
            # first thing F3 should land on, not the thing it skips.
            at = self.view.current_row()
            row = (hits.next_after(at - 1) if delta >= 0
                   else hits.prev_before(at + 1))
        if row is None:
            return
        self._goto_hit(int(row))

    def _goto_hit(self, row: int) -> None:
        self._current_hit = int(row)
        self.view.set_current_row(row)
        self.view.set_current_hit(row)
        self.findbar.set_hits(self.finder.result, row)
        when = self.session.row_to_time(row) if self.session.total_rows else None
        for strip in (self.minimap, self.timeline):
            strip.set_find_current(when)

    # ------------------------------------------------------------------
    # filtering (FR-45, FR-48)
    # ------------------------------------------------------------------

    def open_filter(self) -> None:
        self.filterbar.set_sources([s.alias for s in self.session.sources])
        self.filterbar.show()
        self.filterbar.focus()

    def close_filter(self) -> None:
        """
        Esc or the bar's ✕: drop the filter as well as hiding the bar.

        Hiding a filter that is still in force would leave the view showing
        some of the log with nothing on screen saying so -- the one state
        `filterbar`'s docstring calls the way this application could mislead
        someone badly. So closing the bar is `clear()`, which re-emits an empty
        filter and takes this back through `_filter_changed`.
        """
        self.filterer.cancel()
        self.filterbar.clear()
        self.filterbar.hide()
        self.filterbar.set_scanning(False)
        self.view.setFocus(Qt.FocusReason.OtherFocusReason)

    def clear_filter(self) -> None:
        """Menu and Ctrl+Shift+Backspace: show everything again (FR-45)."""
        if self.filterbar.isVisibleTo(self) or self.session.is_filtered:
            self.close_filter()

    def _filter_changed(self, filt) -> None:
        """
        A filter to evaluate. Tier 1 here, tier 2 on the scanner's thread.

        The split is NFR-9's 500 ms. A severity, source or time-window filter
        is a few vectorised comparisons per source plus a *derived* set of
        checkpoints (`MergeIndex.derive_checkpoints`), so it is applied in the
        same turn as the click that asked for it -- going through a thread
        would add a round trip to something already finished. A text filter has
        to read every byte, so it does not come near this thread.
        """
        if why := filt.validate():
            self.filterbar.set_result(None)
            self._status_msg.setText(why)
            return
        if filt.tier <= 1:
            self.filterer.cancel()
            self.filterbar.set_scanning(False)
            self._filter_ready(filtering.apply(self.session, filt))
            return
        self.filterbar.set_scanning(True)
        self._status_msg.setText(f"Filtering: {filt.describe()}...")
        self.filterer.request(filt)

    def _filter_progress(self, done: int, total: int) -> None:
        self.filterbar.set_progress(done, total)

    def _filter_partial(self, result) -> None:
        self._filter_ready(result, final=False)

    def _filter_ready(self, result, *, final: bool = True) -> None:
        """
        Apply an evaluated filter to the session and put the view back.

        **Row numbers move when a filter changes and times do not**, so the
        reader's place is kept in time: where they were is read before the
        filter is applied and sought again afterwards. Without this, filtering
        a 10M-row log while reading row 4,000,000 leaves the view at whatever
        is now four million rows in, which is somewhere else entirely.

        Only on a completed pass. A partial result lands here every 120 ms
        while a text filter scans (NFR-10), and a view that re-sought on each
        one would pull the rows out from under anyone reading them.
        """
        active = result.filt.active
        was = (self.session.row_to_time(self.view.current_row())
               if final and self.session.total_rows else None)
        self.session.set_filter(result if active else None)
        self.filterbar.set_result(result if active else None)
        self.filterbar.set_scanning(not final)
        self.timeline.set_window(
            (result.filt.since, result.filt.until)
            if active and (result.filt.since is not None
                           or result.filt.until is not None) else None)
        self.refresh()
        if was is not None and self.session.total_rows:
            self.view.scroll_to_time(was)
            self.view.set_current_row(self.session.time_to_row(was))
        # The hits survive a filter -- they are `(source, entry)` pairs -- but
        # their row numbers do not, so they are re-derived rather than rescanned
        # (`Hits.locate`). Same for the rule marks, which are already in time.
        self.finder.relocate()
        self._update_filter_status(result if active else None)

    def _update_filter_status(self, result) -> None:
        """
        Repeat the filter in the status bar, in `theme.FILTER_ACTIVE`.

        Twice on screen on purpose: the bar can be scrolled past, closed or
        simply not looked at, and "some rows are missing" is the one fact about
        this view that a reader must not be able to miss.
        """
        if result is None:
            self._status_filter.setText("")
            self._status_msg.setText("Filter cleared")
            return
        text = result.describe()
        self._status_filter.setText(f"   {text}   ")
        self._status_msg.setText(
            f"Filter: {result.filt.describe()} -- {text}"
            + ("   " + "   ".join(result.notes) if result.notes else ""))

    def _time_range_selected(self, since: int, until: int) -> None:
        """
        A range dragged down the timeline strip (FR-48).

        Opens the filter bar if it was closed, because the drag has just set a
        filter and the bar is where that is visible and reversible.
        """
        if not self.filterbar.isVisibleTo(self):
            self.filterbar.set_sources([s.alias for s in self.session.sources])
            self.filterbar.show()
        self.filterbar.set_window(int(since), int(until))

    # ------------------------------------------------------------------
    # navigating (FR-41, FR-42, FR-43)
    # ------------------------------------------------------------------

    def go_to_time(self) -> None:
        """FR-41 and FR-43: ask for a time and seek to it."""
        if self.session.is_empty:
            self._status_msg.setText("No logs open")
            return
        anchor = self.session.row_to_time(self.view.current_row())
        dialog = GoToTimeDialog(self, anchor_wall_us=anchor,
                                span=self.session.time_span())
        if dialog.exec() == QDialog.DialogCode.Accepted:
            when = dialog.wall_us()
            if when is not None:
                self._seek_time(int(when))
                self._status_msg.setText(
                    f"Moved to {format_wall_us(when, micros=False)}")

    def _jump(self, *, severities=None, source: int | None = None,
              back: bool = False, what: str = "") -> None:
        """
        FR-42's next/previous, by severity or by file.

        One pass over the entries actually skipped, in each source's own
        arrays, and nothing decoded -- see `session.nav`. Not wrapped: silently
        going back to the top when you ask for the next error is how you read
        the same error twice and think there are two, so the end of the log is
        reported instead.
        """
        if self.session.is_empty:
            return
        row = nav.next_row(self.session, self.view.current_row(),
                           severities=severities, source=source, back=back)
        if row is None:
            self._status_msg.setText(
                f"No {what} {'before' if back else 'after'} this row")
            return
        self.view.set_current_row(row)
        self._status_msg.setText(f"Moved to the {'previous' if back else 'next'} "
                                 f"{what}")

    def _jump_severity(self, level: int, *, back: bool = False) -> None:
        self._jump(severities=AT_LEAST[int(level)], back=back,
                   what=f"{Severity(level).name} or worse")

    def _jump_source(self, *, back: bool = False) -> None:
        """
        Next or previous row from the file the cursor is on (FR-42).

        The current row's source rather than a chosen one: "show me this file's
        next line" is the question someone has while reading a row, and
        answering it needs no dialog.
        """
        rows = self.session.rows(self.view.current_row(), 1)
        if not rows:
            return
        pos = int(rows[0].source)
        self._jump(source=pos, back=back,
                   what=f"row from {self.session.sources[pos].alias}")

    # ------------------------------------------------------------------
    # exporting (FR-47)
    # ------------------------------------------------------------------

    def export_view(self) -> None:
        """
        Write the view -- or the selection -- to a file.

        The run is driven by `ExportRun` off a zero-delay timer rather than a
        thread, for the reason `session.export` gives: `session.rows()` mutates
        the decoded-text LRU and the merge's block cache, so reading rows off
        the UI thread while the view paints is a data race on every row.
        """
        if self.session.is_empty:
            self._status_msg.setText("No logs open to export")
            return
        if self._export is not None:
            self._status_msg.setText("An export is already running")
            return
        lo, hi = self.view.selection()
        dialog = ExportDialog(
            self, rows=self.session.total_rows,
            selection=(lo, hi), filtered=self.session.is_filtered,
            columns=self.view.displayed_columns())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        spec = dialog.spec()
        self._export = ExportRun(self.session, dialog.formatter(self.session),
                                 spec, self)
        self._export.progress.connect(self._export_progress)
        self._export.finished.connect(self._export_finished)
        self._export_bar.setRange(0, 1000)
        self._export_bar.setValue(0)
        self._export_bar.show()
        self._export_cancel.show()
        self._status_msg.setText(f"Exporting to {spec.path.name}...")
        self._export.start()

    def cancel_export(self) -> None:
        if self._export is not None:
            self._export.cancel()

    def _export_progress(self, step) -> None:
        self._export_bar.setValue(int(1000 * step.fraction))

    def _export_finished(self, step) -> None:
        self._export = None
        self._export_bar.hide()
        self._export_cancel.hide()
        if step.error:
            self._status_msg.setText(f"Export failed: {step.error}")
            return
        if step.cancelled:
            self._status_msg.setText(
                f"Export cancelled after {step.written:,} rows -- the part "
                f"written is still on disk")
            return
        size = f"{step.bytes_written / 1e6:,.1f} MB" if step.bytes_written > 1e6 \
            else f"{step.bytes_written:,} bytes"
        notes = ("   " + "   ".join(step.notes)) if step.notes else ""
        self._status_msg.setText(
            f"Exported {step.written:,} rows ({size}){notes}")

    # ------------------------------------------------------------------
    # chrome
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_act = QAction("&Open Logs...", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self.open_dialog)
        file_menu.addAction(open_act)

        close_act = QAction("Close &All", self)
        close_act.triggered.connect(self.close_all)
        file_menu.addAction(close_act)

        file_menu.addSeparator()
        self.export_act = QAction("&Export View...", self)
        self.export_act.setShortcut("Ctrl+E")
        self.export_act.setStatusTip(
            "Write the view -- or just the selection -- to a text, CSV or "
            "Markdown file. Filters apply; the whole view streams (FR-47)")
        self.export_act.triggered.connect(self.export_view)
        file_menu.addAction(self.export_act)

        file_menu.addSeparator()
        self.cache_act = QAction("Clear &Index Cache", self)
        self.cache_act.setStatusTip(
            "Delete the saved indexes. Files already open keep working; the "
            "next open of a large log will be slow again"
        )
        self.cache_act.triggered.connect(self.clear_cache)
        file_menu.addAction(self.cache_act)

        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        edit_menu = self.menuBar().addMenu("&Edit")

        copy_act = QAction("&Copy With Attribution", self)
        copy_act.setShortcut(QKeySequence.StandardKey.Copy)
        copy_act.setStatusTip(
            "Copy the selected rows with the file and line each came from")
        copy_act.triggered.connect(lambda: self.view.copy_selection(annotated=True))
        edit_menu.addAction(copy_act)

        copy_plain_act = QAction("Copy &Plain Text", self)
        copy_plain_act.setShortcut("Ctrl+Shift+C")
        copy_plain_act.setStatusTip(
            "Copy the selected rows exactly as they appear in their own files, "
            "with no timestamp column and no attribution")
        copy_plain_act.triggered.connect(self.view.copy_selection_plain)
        edit_menu.addAction(copy_plain_act)

        # FR-46b: the same rows in whatever shape the destination wants. A
        # submenu rather than a dialog, because the choice is remembered by the
        # reader and not by the application -- someone pasting into a
        # spreadsheet wants CSV every time, and a dialog in the way of Ctrl+C
        # would be the thing they complain about.
        copy_as = edit_menu.addMenu("Copy &As")
        self.copy_as_acts = {}
        for preset in Preset:
            if preset is Preset.TEMPLATE:
                # A template needs somewhere to type it, which is the export
                # dialog's job. Offering it here would be a menu item that
                # silently used whatever the default template happens to be.
                continue
            act = QAction(PRESET_LABELS[preset], self)
            act.triggered.connect(
                lambda _c=False, preset=preset: self.view.copy_as(preset))
            copy_as.addAction(act)
            self.copy_as_acts[preset] = act

        select_all_act = QAction("Select &All", self)
        select_all_act.setShortcut(QKeySequence.StandardKey.SelectAll)
        select_all_act.triggered.connect(self.view.select_all)
        edit_menu.addAction(select_all_act)
        self.select_all_act = select_all_act
        edit_menu.addSeparator()

        settings_act = QAction("&Settings…", self)
        # The platform shortcut for preferences: Ctrl+, on Windows and Linux,
        # Cmd+, on macOS, which is what Qt's Preferences role already maps to.
        settings_act.setShortcut(QKeySequence("Ctrl+,"))
        settings_act.setMenuRole(QAction.MenuRole.PreferencesRole)
        settings_act.setStatusTip(
            "Text size, columns, strips, and the highlight rules")
        settings_act.triggered.connect(self.open_settings)
        self.settings_act = settings_act
        edit_menu.addAction(settings_act)
        self.copy_act, self.copy_plain_act = copy_act, copy_plain_act

        self._build_search_menu()

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.sources_dock.toggleViewAction())

        self.timestamp_act = QAction("&Timestamp Column", self)
        self.timestamp_act.setCheckable(True)
        self.timestamp_act.setChecked(self.view.show_timestamp)
        self.timestamp_act.setShortcut("Ctrl+T")
        self.timestamp_act.setStatusTip(
            "Hide the parsed timestamp column when every line already carries "
            "its own; the message then keeps its leading timestamp"
        )
        self.timestamp_act.toggled.connect(self._timestamp_toggled)
        view_menu.addAction(self.timestamp_act)

        self.alias_act = QAction("Source &Alias Column", self)
        self.alias_act.setCheckable(True)
        self.alias_act.setChecked(self.view.show_alias)
        self.alias_act.setStatusTip(
            "Show each row's source as a short name beside the lanes (FR-29)")
        self.alias_act.toggled.connect(self._alias_toggled)
        view_menu.addAction(self.alias_act)

        self.line_no_act = QAction("&Line Number Column", self)
        self.line_no_act.setCheckable(True)
        self.line_no_act.setChecked(self.view.show_line_no)
        self.line_no_act.setStatusTip(
            "Show each entry's line number within its own file, not its "
            "position in the merged view")
        self.line_no_act.toggled.connect(self._line_no_toggled)
        view_menu.addAction(self.line_no_act)

        view_menu.addSeparator()
        self.minimap_act = QAction("&Minimap", self)
        self.minimap_act.setCheckable(True)
        # `isVisibleTo`, not `isVisible`. The menus are built before the window
        # is shown, and a child of an unmapped window reports `isVisible()`
        # False however many times `setVisible(True)` was called on it -- so
        # both strips would be ticked off in the menu and drawn on the screen
        # the moment the window appeared. That is the M4 lane-width trap in a
        # new place: a menu built from one source of truth and a widget set
        # from another. `isVisibleTo(self)` asks the question actually being
        # asked -- would this be visible once its ancestors are.
        self.minimap_act.setChecked(self.minimap.isVisibleTo(self))
        self.minimap_act.setShortcut("Ctrl+M")
        self.minimap_act.setStatusTip(
            "Row-proportional overview of the whole merged stream; click or "
            "drag it to scroll (FR-32)")
        self.minimap_act.toggled.connect(
            lambda on: self._set_strips_visible(minimap=on))
        view_menu.addAction(self.minimap_act)

        self.timeline_act = QAction("Time&line", self)
        self.timeline_act.setCheckable(True)
        self.timeline_act.setChecked(self.timeline.isVisibleTo(self))
        self.timeline_act.setShortcut("Ctrl+L")
        self.timeline_act.setStatusTip(
            "Wall-clock overview: idle gaps show as blank, bursts as dense "
            "bands; click to seek to a time (FR-33)")
        self.timeline_act.toggled.connect(
            lambda on: self._set_strips_visible(timeline=on))
        view_menu.addAction(self.timeline_act)

        view_menu.addSeparator()
        self.follow_act = QAction("&Follow New Lines", self)
        self.follow_act.setCheckable(True)
        # Ctrl+Shift+F, not Ctrl+F. M7 gave Ctrl+F to the find bar, which is
        # what that key does in every other application a log reader has open:
        # someone pressing it for a search box and getting the view dragged to
        # the tail has lost their place in exchange for something they did not
        # ask for.
        self.follow_act.setShortcut("Ctrl+Shift+F")
        self.follow_act.setStatusTip(
            "Watch every open file for new lines and keep the view at the end; "
            "scroll up to pause, End to resume (FR-21, FR-22)")
        self.follow_act.toggled.connect(self._follow_toggled)
        view_menu.addAction(self.follow_act)

        view_menu.addSeparator()
        self._build_zoom_menu(view_menu)
        view_menu.addSeparator()
        self._build_lane_menu(view_menu)
        self._build_highlight_menu(view_menu)

    def _build_search_menu(self) -> None:
        """
        Find, filter and go-to, on one menu -- FR-41 to FR-45.

        Its own menu rather than more of Edit: these are the five things a
        reader does *to find out where to look*, and they are the only commands
        in the window with a mode attached (a bar open, a filter in force). A
        menu that says so is also where the shortcuts are discoverable, which
        for F3 and Ctrl+G is the only place they can be.
        """
        menu = self.menuBar().addMenu("&Search")

        self.find_act = QAction("&Find...", self)
        self.find_act.setShortcut(QKeySequence.StandardKey.Find)
        self.find_act.setStatusTip(
            "Search every open file as you type; hits are marked in the view "
            "and on both strips (FR-44)")
        self.find_act.triggered.connect(self.open_find)
        menu.addAction(self.find_act)

        # F3 and Shift+F3 spelled out, **not** `StandardKey.FindNext`. That
        # standard key expands to two sequences on Windows -- F3 *and* Ctrl+G
        # -- which broke stepping twice over: listing it beside an explicit F3
        # registered F3 on this action twice, and Qt answers a shortcut it
        # holds two bindings for with `QAction::event: Ambiguous shortcut
        # overload: F3` and then does nothing; and its Ctrl+G is the key this
        # window gives to Go to Timestamp (FR-41), so that was ambiguous
        # between two different actions as well. An explicit sequence has
        # exactly the binding it says.
        self.find_next_act = QAction("Find &Next", self)
        self.find_next_act.setShortcut(QKeySequence("F3"))
        self.find_next_act.triggered.connect(lambda: self._step_hit(1))
        menu.addAction(self.find_next_act)

        self.find_prev_act = QAction("Find &Previous", self)
        self.find_prev_act.setShortcut(QKeySequence("Shift+F3"))
        self.find_prev_act.triggered.connect(lambda: self._step_hit(-1))
        menu.addAction(self.find_prev_act)

        menu.addSeparator()
        self.filter_act = QAction("Fi&lter...", self)
        self.filter_act.setShortcut("Ctrl+Shift+L")
        self.filter_act.setStatusTip(
            "Show only the rows matching a severity, source, time window or "
            "text; reversible without re-indexing (FR-45)")
        self.filter_act.triggered.connect(self.open_filter)
        menu.addAction(self.filter_act)

        self.clear_filter_act = QAction("&Clear Filter", self)
        self.clear_filter_act.setShortcut("Ctrl+Shift+K")
        self.clear_filter_act.triggered.connect(self.clear_filter)
        menu.addAction(self.clear_filter_act)

        menu.addSeparator()
        self.goto_act = QAction("&Go to Timestamp...", self)
        self.goto_act.setShortcut("Ctrl+G")
        self.goto_act.setStatusTip(
            "Jump to a time: a whole timestamp, a time of day, or a step "
            "like +5m from where you are (FR-41, FR-43)")
        self.goto_act.triggered.connect(self.go_to_time)
        menu.addAction(self.goto_act)

        menu.addSeparator()
        # FR-42, as four keys rather than a submenu per severity. "At least
        # WARNING" and "at least ERROR" are what people mean nine times out of
        # ten -- the same judgement the filter bar's quick items make.
        for level, key in ((Severity.ERROR, "F8"),
                           (Severity.WARNING, "Ctrl+F8")):
            nxt = QAction(f"Next {level.name} or Worse", self)
            nxt.setShortcut(key)
            nxt.triggered.connect(
                lambda _c=False, level=level: self._jump_severity(int(level)))
            menu.addAction(nxt)
            prv = QAction(f"Previous {level.name} or Worse", self)
            prv.setShortcut(f"Shift+{key}" if key == "F8" else "Ctrl+Shift+F8")
            prv.triggered.connect(
                lambda _c=False, level=level: self._jump_severity(
                    int(level), back=True))
            menu.addAction(prv)

        menu.addSeparator()
        self.next_source_act = QAction("Next Row From &This File", self)
        self.next_source_act.setShortcut("Alt+Down")
        self.next_source_act.triggered.connect(lambda: self._jump_source())
        menu.addAction(self.next_source_act)

        self.prev_source_act = QAction("Previous Row From This &File", self)
        self.prev_source_act.setShortcut("Alt+Up")
        self.prev_source_act.triggered.connect(
            lambda: self._jump_source(back=True))
        menu.addAction(self.prev_source_act)
        self.search_menu = menu

    def _build_zoom_menu(self, view_menu) -> None:
        """
        Text size (FR-38), remembered between runs.

        Three shortcuts rather than a dialog, plus Ctrl+wheel in the view: this
        is something people reach for while reading, often because they have
        just moved the window to a different monitor.
        """
        bigger = QAction("Zoom &In", self)
        bigger.setShortcuts([QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")])
        bigger.triggered.connect(lambda: self.view.zoom(1))
        view_menu.addAction(bigger)

        smaller = QAction("Zoom &Out", self)
        smaller.setShortcut(QKeySequence.StandardKey.ZoomOut)
        smaller.triggered.connect(lambda: self.view.zoom(-1))
        view_menu.addAction(smaller)

        reset = QAction("&Reset Text Size", self)
        reset.setShortcut("Ctrl+0")
        reset.triggered.connect(self.view.reset_font)
        view_menu.addAction(reset)
        self.zoom_in_act, self.zoom_out_act, self.zoom_reset_act = \
            bigger, smaller, reset

    def _build_lane_menu(self, view_menu) -> None:
        """
        FR-28: the lanes are configurable between 4 and 12 pixels.

        Every width in the range, not every other one. Stepping by two left the
        shipped default of seven unreachable from the menu and nothing ticked,
        so the menu disagreed with the view the moment the window opened -- and
        a width restored from the store had the same problem.
        """
        menu = view_menu.addMenu("Lane &Width")
        self.lane_acts = []
        group = QActionGroup(self)
        group.setExclusive(True)
        for px in range(theme.LANE_WIDTH_MIN, theme.LANE_WIDTH_MAX + 1):
            act = QAction(f"{px} px", self)
            act.setCheckable(True)
            # The width lives on the action, so anything that has to put these
            # ticks back in step -- `_sync_setting_menus`, after the settings
            # dialog -- can ask rather than reconstruct it from the list index.
            act.setData(px)
            act.setChecked(px == self.view.lane_width)
            act.triggered.connect(lambda _=False, w=px: self._set_lane_width(w))
            group.addAction(act)
            menu.addAction(act)
            self.lane_acts.append(act)

    SET_LABELS = {USER_SET: "My Rules"}
    """
    Menu labels that are not just the set name capitalised.

    Only one so far. `USER_SET` is called `user` in the store, in the settings
    key and in every test, and renaming it to read well in a menu would mean a
    migration for a string nobody sees.
    """

    def _build_highlight_menu(self, view_menu) -> None:
        """
        FR-30/FR-31: which rulesets are in force, including the user's own.

        A switch per set, the user's included, so that "turn my colours off for
        a minute" is one click from the same place as the shipped ones. The
        rules themselves are edited in the settings dialog; this menu only ever
        answers which sets apply.
        """
        menu = view_menu.addMenu("&Highlighting")
        self.highlight_acts = {}
        for name in sorted(self.highlight_group.sets):
            act = QAction(self.SET_LABELS.get(name, name.capitalize()), self)
            act.setCheckable(True)
            act.setChecked(name in self.highlight_group.active)
            act.setStatusTip(f"Apply the {name} highlight rules")
            act.toggled.connect(
                lambda on, n=name: self._toggle_highlight_set(n, on))
            menu.addAction(act)
            self.highlight_acts[name] = act
        menu.addSeparator()
        edit_rules = QAction("&Edit Rules…", self)
        edit_rules.setStatusTip("Add or change your own highlight rules")
        # Straight to the tab that was asked for, rather than to the dialog's
        # first tab: an item called "Edit Rules" that lands on text size is an
        # item that has not done what it said.
        edit_rules.triggered.connect(self.open_highlight_settings)
        menu.addAction(edit_rules)

    def open_highlight_settings(self) -> None:
        """The settings dialog, opened on the highlighting tab."""
        dialog = self.settings_dialog()
        dialog.tabs.setCurrentWidget(dialog.highlight)
        dialog.exec()

    def _timestamp_toggled(self, on: bool) -> None:
        self.view.set_show_timestamp(on)
        settings.write(settings.SHOW_TIMESTAMP, on)

    def _alias_toggled(self, on: bool) -> None:
        self.view.set_show_alias(on)
        settings.write(settings.SHOW_ALIAS, on)

    def _line_no_toggled(self, on: bool) -> None:
        self.view.set_show_line_no(on)
        settings.write(settings.SHOW_LINE_NO, on)

    def _set_lane_width(self, px: int) -> None:
        self.view.set_lane_width(px)
        settings.write(settings.LANE_WIDTH, px)

    def _build_status(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self._status_msg = QLabel("No logs open")
        self._status_pos = QLabel("")
        self._status_span = QLabel("")
        self._status_follow = QLabel("")
        self._status_follow.setStyleSheet(f"color: {theme.FOLLOW_ACTIVE};")
        self._status_filter = QLabel("")
        self._status_filter.setStyleSheet(f"color: {theme.FILTER_ACTIVE};")
        """
        What the filter is keeping out, permanently on show while one is in
        force (FR-45). See `_update_filter_status` for why it is said twice.
        """

        # The export gets its own bar and button rather than sharing the
        # indexing pair: a drop can be indexing while an export runs, and one
        # Cancel button wired to whichever started last is the kind of control
        # that loses someone an hour of writing.
        self._export_bar = QProgressBar()
        self._export_bar.setMaximumWidth(180)
        self._export_bar.setTextVisible(True)
        self._export_bar.hide()
        self._export_cancel = QPushButton("Stop Export")
        self._export_cancel.setFlat(True)
        self._export_cancel.clicked.connect(self.cancel_export)
        self._export_cancel.hide()

        # FR-26, in the status bar rather than floated over the last row: an
        # overlay would sit on top of the text someone scrolled up to read,
        # which is the one thing they are known to be looking at.
        self._new_lines = NewLinesButton(self)
        self._new_lines.clicked.connect(self._resume_tailing)

        # The progress bar lives in the status bar rather than a modal: the
        # window stays usable while a drop indexes, which is the whole point of
        # moving the work off this thread.
        self._progress = QProgressBar()
        self._progress.setMaximumWidth(180)
        self._progress.setTextVisible(True)
        self._progress.hide()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setFlat(True)
        self._cancel_btn.clicked.connect(self.cancel_indexing)
        self._cancel_btn.hide()

        bar.addWidget(self._status_msg, 1)
        bar.addWidget(self._new_lines)
        bar.addWidget(self._progress)
        bar.addWidget(self._cancel_btn)
        bar.addWidget(self._export_bar)
        bar.addWidget(self._export_cancel)
        bar.addPermanentWidget(self._status_filter)
        bar.addPermanentWidget(self._status_follow)
        bar.addPermanentWidget(self._status_span)
        bar.addPermanentWidget(self._status_pos)

    def _build_sources_dock(self) -> None:
        self.sources_dock = QDockWidget("Sources", self)
        self.sources_dock.setObjectName("sources")
        self.sources_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.source_list = QListWidget()
        self.source_list.setEditTriggers(
            QListWidget.EditTrigger.DoubleClicked
            | QListWidget.EditTrigger.EditKeyPressed
        )
        self.source_list.setItemDelegate(AliasDelegate(
            self.source_list,
            alias_of=self._alias_of_row,
            rename=self.rename_source,
        ))
        self.source_list.itemChanged.connect(self._source_toggled)
        # Per-source follow (FR-24) goes on a context menu rather than a second
        # checkbox. The row already has one, for whether the source is in the
        # merge (FR-9), and two checkboxes a few pixels apart meaning "shown"
        # and "watched" is a guessing game -- the one someone would reach for to
        # hide a noisy file is the one that would stop it being read.
        self.source_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.source_list.customContextMenuRequested.connect(self._source_menu)
        self.sources_dock.setWidget(self.source_list)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.sources_dock)
        self.sources_dock.setMinimumWidth(220)

    # ------------------------------------------------------------------
    # opening
    # ------------------------------------------------------------------

    def open_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open log files", "",
            "Logs (" + " ".join(f"*{x}" for x in sorted(LOG_SUFFIXES) if x)
            + ");;All files (*)",
        )
        if paths:
            self.open_paths([Path(p) for p in paths])

    def open_paths(self, paths: list[Path], *,
                   background: bool | None = None) -> None:
        """
        Open files and report honestly what happened (FR-19).

        Directories are expanded here rather than at the drop, so a drop, the
        open dialog and the command line all behave identically -- `logweave
        C:/logs` should do what dragging `C:/logs` does.

        A file with no recognised timestamp format still opens -- it is shown in
        file order with its entries carrying no time -- because "we could not
        parse it" is information the user should see next to the data, not a
        reason to refuse the file.

        The indexing itself happens on a worker thread (NFR-5): what returns
        here is only the knowledge that a sidecar now exists, after which the
        files are opened on this thread as cache hits. `background=False` does
        the lot inline, which is what the tests and the headless paths want.
        """
        if background is None:
            background = self.background_indexing
        files = expand(paths)
        if not files:
            self._status_msg.setText("nothing to open")
            return
        if not background or not self.session.use_cache:
            self._finish_open(files)
            return

        if self._worker is not None:
            # Already indexing. Queue the new paths rather than starting a
            # second pool: two pools on one machine fight for the same disk and
            # the same cores, and finish later than either would alone.
            self._queued.extend(files)
            self._status_msg.setText(
                f"indexing... {len(self._queued)} more file(s) queued")
            return

        self._begin_indexing(files)

    def _begin_indexing(self, files: list[Path]) -> None:
        self._pending = files
        self._progress.setRange(0, len(files))
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m")
        self._progress.show()
        self._cancel_btn.show()
        self._status_msg.setText(f"indexing {len(files)} file(s)...")

        self._worker = IndexWorker(files, root=self.session.cache_root,
                                   use_cache=self.session.use_cache)
        self._worker.progress.connect(self._indexing_progress)
        self._worker.finished.connect(self._indexing_finished)
        self._thread = start(self._worker, self)

    def _indexing_progress(self, done: int, total: int, name: str,
                           status: str) -> None:
        self._progress.setValue(done)
        self._status_msg.setText(f"indexing {name} ({status})")

    def _indexing_finished(self, result) -> None:
        self._worker = None
        self._join_thread()
        self._progress.hide()
        self._cancel_btn.hide()

        files, self._pending = self._pending, []
        if result.cancelled:
            # Whatever finished before the cancel is in the cache and costs
            # nothing to open, so the files that did get indexed are still
            # opened. Nothing is discarded for having been interrupted.
            done = {f.path for f in result.files}
            files = [p for p in files if str(p) in done]
            self._status_msg.setText("indexing cancelled")
            if not files:
                return

        self._finish_open(files, prepared=result)

        queued, self._queued = self._queued, []
        if queued:
            self._begin_indexing(queued)

    def _join_thread(self, timeout_ms: int = 5000) -> None:
        """
        Wait for the indexing thread to actually exit, then forget it.

        Dropping the reference is not enough. The `QThread` is a child of this
        window, so when the window is destroyed Qt destroys the thread too -- and
        destroying a `QThread` that is still running aborts the process rather
        than raising. It showed up as an intermittent crash *after* a test run
        had passed, which is exactly how a user would meet it: a window that
        crashes as it closes, having done everything right until then.

        The wait is bounded and, in practice, instantaneous: by the time this
        runs the worker has already returned from `run()`, so `quit()` only has
        to let an idle event loop notice.
        """
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.quit()
        if not thread.wait(timeout_ms):
            # It did not stop. Saying so is better than either hanging on it or
            # tearing it down underneath itself.
            self._status_msg.setText(
                "indexing did not stop cleanly; the window may close slowly")

    def cancel_indexing(self) -> None:
        self._queued.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._cancel_btn.setEnabled(False)

    def _finish_open(self, files: list[Path], prepared=None) -> None:
        result = self.session.add_files(files)
        # A file opened while following is on is followed from the start.
        # `sync` is idempotent, so calling it unconditionally is cheaper to
        # reason about than deciding here whether it is needed.
        self.follow.sync()
        self.refresh()
        # Opening a file changes which rows a mark falls on, so the cached scan
        # is void whatever the rules say. `request` then re-runs it only if
        # something is actually marked.
        self.marks.invalidate()
        self.marks.request(self.view.highlights.rules)
        # Same for the find and the filter, and for the same reason -- but they
        # are handled differently because they fail differently. The hits are
        # `(source, entry)` pairs that are still true of the files they came
        # from, so the scan is re-run and the new file searched too. A filter's
        # masks are per-source and one source long, so an evaluation that
        # predates the new file cannot be stretched over it: it is re-evaluated
        # from the bar, which is the only place that still knows what was
        # asked.
        self.finder.invalidate()
        if self.findbar.isVisibleTo(self):
            self._find_query(self.findbar.query())
        if self.session.is_filtered or self.filterbar.isVisibleTo(self):
            self._filter_changed(self.filterbar.filter())
        self._cancel_btn.setEnabled(True)

        parts = [result.summary()]
        if result.no_format:
            parts.append("no timestamp format: " + ", ".join(
                Path(p).name for p in result.no_format))
        if result.low_confidence:
            parts.append("low confidence: " + ", ".join(
                f"{Path(p).name} {c:.0%}" for p, c in result.low_confidence))
        if result.failed:
            parts.append("failed: " + ", ".join(
                Path(p).name for p, _ in result.failed))
        if prepared is not None and prepared.indexed_bytes:
            parts.append(prepared.summary())

        # Reported in the status bar, not a modal dialog. Dropping a folder of
        # fifty logs where three are locked by another process is an ordinary
        # thing to do, and a modal that has to be dismissed before the other
        # forty-seven can be read is worse than useless. The detail goes in the
        # tooltip for whoever wants it.
        self._status_msg.setText("  |  ".join(parts))
        self._status_msg.setToolTip(
            "\n".join(f"{p}: {why}" for p, why in result.failed)
            if result.failed else ""
        )

    def clear_cache(self) -> None:
        """
        NFR-22: the saved indexes are the user's disk, so they get a way to
        reclaim it.

        Files already open are unaffected -- their arrays are mapped, and on
        Windows a mapped sidecar simply refuses to be deleted, so the count
        reported is what actually went.
        """
        root = self.session.cache_root or sidecar.cache_root()
        before, bytes_before = sidecar.usage(root)
        freed = sidecar.clear(root)
        after, _ = sidecar.usage(root)
        gone = before - after
        if not before:
            self._status_msg.setText("index cache is already empty")
            return
        kept = (f", {after} still in use" if after else "")
        self._status_msg.setText(
            f"cleared {gone} of {before} cached index(es), "
            f"{freed / 1e6:.1f} MB freed{kept}")
        self._status_msg.setToolTip(
            f"{root}\n{bytes_before / 1e6:.1f} MB before")

    def close_all(self) -> None:
        # The watchers hold a callback that reaches this window, and the states
        # are keyed by source id, so both have to go before the session does --
        # otherwise a file written to a moment later wakes a tick that looks up
        # sources that are no longer there.
        self.follow.stop()
        self.follow.states.clear()
        self.session.close()
        self.session = Session()
        self.follow.session = self.session
        self.view.session = self.session
        self.minimap.session = self.session
        self.timeline.session = self.session
        # The layers are timestamps from files that are no longer open, so they
        # would mark positions in a stream that no longer exists.
        self.marks.session = self.session
        self.marks.stop()
        self.marks.invalidate()
        self.minimap.set_marks(())
        self.timeline.set_marks(())
        for scanner in (self.finder, self.filterer):
            scanner.stop()
            scanner.session = self.session
        self.finder.invalidate()
        self.close_find()
        self.filterbar.hide()
        self.filterbar.set_sources([])
        self.timeline.set_window(None)
        self._status_filter.setText("")
        self.view.set_tailing(False)
        self._new_lines.clear()
        self.follow.set_global_follow(self.follow_act.isChecked())
        self.refresh()
        self._update_follow_status()
        self._status_msg.setText("No logs open")

    # ------------------------------------------------------------------
    # drag and drop (FR-1)
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if u.isLocalFile()
        ]
        if paths:
            self.open_paths(paths)
            event.acceptProposedAction()

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self.view.refresh()
        self.minimap.refresh()
        self.timeline.refresh()
        self._sync_strips()
        self.filterbar.set_sources([s.alias for s in self.session.sources])
        self._reload_source_list()
        self._update_span()
        self._update_position(self.view.current_row())

    def _reload_source_list(self) -> None:
        self.source_list.blockSignals(True)
        self.source_list.clear()
        for i, s in enumerate(self.session.sources):
            state = self.follow.states.get(str(s.source.id))
            mark = ""
            if state is not None and state.stale:
                mark = "  ⊘"
            elif state is not None and state.live:
                mark = "  ●"
            item = QListWidgetItem(f"{s.alias}   {s.index.n:,}{mark}")
            pix = QPixmap(SWATCH, SWATCH)
            pix.fill(QColor(s.colour))
            item.setIcon(pix)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                          | Qt.ItemFlag.ItemIsEditable)
            item.setCheckState(
                Qt.CheckState.Checked if s.meta.enabled else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, i)
            # FR-25: a stale source stays in the list, stays readable and stays
            # tickable -- it is only coloured differently and marked, because
            # the content already read is no less good for the file being gone.
            if state is not None and state.stale:
                item.setForeground(QColor(theme.STALE))
            elif state is not None and state.live:
                item.setForeground(QColor(theme.FOLLOW_ACTIVE))
            fmt = s.index.format_name or "no format"
            custom = "  (renamed)" if s.meta.alias_custom else ""
            item.setToolTip(
                f"{s.meta.origin or s.alias}\n"
                f"{s.index.n:,} entries, {fmt}, {s.index.encoding}\n"
                f"confidence {s.index.confidence:.0%}\n"
                f"{self._follow_tooltip(s, state)}\n"
                f"alias: {s.alias}{custom} -- double-click to rename, "
                f"clear it to go back to the default"
            )
            self.source_list.addItem(item)
        self.source_list.blockSignals(False)

    def _follow_tooltip(self, source, state) -> str:
        """One line in a source's tooltip saying what following is doing to it."""
        if not self.follow.global_follow:
            return "not following (View > Follow New Lines)"
        if not source.meta.follow:
            return "follow is off for this file"
        if state is None:
            return "not followed"
        if state.stale:
            return f"stale: {state.stale_reason}"
        if not state.following:
            return state.note or "not followed"
        bits = [f"following, +{state.added:,} since started"]
        if state.pending_bytes:
            bits.append(f"{state.pending_bytes} bytes of a part-written line held")
        if state.truncations:
            bits.append(f"truncated {state.truncations}x")
        return "; ".join(bits)

    def _source_menu(self, point) -> None:
        """Right-click on a source: the per-file follow toggle (FR-24)."""
        item = self.source_list.itemAt(point)
        if item is None:
            return
        pos = item.data(Qt.ItemDataRole.UserRole)
        if pos is None:
            return
        pos = int(pos)
        source = self.session.sources[pos]
        menu = QMenu(self.source_list)
        act = QAction(f"Follow “{source.alias}”", menu)
        act.setCheckable(True)
        act.setChecked(bool(source.meta.follow))
        act.setEnabled(self.follow.global_follow)
        act.setStatusTip(
            "Watch this file for new lines. The global switch is "
            "View > Follow New Lines")
        act.toggled.connect(lambda on, p=pos: self._source_follow_toggled(p, on))
        menu.addAction(act)
        menu.exec(self.source_list.mapToGlobal(point))

    def _alias_of_row(self, row: int) -> str:
        if 0 <= row < len(self.session.sources):
            return self.session.sources[row].alias
        return ""

    def rename_source(self, source: int, alias: str) -> None:
        """
        FR-29: the alias is user-editable. An empty name restores the default.

        The whole dock is rebuilt afterwards rather than the one row patched,
        because a rename can change more than the row it happened on: clearing
        one puts it back under the shortest-unique rule, which may lengthen or
        shorten every other automatic alias in the set.
        """
        if not 0 <= source < len(self.session.sources):
            return
        before = self.session.sources[source].alias
        now = self.session.set_alias(source, alias)
        if now == before:
            return
        self.refresh()
        self._status_msg.setText(
            f"{before} renamed to {now}" if alias.strip()
            else f"alias reset to {now}")

    def _source_toggled(self, item: QListWidgetItem) -> None:
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None:
            return
        self.session.set_enabled(int(idx), item.checkState() == Qt.CheckState.Checked)
        self.view.refresh()
        # A source going out of the merge changes what both strips aggregate
        # (FR-9), so this is a rebuild and not a repaint.
        self.minimap.refresh()
        self.timeline.refresh()
        # ... and the marks with them: a layer is merged timestamps, and a
        # source switched off removes some of them. `scan_key` carries the
        # enabled flags for exactly this, so `request` re-runs itself.
        self.marks.request(self.view.highlights.rules)
        self._update_span()

    def _update_span(self) -> None:
        if self.session.is_empty:
            self._status_span.setText("")
            return
        lo, hi = self.session.time_span()
        self._status_span.setText(
            f"{format_wall_us(lo, micros=False)}  to  {format_wall_us(hi, micros=False)}   "
        )

    def _update_position(self, row: int) -> None:
        total = self.session.total_rows
        if not total:
            self._status_pos.setText("")
            return
        rows = self.session.rows(row, 1)
        where = ""
        if rows:
            r = rows[0]
            where = f"   {self.session.sources[r.source].alias}:{r.line_no}"
        self._status_pos.setText(f"row {row + 1:,} of {total:,}{where}   ")

    def _report_copied(self, count: int) -> None:
        self.statusBar().showMessage(
            f"Copied {count:,} row{'s' if count != 1 else ''} with attribution", 4000
        )

    def _report_copy_refused(self, count: int) -> None:
        from .mergedview import COPY_ROW_LIMIT

        self.statusBar().showMessage(
            f"{count:,} rows is too many for the clipboard "
            f"(limit {COPY_ROW_LIMIT:,}) -- narrow the selection",
            6000,
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        """
        Stop indexing before the session goes away.

        The worker shares no object with the session, but it is still writing
        sidecars for files this window asked about, and a half-written one must
        not be left behind -- which is why it is cancelled and joined rather
        than abandoned.
        """
        self.cancel_indexing()
        self._join_thread()
        self.follow.stop()
        self.marks.stop()
        # Both scanners, and the export: a worker still reading a source's
        # bytes when `session.close()` unmaps them is a segfault, not an
        # exception.
        self.finder.stop()
        self.filterer.stop()
        self.cancel_export()
        self.session.close()
        super().closeEvent(event)
