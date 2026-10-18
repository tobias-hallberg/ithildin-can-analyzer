# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Application context — the bundle of singletons child windows can bind to.

Constructed once in ``app.py`` and threaded into every child window via
``ChildWindow.bind(ctx)`` so the windows never have to reach into globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ithildin.app_state.bus_reader import BusReader
    from ithildin.app_state.database import Database
    from ithildin.app_state.project import Project
    from ithildin.mcp.bridge import McpServerBridge
    from ithildin.mcp.permissions import AllowlistStore
    from ithildin.ui.ui_events import UiEvents


@dataclass(frozen=True)
class AppContext:
    bus_reader: BusReader
    database: Database
    events: UiEvents
    allowlist: AllowlistStore
    audit_path: Path
    mcp_bridge: McpServerBridge
    project: Project
    """The current project — always set. Use ``project.is_saved`` to
    distinguish a fresh in-memory session from one bound to a ``.itp``.
    Views that mutate ``workspace.dbc`` / ``workspace.bus`` write
    through ``project`` and call ``project.save()`` only when
    ``is_saved`` is ``True`` (otherwise persistence is deferred to the
    user's first File→Save)."""
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 7320
