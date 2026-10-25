# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Application bootstrap.

Constructs the QApplication, application-level singletons (``BusReader``,
``Database``, ``UiEvents``, ``AllowlistStore``, ``AuditWriter``,
``McpServerBridge``), the single ``MainWindow`` (with its embedded
``QMdiArea``), and a ``WindowManager`` over that MDI area. Everything
else lives as ``QMdiSubWindow`` instances inside the main window.

Usage:
    ithildin                       # opens with no project loaded
    ithildin path/to/project.itp   # opens that project
    ithildin --no-prompt           # close without the save prompt
    ithildin --dev                 # expose dev-only surfaces (virtual loopback)
"""

from __future__ import annotations

import logging
import os
import sys
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QSurfaceFormat
from PySide6.QtWidgets import QApplication

from ithildin.app_state import diagnostics
from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.context import AppContext
from ithildin.app_state.database import Database
from ithildin.app_state.project import Project, new_unsaved, open_or_migrate
from ithildin.app_state.recent_projects import RecentProjectsStore
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.auth import TOKEN_FILENAME, load_or_create_token
from ithildin.mcp.bridge import McpServerBridge
from ithildin.mcp.permissions import AllowlistStore
from ithildin.mcp.tools import McpReadContext, McpWriteContext
from ithildin.ui.factory import make_factory
from ithildin.ui.fonts import ui_font
from ithildin.ui.main_window import MainWindow
from ithildin.ui.theme import ThemeManager, resolve
from ithildin.ui.ui_events import UiEvents
from ithildin.ui.window_manager import WindowManager


def main(argv: list[str] | None = None) -> int:
    log_file = diagnostics.install()
    args = list(argv) if argv is not None else list(sys.argv)
    # Strip the ``--no-prompt`` flag before handing argv to QApplication.
    # Qt would otherwise warn about an unknown option. Bool, not stored
    # in argv, so a later filter for the .itp positional still works.
    no_prompt = "--no-prompt" in args
    if no_prompt:
        args = [a for a in args if a != "--no-prompt"]

    # ``--dev`` surfaces dev-only UI (the virtual loopback adapter). It
    # sets the env var the adapter prober and Create-bus dialog read, so
    # the flag doesn't have to thread through the frozen AppContext.
    if "--dev" in args:
        args = [a for a in args if a != "--dev"]
        os.environ["ITHILDIN_DEV"] = "1"

    # Hi-DPI: ask Qt to scale by exact device pixel ratios rather than
    # rounding to the nearest integer. The icon factory's multi-size
    # pixmap cache handles per-screen DPR cleanly.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # Pre-set the OpenGL surface format the signal plot widgets need,
    # so the first ``GLPlotWidget`` creation doesn't have to renegotiate
    # the top-level QWindow's backing format. Without this, the first
    # plot triggers Qt to recreate native surfaces and the whole main
    # window flashes / re-renders. Keep in sync with the format built
    # in ``ithildin/ui/_gl_plot.py::GLPlotWidget.__init__``.
    gl_fmt = QSurfaceFormat()
    gl_fmt.setVersion(3, 3)
    gl_fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    gl_fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(gl_fmt)
    # Eagerly create a shared global GL context at app start, so the
    # first ``QOpenGLWidget`` (signal plot) doesn't pay context-init
    # cost lazily — which on Windows manifests as the whole main
    # window flashing / re-rendering. Must be set pre-QApplication.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    app = QApplication(args)
    app.setApplicationName("Ithildin")
    app.setApplicationDisplayName("Ithildin")
    app.setOrganizationName("Ithildin")
    # Stopped by the first aboutToQuit slot, so the slow joins connected
    # later never read as a GUI stall.
    watchdog = diagnostics.StallWatchdog(log_file)
    app.aboutToQuit.connect(watchdog.stop)

    _apply_default_font(app)

    # Configure pyqtgraph globals before any PlotWidget is instantiated;
    # ThemeManager.apply() does this as part of its end-to-end pass.
    theme_mgr = ThemeManager.instance()
    theme_mgr.apply(theme_mgr.load_choice(), persist=False)

    _apply_window_icon(app)
    theme_mgr.theme_changed.connect(lambda *_: _apply_window_icon(app))

    project = _load_project_arg(args) or new_unsaved()
    recent = RecentProjectsStore.load(Path.home() / ".ithildin" / "recent.json")
    if project.is_saved and project.path is not None:
        recent.add(project.path)
    bus_reader = BusReader()
    database = Database()
    events = UiEvents()

    # Load DBC bindings declared in the .itp into the runtime Database.
    # Without this, projects that ship pre-bound DBCs hand the user a
    # blank Database singleton and decoded views silently show nothing.
    # Empty for an unsaved project; loop is a no-op.
    # DBCs with no bus bindings can only exist in legacy .itp files;
    # in the current UI every DBC must live under a bus, so skip those
    # (they'd be invisible) and surface via the error channel.
    for ref in project.workspace.dbc:
        if not ref.buses:
            database.error.emit(f"DBC {ref.path} has no bus bindings; skipping.")
            continue
        database.add_dbc(ref.path, list(ref.buses), signal_centric=ref.signal_centric)

    project.ensure_user_dir()  # creates _no_project/ for unsaved sessions
    user_dir = project.user_dir

    allowlist = AllowlistStore.load(user_dir / "allowlist.toml")
    audit_path = user_dir / "audit.jsonl"
    audit = AuditWriter(audit_path)

    read_ctx = McpReadContext(
        bus_reader=bus_reader,
        database=database,
        allowlist=allowlist,
        audit_path=audit_path,
    )
    write_ctx = McpWriteContext(
        read=read_ctx,
        audit=audit,
        send=bus_reader.send_message,
        dry_run=project.workspace.mcp.dry_run,
    )
    # Bearer token for the MCP HTTP transport, persisted 0o600 under the
    # per-user project dir (never in the shareable .itp).
    mcp_token = load_or_create_token(user_dir / TOKEN_FILENAME)
    mcp_bridge = McpServerBridge(read_ctx, write_ctx=write_ctx, token=mcp_token)

    ctx = AppContext(
        bus_reader=bus_reader,
        database=database,
        events=events,
        allowlist=allowlist,
        audit_path=audit_path,
        mcp_bridge=mcp_bridge,
        project=project,
        mcp_host=project.workspace.mcp.host,
        mcp_port=project.workspace.mcp.port,
    )

    # MainWindow owns the QMdiArea; WindowManager manages its subwindows.
    main_window = MainWindow(
        bus_reader=bus_reader, database=database, prompt_on_close=not no_prompt
    )
    relay = diagnostics.ReportRelay(Path(log_file.name))
    main_window.bind_log_relay(relay)
    logging.getLogger().addHandler(relay.handler)
    window_manager = WindowManager(main_window.mdi_area, project.layout_path, make_factory(ctx))
    main_window.set_window_manager(window_manager)
    main_window.set_recent_store(recent)
    main_window.bind_events(ctx)
    main_window.show()
    main_window.start_background_probes()

    if project.is_saved:
        window_manager.restore_from_disk()

    # Bring up any bus the user marked "connect on startup". Done after
    # views are restored so they're listening when frames arrive.
    # open_all swallows per-bus failures (surfaced via BusReader.error).
    startup_buses = [b for b in project.workspace.bus if b.connect_on_startup]
    if startup_buses:
        bus_reader.open_all(startup_buses)

    # Opt-in auto-start. Bind failures surface via McpServerBridge.error,
    # which MainWindow routes into the report dock.
    if project.workspace.mcp.enabled:
        mcp_bridge.start(host=ctx.mcp_host, port=ctx.mcp_port)

    app.aboutToQuit.connect(window_manager.flush)
    app.aboutToQuit.connect(lambda: mcp_bridge.stop())
    app.aboutToQuit.connect(audit.close)
    # Armed once the loop runs: synchronous startup (layout restore,
    # open_all) is not a stall.
    QTimer.singleShot(0, watchdog.start)
    rc = app.exec()
    logging.getLogger().removeHandler(relay.handler)
    stuck = bus_reader.shutdown()
    if stuck:
        # Destroying a running QThread aborts the process and joining a hung
        # vendor call never returns, so leave without tearing anything down.
        # Layout, audit and settings are already flushed by aboutToQuit.
        logging.getLogger(__name__).error(
            "Bus worker(s) %s still running at exit; forcing exit", stuck
        )
        logging.shutdown()
        os._exit(rc)
    return rc


def _apply_window_icon(app: QApplication) -> None:
    """Set the app/window icon from the bundled logo, theme-aware.

    Picks `logo_light.png` when the resolved theme is `"light"` and
    `logo_dark.png` otherwise. Loaded via `importlib.resources` so it
    works both in-tree and inside a frozen PyInstaller bundle. When
    the file is absent Qt's default icon stands in.
    """
    resolved = resolve(ThemeManager.instance().current_choice())
    name = "logo_light.png" if resolved == "light" else "logo_dark.png"
    try:
        path = files("ithildin.resources").joinpath(f"logos/{name}")
        if path.is_file():
            app.setWindowIcon(QIcon(str(path)))
    except (FileNotFoundError, ModuleNotFoundError):
        pass


def _apply_default_font(app: QApplication) -> None:
    """Set the default UI font with a platform-aware fallback chain.

    Inter is preferred (handoff §"Type"); on systems without it Qt walks
    the chain to the platform default (Segoe UI on Windows, system-ui on
    Linux). Tabular cells opt into ``ithildin.ui.fonts.tabular_font()``
    per delegate.
    """
    app.setFont(ui_font())


def _load_project_arg(argv: list[str]) -> Project | None:
    """Pick up an optional ``.itp`` / ``.ithildin`` path from argv."""
    candidates = [a for a in argv[1:] if a.endswith((".itp", ".ithildin"))]
    if not candidates:
        return None
    path = Path(candidates[0])
    try:
        project, _report = open_or_migrate(path)
    except (FileNotFoundError, ValueError):
        return None
    return project


if __name__ == "__main__":
    sys.exit(main())
