# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.logger.TeeLogger`` round-trip via can.LogReader."""

from __future__ import annotations

import gc
import logging
import threading
import time
import weakref
from pathlib import Path

import can
from PySide6.QtCore import Qt

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


def test_stopped_after_thread_finished(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The Record window drops its last reference to the logger from ``stopped``.
    A blocking slot on the worker's own ``stopped`` holds the thread open."""
    logger = TeeLogger(tmp_path / "out.unsupported_ext", {"A": RingBuffer(8)})
    logger._worker.stopped.connect(lambda: time.sleep(0.2), Qt.ConnectionType.DirectConnection)
    joined: list[bool] = []
    logger.stopped.connect(lambda: joined.append(logger._thread.isFinished()))

    with qtbot.waitSignal(logger.stopped, timeout=5000):
        logger.start()
    assert joined == [True]


class _Panic(BaseException):
    pass


class _FinalizePanics(_FinalizeFails):
    def stop(self) -> None:
        raise _Panic("writer panicked")


def test_stopped_when_writer_stop_raises(qapp, qtbot, tmp_path: Path, caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(can, "Logger", _FinalizePanics)
    logger = TeeLogger(tmp_path / "out.mf4", {"A": RingBuffer(8)})

    with qtbot.waitSignal(logger.started, timeout=2000):
        logger.start()
    with qtbot.waitSignal(logger.stopped, timeout=5000):
        assert logger.stop(wait_ms=5000)
    (record,) = [r for r in caplog.records if r.name == "ithildin.core.logger"]
    assert record.levelno == logging.ERROR


# ---- Following the open buses ----


def _append(ring: RingBuffer, *ids: int) -> None:
    for aid in ids:
        ring.append(FrameRecord(bus_name="A", timestamp=1000.0 + aid, arbitration_id=aid, data=b""))


def _ids_in(path: Path) -> list[int]:
    with can.LogReader(str(path)) as reader:
        return [msg.arbitration_id for msg in reader]


def test_tee_worker_follows_ring_replacement(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    old = RingBuffer(64)
    _append(old, 1, 2, 3)
    log_path = tmp_path / "out.asc"
    logger = TeeLogger(log_path, {"A": old})
    with qtbot.waitSignal(logger.started, timeout=2000):
        logger.start()

    # A reconnect: a new ring under the same name, its seq back at 0.
    new = RingBuffer(64)
    logger.set_rings({"A": new})
    _append(new, 4, 5)
    assert logger.stop(wait_ms=5000)
    assert _ids_in(log_path) == [1, 2, 3, 4, 5]


def test_tee_worker_drains_removed_ring(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(64)
    log_path = tmp_path / "out.asc"
    logger = TeeLogger(log_path, {"A": ring})
    with qtbot.waitSignal(logger.started, timeout=2000):
        logger.start()

    _append(ring, 1, 2, 3)
    logger.set_rings({})
    assert logger.stop(wait_ms=5000)
    assert _ids_in(log_path) == [1, 2, 3]


def test_tee_worker_reports_ring_overrun(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    # Overwritten before Start: not lost to this recording.
    _append(ring, *range(20))
    log_path = tmp_path / "out.asc"
    logger = TeeLogger(log_path, {"A": ring})
    gate = threading.Event()
    logger._worker.started.connect(lambda: gate.wait(5), Qt.ConnectionType.DirectConnection)
    lost: list[int] = []
    logger.lost_count.connect(lost.append)
    try:
        with qtbot.waitSignal(logger.started, timeout=2000):
            logger.start()
        # The worker holds its Start cursor while 20 more frames overrun the ring.
        _append(ring, *range(20, 40))
    finally:
        gate.set()
    with qtbot.waitSignal(logger.stopped, timeout=5000):
        assert logger.stop(wait_ms=5000)
    assert lost[-1] == 20
    assert _ids_in(log_path) == list(range(32, 40))


def test_tee_worker_reports_no_loss_for_the_start_backlog(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    _append(ring, *range(20))
    log_path = tmp_path / "out.asc"
    logger = TeeLogger(log_path, {"A": ring})
    lost: list[int] = []
    logger.lost_count.connect(lost.append)
    with qtbot.waitSignal(logger.started, timeout=2000):
        logger.start()
    with qtbot.waitSignal(logger.stopped, timeout=5000):
        assert logger.stop(wait_ms=5000)
    assert lost == [0]
    assert _ids_in(log_path) == list(range(12, 20))


def test_quit_waits_for_a_recording_to_be_written_out(
    qapp, qtbot, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """An .mf4 is written out as its writer stops, which can outlast any cap:
    quitting waits for it, also once nothing else holds the logger."""
    release = threading.Event()
    written: list[bool] = []
    real_logger = can.Logger

    class SlowToWriteOut:
        def __init__(self, path: str) -> None:
            self._inner = real_logger(path)

        def on_message_received(self, msg: can.Message) -> None:
            self._inner.on_message_received(msg)

        def stop(self) -> None:
            release.wait(10)
            self._inner.stop()
            written.append(True)

    monkeypatch.setattr(can, "Logger", SlowToWriteOut)
    ring = RingBuffer(8)
    _append(ring, 1, 2)
    log_path = tmp_path / "out.asc"
    logger = TeeLogger(log_path, {"A": ring})
    with qtbot.waitSignal(logger.started, timeout=2000):
        logger.start()
    try:
        assert not logger.stop(wait_ms=50)
        ref = weakref.ref(logger)
        del logger
        gc.collect()
        assert ref() is not None
        threading.Timer(0.3, release.set).start()

        qapp.aboutToQuit.emit()

        assert written == [True]
        assert _ids_in(log_path) == [1, 2]
    finally:
        release.set()
