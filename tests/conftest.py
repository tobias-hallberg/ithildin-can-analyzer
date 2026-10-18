# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.context import AppContext
from ithildin.app_state.database import Database
from ithildin.app_state.project import Project, new_unsaved
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.bridge import McpServerBridge
from ithildin.mcp.permissions import AllowlistStore
from ithildin.mcp.tools import McpReadContext
from ithildin.ui.ui_events import UiEvents

_open_bus_readers: list[BusReader] = []


@pytest.fixture(autouse=True)
def _close_leaked_bus_readers():  # type: ignore[no-untyped-def]
    """Close every BusReader a test built via ``make_app_context``.

    ``BusController`` runs its RX loop on a QThread. A test that opens a
    bus but never closes it leaves that thread running; at interpreter
    shutdown Qt aborts the process ("QThread: Destroyed while thread is
    still running"), which would fail CI even though the tests passed.
    ``close()`` is idempotent and a no-op when nothing is open.
    """
    yield
    while _open_bus_readers:
        reader = _open_bus_readers.pop()
        try:
            reader.close()
        except Exception:
            pass


def make_app_context(
    tmp_path: Path,
    *,
    project: Project | None = None,
) -> AppContext:
    """Build a fully-populated ``AppContext`` for tests.

    Allowlist + audit live under ``tmp_path``; the bridge is constructed
    but not started. ``project`` defaults to a fresh unsaved Project —
    matching production where the app always carries one. Pass
    ``project=`` to thread a real ``Project`` through (e.g. for tests
    that need ``ctx.project.workspace.bus`` populated).
    """
    user_dir = tmp_path / "_test_user_dir"
    user_dir.mkdir(parents=True, exist_ok=True)
    bus_reader = BusReader()
    _open_bus_readers.append(bus_reader)
    database = Database()
    events = UiEvents()
    allowlist = AllowlistStore.load(user_dir / "allowlist.toml")
    audit_path = user_dir / "audit.jsonl"
    AuditWriter(audit_path)  # touch the path
    read_ctx = McpReadContext(bus_reader=bus_reader, database=database, allowlist=allowlist)
    bridge = McpServerBridge(read_ctx)
    return AppContext(
        bus_reader=bus_reader,
        database=database,
        events=events,
        allowlist=allowlist,
        audit_path=audit_path,
        mcp_bridge=bridge,
        project=project if project is not None else new_unsaved(user_root=tmp_path),
    )


# Minimal hand-written DBC. Two messages, two signals each. Avoids depending
# on the exact cantools Signal-constructor API which has shifted across
# major versions.
_DBC_SOURCE = """\
VERSION ""

NS_ :

BS_:

BU_:

BO_ 256 EngineCommand: 8 Vector__XXX
 SG_ EngineSpeed : 0|16@1+ (0.125,0) [0|8000] "rpm" Vector__XXX
 SG_ IdleSetpoint : 16|16@1+ (1,0) [600|1200] "rpm" Vector__XXX

BO_ 512 BodyStatus: 4 Vector__XXX
 SG_ VehicleSpeed : 0|16@1+ (0.01,0) [0|350] "km/h" Vector__XXX
 SG_ DoorOpen : 16|1@1+ (1,0) [0|1] "" Vector__XXX

BO_ 768 GearState: 1 Vector__XXX
 SG_ Gear : 0|8@1+ (1,0) [0|3] "" Vector__XXX

VAL_ 768 Gear 0 "PARK" 1 "REVERSE" 2 "NEUTRAL" 3 "DRIVE" ;
"""


@pytest.fixture
def example_dbc_path(tmp_path: Path) -> Path:
    """Write a minimal DBC file on disk and return its path."""
    path = tmp_path / "example.dbc"
    path.write_text(_DBC_SOURCE, encoding="utf-8")
    return path
