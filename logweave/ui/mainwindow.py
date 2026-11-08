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
    QWidget,
)

from ..highlight import default_group
from ..highlight import store as rulestore
from ..highlight.builtins import USER_SET
from ..highlight.rules import Ruleset
from ..index import sidecar
from ..index.civil import format_wall_us
from ..session import Session
from ..session.follow import FollowEngine
from . import settings, theme
from .followui import FollowNotifier, NewLinesButton
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
        centre = QWidget(self)
        layout = QHBoxLayout(centre)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.minimap)
        layout.addWidget(self.timeline)

        self.minimap.rowSought.connect(self._seek_row)
        self.timeline.timeSought.connect(self._seek_time)
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
        self.follow_act.setShortcut("Ctrl+F")
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
        self.session.close()
        super().closeEvent(event)
