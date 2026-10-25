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
# Freeing a reader whose worker outlived shutdown() would destroy a running
# QThread, and Qt would abort the run before the fixture's assertion reports.
_stuck_bus_readers: list[BusReader] = []


@pytest.fixture(autouse=True)
def _close_leaked_bus_readers():  # type: ignore[no-untyped-def]
    """Shut down every BusReader a test built via ``make_app_context``.

    ``BusController`` runs its RX loop on a QThread. A test that opens a
    bus but never closes it leaves that thread running; at interpreter
    shutdown Qt aborts the process ("QThread: Destroyed while thread is
    still running"), which would fail CI even though the tests passed.
    A worker still running after ``shutdown()`` fails the test here
    instead.
    """
    yield
    stuck: list[str] = []
    while _open_bus_readers:
        reader = _open_bus_readers.pop()
        names = reader.shutdown()
        if names:
            _stuck_bus_readers.append(reader)
            stuck += names
    assert stuck == []


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


def kill_capture(reader: BusReader, name: str) -> None:
    """Fail an open ``virtual`` bus as an unplugged adapter would.

    Shuts its python-can bus down from the test thread: the worker's next
    ``recv`` raises, and the real failure path runs (``error``, then
    ``bus_failed`` and ``bus_stopped``).
    """
    bus = reader._controllers[name].worker._bus
    assert bus is not None
    bus.shutdown()


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


_EDGE_DBC_SOURCE = """\
VERSION ""

NS_ :

BS_:

BU_:

BO_ 1600 ImdCtrl: 4 Vector__XXX
 SG_ Request_mux M : 0|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ Set_max_voltage m240 : 8|16@1+ (1,0) [0|1000] "V" Vector__XXX
 SG_ Restart m193 : 8|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ Excitation_off m98 : 8|8@1+ (1,0) [0|255] "" Vector__XXX

BO_ 1601 ImdStatus: 8 Vector__XXX
 SG_ OpCode M : 0|8@1+ (1,0) [0|255] "" Vector__XXX
 SG_ Temperature m128 : 8|16@1+ (0.1,-40) [0|0] "degC" Vector__XXX
 SG_ Rp m225 : 8|16@1+ (1,0) [0|65535] "kOhm" Vector__XXX
 SG_ Rp_uncertainty m225 : 24|8@1+ (1,0) [0|100] "%" Vector__XXX
 SG_ Rn m225 : 32|16@1+ (1,0) [0|65535] "kOhm" Vector__XXX
 SG_ Rn_uncertainty m225 : 48|8@1+ (1,0) [0|100] "%" Vector__XXX

BO_ 123 LogBlob: 8 Vector__XXX
 SG_ LogData : 0|64@1+ (1,0) [0|0] "" Vector__XXX

BO_ 124 WideScaled: 8 Vector__XXX
 SG_ Odometer : 0|64@1+ (10,-1000) [0|0] "m" Vector__XXX

BO_ 125 WideFractional: 8 Vector__XXX
 SG_ Energy : 0|64@1+ (0.5,0) [0|0] "Wh" Vector__XXX

VAL_ 1601 OpCode 128 "Temperature reading" ;
"""


@pytest.fixture
def edge_dbc_path(tmp_path: Path) -> Path:
    """Shapes the example DBC lacks: multiplexed messages (an IMD-style
    request/response pair), a signal with no declared range, and 64-bit
    signals unscaled, with whole-number scaling, and with a fractional factor."""
    path = tmp_path / "edge.dbc"
    path.write_text(_EDGE_DBC_SOURCE, encoding="utf-8")
    return path
