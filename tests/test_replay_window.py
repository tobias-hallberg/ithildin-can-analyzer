# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.replay_window.ReplayWindow``."""

from __future__ import annotations

import uuid
from pathlib import Path

import can

from ithildin.ui.replay_window import ReplayWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import make_app_context


def _write_log(path: Path, n: int = 3) -> None:
    writer = can.Logger(str(path))
    try:
        for i in range(n):
            writer.on_message_received(
                can.Message(
                    timestamp=1000.0 + 0.005 * i,
                    arbitration_id=0x300 + i,
                    data=bytes([i]),
                    is_extended_id=False,
                    channel="A",
                )
            )
    finally:
        writer.stop()


def test_start_without_bus_shows_error(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = ReplayWindow(instance_id="rp1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    log = tmp_path / "in.asc"
    _write_log(log)
    win._path_edit.setText(str(log))
    win._on_action_clicked()
    assert "No bus open" in win._status_lbl.text()


def test_replay_routes_through_bus_reader(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: replay drives BusReader.send_message and a sibling Bus on the
    same virtual channel sees the frames.

    python-can's virtual backend does not echo a Bus's own sends back into
    its own recv() queue, so we attach a sibling receiver to verify the
    frames actually hit the wire.
    """
    ctx = make_app_context(tmp_path)
    channel = f"rp-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)

    win = ReplayWindow(instance_id="rp1")
    qtbot.addWidget(win)
    win.bind(ctx)

    log = tmp_path / "in.asc"
    _write_log(log, n=3)
    win._path_edit.setText(str(log))
    win._speed_combo.setCurrentIndex(_speed_index_for(win, 100.0))

    receiver = can.Bus(interface="virtual", channel=channel)
    received: list[int] = []
    try:
        win._on_action_clicked()
        assert win._controller is not None
        qtbot.waitUntil(lambda: win._controller is None, timeout=5000)
        assert "Done" in win._status_lbl.text()
        # Drain receiver.
        while True:
            msg = receiver.recv(timeout=0.1)
            if msg is None:
                break
            received.append(msg.arbitration_id)
    finally:
        receiver.shutdown()
    assert received == [0x300, 0x301, 0x302]
    ctx.bus_reader.close()


def _speed_index_for(win: ReplayWindow, factor: float) -> int:
    for i in range(win._speed_combo.count()):
        if abs(float(win._speed_combo.itemData(i)) - factor) < 1e-6:
            return i
    return 1


def test_quit_stops_replay(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Child views get no closeEvent on quit; aboutToQuit stops the replay."""
    ctx = make_app_context(tmp_path)
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"rp-{uuid.uuid4().hex[:8]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    win = ReplayWindow(instance_id="rp1")
    qtbot.addWidget(win)
    win.bind(ctx)
    log = tmp_path / "in.asc"
    writer = can.Logger(str(log))
    try:
        for i in range(50):
            writer.on_message_received(
                can.Message(timestamp=1000.0 + 0.1 * i, arbitration_id=0x300, channel="A")
            )
    finally:
        writer.stop()
    win._path_edit.setText(str(log))
    win._speed_combo.setCurrentIndex(_speed_index_for(win, 1.0))
    win._on_action_clicked()
    controller = win._controller
    assert controller is not None
    qtbot.waitUntil(lambda: win._status_lbl.text().startswith("Playing"), timeout=2000)

    qapp.aboutToQuit.emit()

    assert controller._thread.isFinished()
