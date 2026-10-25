# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Main window — single QMainWindow + QMdiArea inside.

Layout:

    Title bar:  Ithildin · <project>.itp
    Toolbar:    ☰ hamburger + Open + Save + Connect bus
                + view launchers (Trace / Plot / Monitor / Data view)
                + MCP allowlist + Settings + pane toggles
    [left dock]  |  QMdiArea center
                 |  Window-tray rail at the bottom of the canvas
    Status bar:  bus state · MCP state

The native menubar is hidden; the hamburger menu on the toolbar
consolidates File / Window / About / Quit. Edit / View / Tools /
Capture / MCP top-level menus were placeholders and have been removed.

This module owns the shell. Sub-modules host the heavy lifting:
``LeftDockWidget`` (search + 4 accordion sections), ``ViewLauncherButton``
(launcher dropdowns + drop targets), ``WindowTrayWidget`` (bottom rail).
Settings / MCP / About are ``QDialog``s opened from toolbar actions,
not MDI subwindows. Bus declaration lives in the left dock's Buses
``+``; the toolbar Connect action is a stateful toggle that opens /
closes every declared bus in one click.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMdiArea,
    QMdiSubWindow,
    QMenu,
    QMessageBox,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ithildin.app_state.project import MigrationError, Project, new_unsaved, open_or_migrate
from ithildin.app_state.recent_projects import RecentProjectsStore
from ithildin.ui.about_window import AboutDialog
from ithildin.ui.icons import icon, themed_callback, themed_icon
from ithildin.ui.left_dock import LeftDockWidget
from ithildin.ui.mcp_panel import McpDialog
from ithildin.ui.open_project_button import OpenProjectButton
from ithildin.ui.report_dock import ReportDockWidget
from ithildin.ui.settings_window import SettingsDialog
from ithildin.ui.theme import ThemeManager, ThemeName
from ithildin.ui.view_launcher import ViewLauncherButton
from ithildin.ui.window_tray import WindowTrayWidget
from ithildin.workspace.schema import BusConfig, DbcRef, McpConfig, ViewState, Workspace

if TYPE_CHECKING:
    from ithildin.app_state.bus_reader import BusReader
    from ithildin.app_state.context import AppContext
    from ithildin.app_state.database import Database
    from ithildin.app_state.diagnostics import ReportRelay

from ithildin.ui.window_manager import WindowManager

_DEFAULT_SIZE = (1280, 800)

_REPORT_LOG = logging.getLogger("ithildin.report")


