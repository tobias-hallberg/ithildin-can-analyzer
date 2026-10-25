# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.logger.TeeLogger`` round-trip via can.LogReader."""

from __future__ import annotations

import logging
from pathlib import Path

import can

from ithildin.core.logger import TeeLogger
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord


def test_log_roundtrip_asc(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(64)
    for i in range(8):
        ring.append(
            FrameRecord(
                bus_name="A",
                timestamp=1000.0 + i,
                arbitration_id=0x100 + i,
                data=bytes([i, i + 1, i + 2, i + 3]),
            )
        )

    log_path = tmp_path / "out.asc"
    logger = TeeLogger(log_path, {"A": ring})

    with qtbot.waitSignal(logger.started, timeout=2000):
        logger.start()
    qtbot.waitUntil(lambda: log_path.exists(), timeout=2000)
    assert logger.stop(wait_ms=5000)

    # Read back via python-can's LogReader and check arbitration IDs match.
    with can.LogReader(str(log_path)) as reader:
        ids = [msg.arbitration_id for msg in reader]
    assert ids == [0x100 + i for i in range(8)]


def test_log_open_failure_emits_error(qapp, qtbot, tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    # Unsupported extension forces can.Logger to raise.
    ring = RingBuffer(8)
    bad_path = tmp_path / "out.unsupported_ext"
    logger = TeeLogger(bad_path, {"A": ring})

    with qtbot.waitSignal(logger.error, timeout=2000) as blocker:
        logger.start()
    assert "Failed to open log file" in blocker.args[0]
    assert logger.stop(wait_ms=2000)
    (record,) = [r for r in caplog.records if r.name == "ithildin.core.logger"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"Failed to open log file {bad_path}"
    assert record.exc_info is not None


class _FinalizeFails:
    def __init__(self, _path: str) -> None:
        pass

    def on_message_received(self, _msg: can.Message) -> None:
        pass

    def stop(self) -> None:
        raise OSError("disk gone")


def test_finalize_failure_logged_as_error(qapp, qtbot, tmp_path: Path, caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Nothing else surfaces a failed finalize, and the file is then corrupt."""
    monkeypatch.setattr(can, "Logger", _FinalizeFails)
    logger = TeeLogger(tmp_path / "out.mf4", {"A": RingBuffer(8)})

    with qtbot.waitSignal(logger.started, timeout=2000):
        logger.start()
    assert logger.stop(wait_ms=5000)

    (record,) = [r for r in caplog.records if r.name == "ithildin.core.logger"]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f"Failed to finalize log file {tmp_path / 'out.mf4'}"
    assert record.exc_info is not None
