# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.record_window.RecordWindow``."""

from __future__ import annotations

import uuid
from pathlib import Path

import can

from ithildin.ui.record_window import RecordWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import kill_capture, make_app_context


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


# ---- Recording across bus loss and reconnect ----


def _send_ids(channel: str, *ids: int) -> None:
    sender = can.Bus(interface="virtual", channel=channel)
    try:
        for aid in ids:
            sender.send(can.Message(arbitration_id=aid, data=b"\x00"))
    finally:
        sender.shutdown()


def _recording(qtbot, tmp_path: Path):  # type: ignore[no-untyped-def]
    """A Record window recording open bus CAN_1 into rec.asc."""
    ctx = make_app_context(tmp_path)
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"r-{uuid.uuid4().hex[:8]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    win = RecordWindow(instance_id="r1")
    qtbot.addWidget(win)
    win.bind(ctx)
    out_path = tmp_path / "rec.asc"
    win._path_edit.setText(str(out_path))
    win._on_action_clicked()
    qtbot.waitUntil(lambda: win._action_btn.text() == "Stop", timeout=2000)
    return ctx, win, cfg, out_path


def _wait_captured(qtbot, ctx, count: int) -> None:  # type: ignore[no-untyped-def]
    ring = ctx.bus_reader.ring
    assert ring is not None
    qtbot.waitUntil(lambda: ring.total_seq >= count, timeout=2000)


def _stop_and_read(qtbot, win: RecordWindow, out_path: Path) -> list[int]:  # type: ignore[no-untyped-def]
    win._on_action_clicked()
    qtbot.waitUntil(lambda: win._logger is None, timeout=5000)
    with can.LogReader(str(out_path)) as reader:
        return [msg.arbitration_id for msg in reader]


def test_recording_survives_bus_failure_and_reconnect(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, cfg, out_path = _recording(qtbot, tmp_path)
    _send_ids(str(cfg.channel), 0x100, 0x101)
    _wait_captured(qtbot, ctx, 2)

    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")
    assert win._logger is not None
    assert win._banner.kind == "warn"
    assert "lost" in win._banner._label.text()
    assert "waiting for a bus" in win._status_label.text()

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.reopen(cfg)
    assert win._banner.isHidden()
    _send_ids(str(cfg.channel), 0x200, 0x201)
    _wait_captured(qtbot, ctx, 2)
    assert _stop_and_read(qtbot, win, out_path) == [0x100, 0x101, 0x200, 0x201]


def test_stop_while_a_bus_is_lost_reads_stopped(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, cfg, _out_path = _recording(qtbot, tmp_path)
    win.show()
    qtbot.waitExposed(win)
    _send_ids(str(cfg.channel), 0x100)
    _wait_captured(qtbot, ctx, 1)
    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")
    assert win._banner.isVisible()

    win._on_action_clicked()
    qtbot.waitUntil(lambda: win._logger is None, timeout=5000)
    assert win._status_label.text() == "Stopped · 1 frames written"
    assert win._banner.isHidden()


def test_recording_follows_disconnect_connect(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, cfg, out_path = _recording(qtbot, tmp_path)
    _send_ids(str(cfg.channel), 0x100)
    _wait_captured(qtbot, ctx, 1)

    ctx.bus_reader.close()
    assert win._logger is not None
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    _send_ids(str(cfg.channel), 0x200)
    _wait_captured(qtbot, ctx, 1)
    assert _stop_and_read(qtbot, win, out_path) == [0x100, 0x200]


def test_lost_frames_shown_and_reported_on_stop(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, _cfg, out_path = _recording(qtbot, tmp_path)
    reports: list[tuple[str, str]] = []
    ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))

    logger = win._logger
    assert logger is not None
    # As if the ring had overwritten 3 frames before the worker wrote them.
    logger._worker._lost = 3
    win._on_action_clicked()
    qtbot.waitUntil(lambda: win._logger is None, timeout=5000)
    assert win._counts_label.text().endswith("LOST 3")
    assert reports == [(f"Recording to {out_path} finished with 3 frames lost", "warn")]
    ctx.bus_reader.close()