class MainWindow(QMainWindow):
    """The single application window. Everything else is content inside."""

    def __init__(
        self,
        window_manager: WindowManager | None = None,
        bus_reader: BusReader | None = None,
        database: Database | None = None,
        prompt_on_close: bool = True,
    ) -> None:
        super().__init__()
        self._wm = window_manager
        self._bus = bus_reader
        self._db = database
        # When False, ``closeEvent`` skips the unsaved-changes prompt,
        # closing cleanly without dialogs. Set via the ``--no-prompt``
        # CLI flag — primarily for the assistant's manual test runs
        # where blocking dialogs stall the agent.
        self._prompt_on_close = prompt_on_close

        self.setWindowTitle("Ithildin")
        self.resize(*_DEFAULT_SIZE)

        self._dialogs: dict[str, QWidget] = {}
        self._ctx: AppContext | None = None
        self._recent: RecentProjectsStore | None = None
        # Always carry a Project — replaced by ``bind_events`` with the
        # one threaded through ``AppContext``. The placeholder lets tests
        # that skip ``bind_events`` still see a non-None project.
        self._current_project: Project = new_unsaved()
        # Last "saved" workspace shape, used to detect unsaved changes
        # at close. Refreshed on save / open / new. The dict form is a
        # canonical comparison key (model_dump on the same Pydantic
        # model is order-stable).
        self._workspace_baseline: dict[str, object] | None = None

        # Give the bottom-left corner to the left dock so the project pane
        # keeps full height and the report pane only spans the central
        # column.
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)

        self._build_hamburger_menu()
        self._build_toolbar()
        self._build_left_dock()
        self._build_central()
        self._build_report_dock()
        self._build_status_bar()
        self._wire_app_state()
        # Theme-reactive bits depend on docks + MDI being built. Register
        # last so the initial callback invocations see a fully-constructed
        # window.
        themed_callback(self._refresh_pane_toggle_icons)
        themed_callback(self._refresh_mdi_background)
        themed_callback(self._refresh_connect_action)

    @property
    def mdi_area(self) -> QMdiArea:
        return self._mdi

    def set_window_manager(self, wm: WindowManager) -> None:
        """Wire a ``WindowManager`` after construction.

        The MDI area is owned by ``MainWindow``, so the ``WindowManager``
        is built second; this is the seam. We also build the launcher
        toolbar buttons (which need ``wm`` to populate their dropdowns)
        and bind the window tray here.
        """
        self._wm = wm
        self._install_view_launchers(wm)
        self._window_tray.bind(wm)
        wm.subwindow_maximized.connect(self._on_subwindow_maximized)
        wm.subwindow_restored.connect(self._on_subwindow_restored)

    def set_recent_store(self, store: RecentProjectsStore) -> None:
        """Wire the recent-projects store. Seeds the toolbar Open dropdown."""
        self._recent = store
        self._open_btn.set_recent(store.paths())

    def _install_view_launchers(self, wm: WindowManager) -> None:
        """Insert ``ViewLauncherButton`` widgets between Open and Connect."""
        for kind, label, icon_name in self._launcher_buttons:
            btn = ViewLauncherButton(kind, label, wm, parent=self._toolbar, icon_name=icon_name)
            self._launcher_widgets.append(btn)
            self._toolbar.insertWidget(self._capture_sep, btn)

    # ---- Build ----

    def _build_hamburger_menu(self) -> None:
        self.menuBar().hide()

        menu = QMenu(self)
        new_action = QAction("New project…", self)
        new_action.triggered.connect(self._new_project)
        themed_icon("new", new_action.setIcon)
        menu.addAction(new_action)
        open_action = QAction("Open project…", self)
        open_action.triggered.connect(self._open_project)
        themed_icon("open", open_action.setIcon)
        menu.addAction(open_action)
        save_action = QAction("Save project", self)
        save_action.triggered.connect(self._save_project)
        themed_icon("save", save_action.setIcon)
        menu.addAction(save_action)
        save_as_action = QAction("Save project as…", self)
        save_as_action.triggered.connect(self._save_project_as)
        themed_icon("save", save_as_action.setIcon)
        menu.addAction(save_as_action)
        menu.addSeparator()
        self._build_theme_submenu(menu)
        menu.addSeparator()
        about_action = QAction("About Ithildin…", self)
        about_action.triggered.connect(self._open_about_dialog)
        menu.addAction(about_action)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

        self._hamburger_menu = menu

    def _build_theme_submenu(self, parent_menu: QMenu) -> None:
        theme_menu = QMenu("Theme", parent_menu)
        group = QActionGroup(self)
        group.setExclusive(True)
        self._theme_actions: dict[ThemeName, QAction] = {}
        entries: tuple[tuple[ThemeName, str], ...] = (
            ("system", "System"),
            ("light", "Light"),
            ("dark", "Dark"),
        )
        for choice, label in entries:
            act = QAction(label, group)
            act.setCheckable(True)
            act.triggered.connect(lambda _checked=False, c=choice: ThemeManager.instance().apply(c))
            theme_menu.addAction(act)
            self._theme_actions[choice] = act
        parent_menu.addMenu(theme_menu)
        self._sync_theme_actions()
        # Bound method (not a lambda) so the connection auto-drops when the
        # window is destroyed — otherwise the app-lifetime ThemeManager keeps
        # firing into deleted QActions.
        ThemeManager.instance().theme_changed.connect(self._sync_theme_actions)

    def _sync_theme_actions(self, _tokens: dict[str, str] | None = None) -> None:
        current = ThemeManager.instance().current_choice()
        for choice, act in self._theme_actions.items():
            act.setChecked(choice == current)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Views & Capture", self)
        tb.setMovable(False)
        tb.setIconSize(QSize(24, 24))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)

        self._hamburger_btn = QToolButton(tb)
        themed_icon("menu", self._hamburger_btn.setIcon)
        self._hamburger_btn.setText("Menu")
        self._hamburger_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self._hamburger_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._hamburger_btn.setMenu(self._hamburger_menu)
        self._hamburger_btn.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        tb.addWidget(self._hamburger_btn)

        self._open_btn = OpenProjectButton(parent=tb)
        self._open_btn.open_requested.connect(self._open_project)
        self._open_btn.recent_chosen.connect(self._open_project_path)
        tb.addWidget(self._open_btn)

        self._save_btn = QToolButton(tb)
        self._save_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        save_default = QAction("Save", self)
        save_default.triggered.connect(self._save_project)
        themed_icon("save", save_default.setIcon)
        self._save_btn.setDefaultAction(save_default)
        save_menu = QMenu(self._save_btn)
        save_as_action = QAction("Save project as…", self._save_btn)
        save_as_action.triggered.connect(self._save_project_as)
        themed_icon("save", save_as_action.setIcon)
        save_menu.addAction(save_as_action)
        self._save_btn.setMenu(save_menu)
        self._save_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        tb.addWidget(self._save_btn)

        self._connect_action = QAction("Connect", self)
        self._connect_action.triggered.connect(self._on_connect_toggled)
        self._connect_action.setIcon(icon("plug"))
        tb.addAction(self._connect_action)
        connect_btn = tb.widgetForAction(self._connect_action)
        if connect_btn is not None:
            self._reserve_action_button_width(
                connect_btn, self._connect_action, ("Connect", "Disconnect")
            )

        tb.addSeparator()

        # Real view-launcher buttons attach via set_window_manager(),
        # which inserts them before ``self._capture_sep``.
        self._launcher_buttons: list[tuple[str, str, str]] = [
            ("trace", "Trace", "message_trace"),
            ("signal_plot", "Plot", "plot"),
            ("signal_monitor", "Monitor", "monitor"),
            ("data_view", "Data view", "data_view"),
            ("transmit", "Transmit", "transmit"),
            ("record", "Record", "record"),
        ]
        self._launcher_widgets: list[ViewLauncherButton] = []

        self._capture_sep = tb.addSeparator()

        mcp_action = QAction("MCP", self)
        mcp_action.triggered.connect(self._open_mcp_dialog)
        mcp_action.setToolTip("MCP server + allowlist")
        themed_icon("shield", mcp_action.setIcon)
        tb.addAction(mcp_action)

        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self._open_settings_dialog)
        themed_icon("settings", settings_action.setIcon)
        tb.addAction(settings_action)

        spacer = QWidget(tb)
        policy = spacer.sizePolicy()
        spacer.setSizePolicy(policy.Policy.Expanding, policy.Policy.Preferred)
        tb.addWidget(spacer)

        self._toggle_left_btn = QToolButton(tb)
        self._toggle_left_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._toggle_left_btn.setToolTip("Show/hide project pane")
        self._toggle_left_btn.clicked.connect(self._on_toggle_left_pane)
        tb.addWidget(self._toggle_left_btn)

        self._toggle_bottom_btn = QToolButton(tb)
        self._toggle_bottom_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._toggle_bottom_btn.setToolTip("Show/hide report pane")
        self._toggle_bottom_btn.clicked.connect(self._on_toggle_bottom_pane)
        tb.addWidget(self._toggle_bottom_btn)

        # When a subwindow is maximized its native title bar collapses to
        # 0 px; the restore/close controls live on the active view's own
        # header bar (see ViewHeaderBar / WindowManager). MainWindow only
        # mirrors the maximized window's title here.
        self._maximized_sub: QMdiSubWindow | None = None

        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self._toolbar = tb

    def _reserve_action_button_width(
        self,
        button: QWidget,
        action: QAction,
        texts: tuple[str, ...],
    ) -> None:
        # Toggling an action's text (Connect ↔ Disconnect) resizes its
        # toolbar button and shifts every neighbour. Pre-measure the widest
        # variant and pin that as the minimum so the toolbar stays put.
        original = action.text()
        widest = 0
        for text in texts:
            action.setText(text)
            widest = max(widest, button.sizeHint().width())
        action.setText(original)
        button.setMinimumWidth(widest)

    def _build_left_dock(self) -> None:
        dock = QDockWidget("Project", self)
        dock.setObjectName("project_dock")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        # Replace the default "Project" title bar with an empty widget.
        # The caption was visual noise — every dock-aware control already
        # tells the user this is the project pane. Loses the drag handle,
        # but this is a single-window MDI shell where the dock isn't
        # expected to move.
        dock.setTitleBarWidget(QWidget(dock))
        self._left_dock_widget = LeftDockWidget(dock)
        dock.setWidget(self._left_dock_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self._left_dock = dock

    def _on_toggle_left_pane(self) -> None:
        self._left_dock.setVisible(not self._left_dock.isVisible())
        self._refresh_pane_toggle_icons()

    def _on_toggle_bottom_pane(self) -> None:
        self._report_dock.setVisible(not self._report_dock.isVisible())

    def _on_subwindow_maximized(self, sub: QMdiSubWindow) -> None:
        self._maximized_sub = sub
        self.setWindowTitle(f"{sub.windowTitle()} — Ithildin")
        sub.windowTitleChanged.connect(self._on_maximized_title_changed)

    def _on_subwindow_restored(self) -> None:
        prev = self._maximized_sub
        self._maximized_sub = None
        self.setWindowTitle("Ithildin")
        if prev is not None:
            try:
                prev.windowTitleChanged.disconnect(self._on_maximized_title_changed)
            except (RuntimeError, TypeError):
                pass

    def _on_maximized_title_changed(self, title: str) -> None:
        if self._maximized_sub is not None:
            self.setWindowTitle(f"{title} — Ithildin")

    def _on_report_dock_visibility(self, _visible: bool) -> None:
        self._refresh_pane_toggle_icons()

    def _refresh_pane_toggle_icons(self) -> None:
        # ``isVisible`` is False until MainWindow itself is shown; use
        # ``isHidden`` to capture intended visibility regardless of
        # parent show state.
        left_visible = not self._left_dock.isHidden()
        bottom_visible = not self._report_dock.isHidden()
        self._toggle_left_btn.setIcon(icon("pane_left_on" if left_visible else "pane_left_off"))
        self._toggle_bottom_btn.setIcon(
            icon("pane_bottom_on" if bottom_visible else "pane_bottom_off")
        )

    def _refresh_mdi_background(self) -> None:
        """Re-apply the MDI brush from the current palette.

        ``QMdiArea.setBackground`` copies the brush, so an old colour
        sticks after a theme switch unless we re-set it.
        """
        self._mdi.setBackground(self.palette().window())

    def _build_central(self) -> None:
        # MDI canvas above a window-tray rail.
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._mdi = QMdiArea(central)
        self._mdi.setViewMode(QMdiArea.ViewMode.SubWindowView)
        self._mdi.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._mdi.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Initial brush; ``_refresh_mdi_background`` re-applies it on
        # every theme switch since QBrush captures a copy of the colour
        # rather than tracking the palette role.
        self._mdi.setBackground(self.palette().window())
        outer.addWidget(self._mdi, 1)

        self._window_tray = WindowTrayWidget(central)
        outer.addWidget(self._window_tray)

        self.setCentralWidget(central)

        # GL compositor warm-up. A 1×1 ``QOpenGLWidget`` parented to the
        # central widget but positioned outside its bounds (so it never
        # paints anything the user sees). Its presence puts the main
        # window's per-window compositor into GL-aware mode from
        # startup, so the first ``GLPlotWidget`` the user opens
        # doesn't trigger a compositor switch — which manifests as
        # the whole window flashing / re-rendering.
        self._gl_warmup = QOpenGLWidget(central)
        self._gl_warmup.setFixedSize(1, 1)
        self._gl_warmup.move(-2, -2)
        self._gl_warmup.lower()
        self._gl_warmup.show()

    def _build_report_dock(self) -> None:
        """Bottom dock for cross-window event log; hidden until needed."""
        dock = QDockWidget("Report", self)
        dock.setObjectName("report_dock")
        dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._report_widget = ReportDockWidget()
        dock.setWidget(self._report_widget)
        dock.setVisible(False)
        # Keep the toolbar button's icon in sync if the dock is shown via
        # ``report()`` or hidden by the user clicking the dock's close box.
        dock.visibilityChanged.connect(self._on_report_dock_visibility)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self._report_dock = dock

    def report(self, message: str, *, kind: str = "info") -> None:
        """Append to the Report dock; auto-reveal only for warn/error.

        Public so callers (Trace banner, McpDialog) can write into the
        same shared log surface. Routine info messages append silently
        so the dock doesn't pop up on every successful operation. Every
        entry also lands in the log file.
        """
        # Formatted up front: each distinct message keeps its own rate-limit
        # key in the log.
        _REPORT_LOG.info(f"[{kind}] {message}")
        self._report_widget.append(message, kind=kind)  # type: ignore[arg-type]
        if kind in ("warn", "error"):
            self._report_dock.setVisible(True)

    def bind_log_relay(self, relay: ReportRelay) -> None:
        """Show the relay's mirrored log records in the Report dock.

        Queued explicitly: records are logged on capture, cyclic, MCP and
        Qt threads, and the dock must only be touched on the GUI thread.
        """
        relay.message.connect(self._on_log_message, Qt.ConnectionType.QueuedConnection)

    @Slot(str)
    def _on_log_message(self, message: str) -> None:
        # Not via report(): the record is already in the log file.
        self._report_widget.append(message, kind="error")
        self._report_dock.setVisible(True)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self._status_left = QLabel("Disconnected")
        self._status_right = QLabel("MCP off")
        self._status_right.setToolTip("MCP server state — open the MCP dialog to start it")
        bar.addWidget(self._status_left, 1)
        bar.addPermanentWidget(self._status_right)

    def _wire_app_state(self) -> None:
        if self._bus is not None:
            self._bus.bus_started.connect(lambda name: self._status_left.setText(f"{name} · LIVE"))
            self._bus.bus_stopped.connect(
                lambda name: self._status_left.setText(f"{name} · stopped")
            )
            self._bus.bus_started.connect(lambda _name: self._refresh_connect_action())
            self._bus.bus_stopped.connect(lambda _name: self._refresh_connect_action())
            self._bus.error.connect(lambda msg: self.report(msg, kind="error"))
            # Bound slots, so the off-GUI-thread send_failed is queued here.
            self._bus.send_failed.connect(self._on_send_failed)
            self._bus.cyclic_failed.connect(self._on_cyclic_failed)
        if self._db is not None:
            self._db.error.connect(lambda msg: self.report(msg, kind="error"))
        self._refresh_connect_action()

    @Slot(str, int, str)
    def _on_send_failed(self, bus_name: str, message_id: int, error: str) -> None:
        self.report(f"TX 0x{message_id:X} on {bus_name} failed: {error}", kind="error")

    @Slot(str, int, str)
    def _on_cyclic_failed(self, bus_name: str, message_id: int, error: str) -> None:
        self.report(f"Periodic TX 0x{message_id:X} on {bus_name} stopped: {error}", kind="error")

    @Slot(str, str)
    def _on_report_requested(self, message: str, kind: str) -> None:
        self.report(message, kind=kind)

    def bind_events(self, ctx: AppContext) -> None:
        """Subscribe to UI-event hub events + bind the left dock.

        The left dock binds to BusReader / Database here too. The
        WindowManager exists by now and the AppContext / factory chain
        is wired, so views can bind on construction inside
        ``restore_from_workspace``.
        """
        self._ctx = ctx
        self._current_project = ctx.project
        ctx.events.buses_changed.connect(self._refresh_connect_action)
        ctx.events.report_requested.connect(self._on_report_requested)
        bridge = ctx.mcp_bridge
        bridge.started.connect(self._on_mcp_started)
        bridge.stopped.connect(self._on_mcp_stopped)
        bridge.error.connect(lambda msg: self.report(msg, kind="error"))
        if bridge.is_running:
            self._on_mcp_started(f"http://{ctx.mcp_host}:{ctx.mcp_port}")
        self._left_dock_widget.bind(ctx)
        self._adopt_project_state(ctx.project)
        self._refresh_connect_action()
        self._capture_baseline()

    def _adopt_project_state(self, project: Project) -> None:
        """Apply a project's persisted view list to the running shell.

        Used both at launch (from ``bind_events``) and from ``_open_project``.
        Geometry restore (``restore_from_disk``) runs separately and matches
        windows by ``instance_id``, so this must run first.
        """
        if self._wm is not None and project.workspace.views:
            self._wm.restore_from_workspace(project.workspace.views)

    def start_background_probes(self) -> None:
        """Kick off background work that should run after first paint.

        Currently: the left-dock adapter prober (vendor python-can
        backend imports happen on a worker thread, not the GUI thread).
        Called from ``app.main`` immediately after ``show()``.
        """
        self._left_dock_widget.start_adapter_probe()

    # ---- Slots ----

    def _open_view(self, kind: str) -> None:
        if self._wm is None:
            return
        self._wm.open(kind)

    def _show_singleton_dialog(self, key: str, dialog_cls: type[QWidget]) -> QWidget:
        """Show or focus a singleton dialog tied to the main window."""
        existing = self._dialogs.get(key)
        if existing is not None and not existing.isHidden():
            existing.raise_()
            existing.activateWindow()
            return existing
        dlg: QWidget = dialog_cls(self)
        self._dialogs[key] = dlg
        dlg.destroyed.connect(lambda _obj=None: self._dialogs.pop(key, None))
        dlg.show()
        return dlg

    def _on_connect_toggled(self) -> None:
        """Toolbar Connect/Disconnect: bring up every declared bus or close all open ones."""
        if self._bus is None:
            return
        if self._bus.is_open:
            self._bus.close()
            return
        configs = list(self._current_project.workspace.bus)
        if not configs:
            return  # button should be disabled in this state
        self._bus.open_all(configs)

    def _refresh_connect_action(self) -> None:
        """Sync the Connect action's label / icon / enabled state with bus + workspace state."""
        bus_open = self._bus is not None and self._bus.is_open
        has_buses = bool(self._current_project.workspace.bus)
        self._connect_action.setText("Disconnect" if bus_open else "Connect")
        self._connect_action.setIcon(icon("unplug" if bus_open else "plug"))
        self._connect_action.setEnabled(bus_open or has_buses)
        if not bus_open and not has_buses:
            self._connect_action.setToolTip("Add a bus first (click + on Buses)")
        else:
            self._connect_action.setToolTip("")

    def _open_settings_dialog(self) -> None:
        dlg = self._show_singleton_dialog("settings", SettingsDialog)
        if isinstance(dlg, SettingsDialog) and self._ctx is not None:
            dlg.bind_app(self._ctx)

    def _on_mcp_started(self, url: str) -> None:
        # Port is the useful half — the host is always loopback.
        port = url.rsplit(":", 1)[-1].split("/", 1)[0]
        self._status_right.setText(f"MCP on · :{port}")
        self._status_right.setToolTip(f"MCP server listening at {url}")
        self.report(f"MCP server listening at {url}", kind="info")

    def _on_mcp_stopped(self) -> None:
        self._status_right.setText("MCP off")
        self._status_right.setToolTip("MCP server state — open the MCP dialog to start it")

    def _open_mcp_dialog(self) -> None:
        dlg = self._show_singleton_dialog("mcp", McpDialog)
        if isinstance(dlg, McpDialog) and self._ctx is not None:
            dlg.bind_app(self._ctx)

    def _open_about_dialog(self) -> None:
        self._show_singleton_dialog("about", AboutDialog)

    def _save_project(self) -> None:
        if not self._current_project.is_saved:
            if not self._rebind_to_new_path():
                return
        self._write_workspace_to_disk()

    def _save_project_as(self) -> None:
        if not self._rebind_to_new_path():
            return
        self._write_workspace_to_disk()

    def _rebind_to_new_path(self) -> bool:
        """Prompt for an .itp path and rebind the current project to it.

        Returns False if the user cancelled. Also re-targets the
        WindowManager's ``layout.json`` so geometry persistence follows
        the new project's per-user directory.
        """
        project = self._current_project
        path = self._prompt_save_path(default_stem=project.workspace.name or "Untitled")
        if path is None:
            return False
        project.set_path(path)
        project.ensure_user_dir()
        if self._wm is not None:
            self._wm.set_layout_path(project.layout_path)
        self._after_project_bound(path)
        return True

    def _write_workspace_to_disk(self) -> None:
        # Always rebuild the workspace from the live UI state. Saving the
        # in-memory workspace as-loaded would silently drop session-level
        # changes (DBCs added/removed, MCP tweaks, view drops, bus
        # reconnects).
        project = self._current_project
        ws = self._snapshot_workspace(name=project.workspace.name)
        project.set_workspace(ws)
        try:
            project.save()
        except (OSError, ValueError) as exc:
            self.report(f"Save failed: {exc}", kind="error")
            return
        self._workspace_baseline = ws.model_dump(mode="json")

    def _prompt_save_path(self, *, default_stem: str) -> Path | None:
        default = str(Path.home() / f"{default_stem}.itp")
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save project", default, "Ithildin project (*.itp)"
        )
        if not path_str:
            return None
        p = Path(path_str)
        return p if p.suffix == ".itp" else p.with_suffix(".itp")

    def _snapshot_workspace(self, *, name: str) -> Workspace:
        dbc_refs: list[DbcRef] = []
        if self._db is not None:
            for p in self._db.loaded_paths():
                dbc_refs.append(
                    DbcRef(
                        path=p,
                        buses=self._db.buses_for_path(p),
                        signal_centric=self._db.is_signal_centric(p),
                    )
                )
        mcp = McpConfig()
        if self._ctx is not None:
            mcp = McpConfig(host=self._ctx.mcp_host, port=self._ctx.mcp_port)
        bus = self._collect_bus_configs(dbc_refs)
        views: list[ViewState] = []
        if self._wm is not None:
            views = [ViewState(**w) for w in self._wm.snapshot_views()]
        return Workspace(name=name, bus=bus, dbc=dbc_refs, mcp=mcp, views=views)

    def _capture_baseline(self) -> None:
        """Snapshot the current workspace shape as the diff target for close."""
        ws = self._snapshot_workspace(name=self._current_project.workspace.name)
        self._workspace_baseline = ws.model_dump(mode="json")

    def _workspace_is_dirty(self) -> bool:
        """True iff the live workspace differs from the last captured baseline."""
        if self._workspace_baseline is None:
            return False
        current = self._snapshot_workspace(name=self._current_project.workspace.name).model_dump(
            mode="json"
        )
        return current != self._workspace_baseline

    def _collect_bus_configs(self, _dbc_refs: list[DbcRef]) -> list[BusConfig]:
        """Buses are user-declared via the left dock's ``+``.

        ``project.workspace.bus`` is the only source of truth — the dock's
        add / remove handlers and ``_on_connect_requested`` keep it in
        sync. The snapshot just returns the current list so File → Save
        round-trips losslessly.
        """
        return list(self._current_project.workspace.bus)

    def _open_project(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open project",
            str(Path.home()),
            "Ithildin project (*.itp *.ithildin)",
        )
        if not path_str:
            return
        self._open_project_path(Path(path_str))

    def _open_project_path(self, path: Path) -> None:
        try:
            project, report = open_or_migrate(path)
        except (FileNotFoundError, ValueError, MigrationError) as exc:
            self.report(f"Open failed: {exc}", kind="error")
            return

        self._reset_runtime_state()
        # Mutate the existing Project in place so AppContext.project
        # (frozen field) and any cached references in the left dock
        # see the new workspace too. Reassigning would leave them
        # pointing at the previous instance.
        self._current_project.adopt(project)
        self._current_project.ensure_user_dir()
        if self._db is not None:
            # Legacy unbound DBCs would be invisible in the new model;
            # skip and surface via the error channel.
            for ref in self._current_project.workspace.dbc:
                if not ref.buses:
                    self._db.error.emit(f"DBC {ref.path} has no bus bindings; skipping.")
                    continue
                self._db.add_dbc(
                    ref.path,
                    list(ref.buses),
                    signal_centric=ref.signal_centric,
                )
        if self._wm is not None:
            self._wm.set_layout_path(self._current_project.layout_path)
        self._adopt_project_state(self._current_project)
        if self._wm is not None:
            self._wm.restore_from_disk()
        self._left_dock_widget.refresh_buses()
        self._refresh_connect_action()
        self._capture_baseline()

        if report is not None:
            self.report(
                f"Migrated v1 → v2: {report.legacy_path.name} "
                f"({report.rules_imported} allowlist rules)",
                kind="info",
            )
        self._after_project_bound(path)

    def _after_project_bound(self, path: Path) -> None:
        """MRU bookkeeping after the current project gets bound to ``path``."""
        if self._recent is None:
            return
        self._recent.add(path)
        self._open_btn.set_recent(self._recent.paths())

    def _new_project(self) -> None:
        self._reset_runtime_state()
        # Same in-place adopt as ``_open_project`` so consumers that
        # hold ``ctx.project`` (frozen field on AppContext) see the
        # reset workspace.
        self._current_project.adopt(new_unsaved())
        if self._wm is not None:
            self._wm.set_layout_path(self._current_project.layout_path)
        self._left_dock_widget.refresh_buses()
        self._refresh_connect_action()
        self._capture_baseline()

    def _reset_runtime_state(self) -> None:
        # Close any child views so a freshly-opened project isn't piled
        # on top of the previous session's leftovers. This is technically
        # a state-transition concern but is a hard prerequisite for clean
        # view-restore on Open Project.
        if self._wm is not None:
            for entry in list(self._wm.list_windows()):
                self._wm.close(entry.window_id)
        if self._db is not None:
            for p in list(self._db.loaded_paths()):
                self._db.remove_dbc(p)

    # ---- Lifecycle ----

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._prompt_on_close and self._workspace_is_dirty():
            reply = QMessageBox.question(
                self,
                "Save changes?",
                "The project has unsaved changes. Save before quitting?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.StandardButton.Save:
                self._save_project()
                # Save-As cancelled or save failed leaves the workspace
                # dirty. Abort close so the user doesn't lose work.
                if self._workspace_is_dirty():
                    event.ignore()
                    return
        if self._bus is not None and self._bus.is_open:
            self._bus.close()
        if self._wm is not None:
            self._wm.flush()
        self._left_dock_widget.save_state()
        super().closeEvent(event)
