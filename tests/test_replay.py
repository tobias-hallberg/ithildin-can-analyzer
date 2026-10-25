# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Replay round-trip: write log, replay onto a virtual bus, count frames."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

import can
from PySide6.QtCore import Qt

from ithildin.core.replay import ReplayController


def _write_log(path: Path) -> int:
    """Write 6 frames with 5 ms spacing. Returns count."""
    writer = can.Logger(str(path))
    try:
        ts0 = 1000.0
        for i in range(6):
            writer.on_message_received(
                can.Message(
                    timestamp=ts0 + 0.005 * i,
                    arbitration_id=0x200 + i,
                    data=bytes([i]),
                    is_extended_id=False,
                    channel="A",
                )
            )
    finally:
        writer.stop()
    return 6


def test_replay_roundtrip(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    log_path = tmp_path / "in.asc"
    n_written = _write_log(log_path)

    channel = f"replay-{uuid.uuid4().hex[:8]}"
    # Receiver sees what the replay puts onto the bus.
    receiver = can.Bus(interface="virtual", channel=channel)
    sender = can.Bus(interface="virtual", channel=channel)

    received: list[int] = []

    try:
        # Replay at high speed to keep test runtime negligible.
        controller = ReplayController(log_path, sender.send, speed=100.0)

        with qtbot.waitSignal(controller.finished, timeout=5000) as blocker:
            controller.start()
        sent_count = blocker.args[0]
        assert sent_count == n_written
        assert controller.stop(wait_ms=2000)

        # Drain receiver (non-blocking).
        while True:
            msg = receiver.recv(timeout=0.1)
            if msg is None:
                break
            received.append(msg.arbitration_id)
    finally:
        sender.shutdown()
        receiver.shutdown()

    assert received == [0x200 + i for i in range(n_written)]


def test_replay_stop_early(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    log_path = tmp_path / "in.asc"
    _write_log(log_path)

    channel = f"replay-{uuid.uuid4().hex[:8]}"
    sender = can.Bus(interface="virtual", channel=channel)
    receiver = can.Bus(interface="virtual", channel=channel)

    try:
        # Real-time speed so the loop is paced; we'll cut it off after the
        # first frame is sent.
        controller = ReplayController(log_path, sender.send, speed=1.0)

        with qtbot.waitSignal(controller.progress, timeout=2000):
            controller.start()
        assert controller.stop(wait_ms=2000)
    finally:
        sender.shutdown()
        receiver.shutdown()


def test_finished_waits_for_the_replay_thread(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """``ReplayWindow._on_finished`` drops the last reference to the controller.

    Relaying the worker's own ``finished`` announced completion while the replay
    thread was still unwinding, so the consumer's delete could land on a live
    QThread — an access violation on Windows, and the reason CI died here. The
    real window is a few microseconds; a blocking slot on the worker's own
    signal holds the thread open long enough to check without racing.
    """
    log_path = tmp_path / "in.asc"
    _write_log(log_path)

    controller = ReplayController(log_path, lambda msg: None, speed=100.0)
    controller._worker.finished.connect(
        lambda _sent, _outcome: time.sleep(0.2), Qt.ConnectionType.DirectConnection
    )

    joined: list[bool] = []
    controller.finished.connect(
        lambda _sent, _outcome: joined.append(controller._thread.isFinished())
    )
    with qtbot.waitSignal(controller.finished, timeout=5000):
        controller.start()

    assert joined == [True]


def test_replay_failure_logs_traceback(qapp, qtbot, tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    log_path = tmp_path / "in.asc"
    _write_log(log_path)

    def send(_msg: can.Message) -> None:
        raise can.CanOperationError("bus gone")

    controller = ReplayController(log_path, send, speed=100.0)
    with qtbot.waitSignal(controller.error, timeout=5000) as blocker:
        controller.start()
    assert "bus gone" in blocker.args[0]
    assert controller.stop(wait_ms=2000)

    (record,) = [r for r in caplog.records if r.name == "ithildin.core.replay"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"Replay of {log_path} failed"
    assert record.exc_info is not None


def _write_gapped_log(path: Path, gap_s: float) -> None:
    """Two frames ``gap_s`` apart."""
    writer = can.Logger(str(path))
    try:
        for i, ts in enumerate((1000.0, 1000.0 + gap_s)):
            writer.on_message_received(
                can.Message(timestamp=ts, arbitration_id=0x300 + i, channel="A")
            )
    finally:
        writer.stop()


class _Sender:
    """Records sends; raises from the ``fail_from``-th one (0-based) on."""

    def __init__(self, fail_from: int | None = None) -> None:
        self.sent: list[int] = []
        self.first = threading.Event()
        self._fail_from = fail_from

    def __call__(self, msg: can.Message) -> None:
        if self._fail_from is not None and len(self.sent) >= self._fail_from:
            raise can.CanOperationError("bus gone")
        self.sent.append(msg.arbitration_id)
        self.first.set()


def test_outcome_done_stopped_failed(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    log_path = tmp_path / "in.asc"
    _write_log(log_path)

    done = ReplayController(log_path, _Sender(), speed=100.0)
    with qtbot.waitSignal(done.finished, timeout=5000) as blocker:
        done.start()
    assert blocker.args == [6, "done"]

    failed = ReplayController(log_path, _Sender(fail_from=2), speed=100.0)
    with qtbot.waitSignal(failed.finished, timeout=5000) as blocker:
        failed.start()
    assert blocker.args == [2, "failed"]

    gapped = tmp_path / "gapped.asc"
    _write_gapped_log(gapped, 30.0)
    sender = _Sender()
    stopped = ReplayController(gapped, sender, speed=1.0)
    stopped.start()
    assert sender.first.wait(5)
    with qtbot.waitSignal(stopped.finished, timeout=5000) as blocker:
        assert stopped.stop(wait_ms=2000)
    assert blocker.args == [1, "stopped"]


def test_stop_interrupts_long_gap(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    log_path = tmp_path / "in.asc"
    _write_gapped_log(log_path, 30.0)
    sender = _Sender()
    controller = ReplayController(log_path, sender, speed=1.0)
    controller.start()
    assert sender.first.wait(5)

    assert controller.stop(wait_ms=500)
    assert sender.sent == [0x300]
