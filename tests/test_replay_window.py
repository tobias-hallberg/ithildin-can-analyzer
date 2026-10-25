# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.replay_window.ReplayWindow``."""

from __future__ import annotations

import uuid
from pathlib import Path

import can

from ithildin.ui.replay_window import ReplayWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import kill_capture, make_app_context


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


# ---- Target bus loss and failures ----


def _replaying_slow_log(qtbot, tmp_path: Path):  # type: ignore[no-untyped-def]
    """A replay at 1x of frames 1 s apart onto open bus CAN_1, its first frame sent."""
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
        for i in range(10):
            writer.on_message_received(
                can.Message(timestamp=1000.0 + i, arbitration_id=0x300, channel="A")
            )
    finally:
        writer.stop()
    win._path_edit.setText(str(log))
    win._speed_combo.setCurrentIndex(_speed_index_for(win, 1.0))
    ring = ctx.bus_reader.ring
    assert ring is not None
    win._on_action_clicked()
    qtbot.waitUntil(lambda: ring.total_seq >= 1, timeout=2000)
    reports: list[tuple[str, str]] = []
    ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))
    return ctx, win, reports


def test_replay_stops_when_target_bus_fails(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, reports = _replaying_slow_log(qtbot, tmp_path)

    kill_capture(ctx.bus_reader, "CAN_1")
    qtbot.waitUntil(lambda: win._controller is None, timeout=5000)
    status = win._status_lbl.text()
    assert status.startswith("Stopped · 1 frames sent — bus CAN_1 lost:")
    assert [kind for _msg, kind in reports] == ["warn"]


def test_replay_stops_when_target_bus_degrades(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, reports = _replaying_slow_log(qtbot, tmp_path)

    ctx.bus_reader.bus_health_changed.emit("CAN_1", "errors", "no ACK")
    qtbot.waitUntil(lambda: win._controller is None, timeout=5000)
    assert win._status_lbl.text() == "Stopped · 1 frames sent — bus CAN_1: bus errors (no ACK)"
    assert reports == [("Replay stopped after 1 frames: bus CAN_1: bus errors (no ACK)", "warn")]
    ctx.bus_reader.close()


def test_disconnecting_the_target_stops_without_a_report(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, reports = _replaying_slow_log(qtbot, tmp_path)

    ctx.bus_reader.close()
    qtbot.waitUntil(lambda: win._controller is None, timeout=5000)
    assert win._status_lbl.text() == "Stopped · 1 frames sent — bus CAN_1 disconnected"
    assert reports == []


def test_error_not_overwritten_by_done(qapp, qtbot, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"rp-{uuid.uuid4().hex[:8]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    win = ReplayWindow(instance_id="rp1")
    qtbot.addWidget(win)
    win.bind(ctx)
    reports: list[tuple[str, str]] = []
    ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))

    def send_fails(_msg: can.Message, _bus: str | None = None) -> None:
        raise can.CanOperationError("adapter jammed")

    monkeypatch.setattr(ctx.bus_reader, "send_message", send_fails)
    log = tmp_path / "in.asc"
    _write_log(log, n=3)
    win._path_edit.setText(str(log))
    win._speed_combo.setCurrentIndex(_speed_index_for(win, 100.0))
    win._on_action_clicked()
    qtbot.waitUntil(lambda: win._controller is None, timeout=5000)

    status = win._status_lbl.text()
    assert status.startswith("Failed after 0 frames:")
    assert "adapter jammed" in status
    assert "Done" not in status
    assert [kind for _msg, kind in reports] == ["error"]
    ctx.bus_reader.close()
