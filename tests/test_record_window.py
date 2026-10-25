# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.record_window.RecordWindow``."""

from __future__ import annotations

import uuid
from pathlib import Path

import can

from ithildin.ui.record_window import RecordWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import make_app_context


def test_start_without_bus_shows_error(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = RecordWindow(instance_id="r1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    win._path_edit.setText(str(tmp_path / "out.asc"))
    win._on_action_clicked()
    assert "No bus open" in win._status_label.text()
    assert win._logger is None


def test_start_without_path_shows_error(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"r-{uuid.uuid4().hex[:8]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)

    win = RecordWindow(instance_id="r1")
    qtbot.addWidget(win)
    win.bind(ctx)

    win._on_action_clicked()
    assert "Pick an output file" in win._status_label.text()
    ctx.bus_reader.close()


def test_record_lifecycle(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    channel = f"r-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)

    win = RecordWindow(instance_id="r1")
    qtbot.addWidget(win)
    win.bind(ctx)

    out_path = tmp_path / "rec.asc"
    win._path_edit.setText(str(out_path))
    win._on_action_clicked()
    qtbot.waitUntil(
        lambda: win._logger is not None and win._action_btn.text() == "Stop",
        timeout=2000,
    )

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        for i in range(3):
            sender.send(can.Message(arbitration_id=0x100 + i, data=bytes([i])))
    finally:
        sender.shutdown()

    ring = ctx.bus_reader.ring
    qtbot.waitUntil(lambda: ring is not None and ring.total_seq >= 3, timeout=2000)
    win._on_action_clicked()
    qtbot.waitUntil(lambda: win._logger is None, timeout=5000)
    assert out_path.exists()
    assert out_path.stat().st_size > 0
    ctx.bus_reader.close()


def test_quit_finalizes_recording(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Child views get no closeEvent on quit; aboutToQuit stops the recorder."""
    ctx = make_app_context(tmp_path)
    channel = f"r-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    win = RecordWindow(instance_id="r1")
    qtbot.addWidget(win)
    win.bind(ctx)
    out_path = tmp_path / "rec.asc"
    win._path_edit.setText(str(out_path))
    win._on_action_clicked()
    qtbot.waitUntil(lambda: win._action_btn.text() == "Stop", timeout=2000)
    logger = win._logger
    assert logger is not None

    ctx.bus_reader.close()
    qapp.aboutToQuit.emit()

    assert logger._thread.isFinished()
    assert out_path.read_text(encoding="utf-8").rstrip().endswith("End TriggerBlock")
