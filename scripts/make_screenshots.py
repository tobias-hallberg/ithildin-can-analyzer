# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Generate the README screenshots from the bundled demo data.

Runs the real capture pipeline headlessly: opens a ``virtual`` CAN bus,
binds ``can_database.dbc``, replays a slice of ``examples/drive_5min.blf``
onto the wire, lets the views decode + populate, then grabs each view to
``docs/img/*.png``.

Run offscreen so no windows pop:

    QT_QPA_PLATFORM=offscreen uv run python scripts/make_screenshots.py

Note: the Signal Plot uses a QOpenGLWidget, which the offscreen platform
cannot always render — its grab may come out blank. The raster views
(Trace, Signal Monitor, Data view, Frame Inspector) grab cleanly.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def _ensure_offscreen_fonts() -> None:
    """The offscreen platform ships no fonts, so text renders as tofu boxes.
    Point Qt at the OS font directory when running headless and none is set."""
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return
    if os.environ.get("QT_QPA_FONTDIR"):
        return
    for candidate in (r"C:\Windows\Fonts", "/usr/share/fonts", "/Library/Fonts"):
        if Path(candidate).is_dir():
            os.environ["QT_QPA_FONTDIR"] = candidate
            return


_ensure_offscreen_fonts()

import can  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from ithildin.app_state.bus_reader import BusReader  # noqa: E402
from ithildin.app_state.context import AppContext  # noqa: E402
from ithildin.app_state.database import Database  # noqa: E402
from ithildin.app_state.project import new_unsaved  # noqa: E402
from ithildin.mcp.audit import AuditWriter  # noqa: E402
from ithildin.mcp.bridge import McpServerBridge  # noqa: E402
from ithildin.mcp.permissions import AllowlistStore  # noqa: E402
from ithildin.mcp.tools import McpReadContext  # noqa: E402
from ithildin.ui._drag_payload import BusPayload  # noqa: E402
from ithildin.ui.signal_monitor import SignalMonitorWindow  # noqa: E402
from ithildin.ui.theme import ThemeManager  # noqa: E402
from ithildin.ui.trace_window import TraceWindow  # noqa: E402
from ithildin.ui.ui_events import UiEvents  # noqa: E402
from ithildin.workspace.schema import BusConfig  # noqa: E402

_DBC = _ROOT / "can_database.dbc"
_BLF = _ROOT / "examples" / "drive_5min.blf"
_OUT = _ROOT / "docs" / "img"
_CHANNEL = "ithildin_screenshots"
_MONITOR_SIGNALS = [
    "Vehicle_Control.VehicleSpeed",
    "Motor_Status.MotorRPM",
    "Motor_Status.MotorTorque",
    "Battery_Status.PackVoltage",
    "Battery_Status.SoC",
    "Battery_Status.PackCurrent",
]


def _pump(app: QApplication, seconds: float) -> None:
    """Spin the Qt event loop for a wall-clock window (drives the 60 Hz fan-out)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _replay_slice(channel: str, limit: int) -> None:
    """Send the first ``limit`` frames of the demo BLF onto ``channel``, fast."""
    sender = can.Bus(interface="virtual", channel=channel)
    try:
        with can.BLFReader(str(_BLF)) as reader:
            for i, msg in enumerate(reader):
                if i >= limit:
                    break
                sender.send(
                    can.Message(
                        arbitration_id=msg.arbitration_id,
                        data=msg.data,
                        is_extended_id=msg.is_extended_id,
                    )
                )
    finally:
        sender.shutdown()


def _make_ctx() -> AppContext:
    import tempfile

    user_dir = Path(tempfile.mkdtemp(prefix="ithildin-shots-"))
    bus_reader = BusReader()
    database = Database()
    database.add_dbc(_DBC, ["CAN_1"])
    allowlist = AllowlistStore(user_dir / "allowlist.toml")
    AuditWriter(user_dir / "audit.jsonl")  # touch the path
    read_ctx = McpReadContext(bus_reader=bus_reader, database=database, allowlist=allowlist)
    return AppContext(
        bus_reader=bus_reader,
        database=database,
        events=UiEvents(),
        allowlist=allowlist,
        audit_path=user_dir / "audit.jsonl",
        mcp_bridge=McpServerBridge(read_ctx),
        project=new_unsaved(user_root=user_dir),
    )


def main() -> int:
    app = QApplication(sys.argv)
    ThemeManager.instance().apply("dark", persist=False)
    app.setFont(app.font())

    _OUT.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx()
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=_CHANNEL, bitrate=500000)

    monitor = SignalMonitorWindow(instance_id="shot-monitor")
    monitor.bind(ctx)
    for path in _MONITOR_SIGNALS:
        monitor.add_signal(path)
    monitor.resize(520, 460)

    trace = TraceWindow(instance_id="shot-trace")
    trace.bind(ctx)
    trace.resize(760, 460)

    ctx.bus_reader.open(cfg)
    _pump(app, 0.3)
    if trace._tracked_pane is not None:
        trace._tracked_pane._on_payload_dropped(BusPayload(bus_name="CAN_1"))

    _replay_slice(_CHANNEL, limit=4000)
    _pump(app, 1.5)

    monitor.grab().save(str(_OUT / "signal_monitor.png"))
    trace.grab().save(str(_OUT / "trace.png"))
    print(f"Wrote screenshots to {_OUT}")

    ctx.bus_reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
