# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.bus.BusController`` against python-can's virtual backend."""

from __future__ import annotations

import logging
import threading
import time
import uuid

import can
import pytest
from can.interfaces.virtual import VirtualBus
from PySide6.QtCore import Qt

from ithildin.core.bus import BusController
from ithildin.core.ringbuf import RingBuffer
from ithildin.workspace.schema import BusConfig
from tests.fake_bus import FakeBus, use_fake_bus


@pytest.fixture
def virtual_channel() -> str:
    return f"ithildin-test-{uuid.uuid4().hex[:8]}"


def test_capture_lifecycle(qapp, qtbot, virtual_channel: str) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name="Test",
        interface="virtual",
        channel=virtual_channel,
        bitrate=500000,
    )
    ring = RingBuffer(64)
    controller = BusController(cfg, ring)

    with qtbot.waitSignal(controller.started, timeout=2000):
        controller.start()

    sender = can.Bus(interface="virtual", channel=virtual_channel)
    try:
        for i in range(5):
            sender.send(
                can.Message(
                    arbitration_id=0x100 + i,
                    data=bytes([i, i, i, i]),
                    is_extended_id=False,
                )
            )
    finally:
        sender.shutdown()

    qtbot.waitUntil(lambda: ring.total_seq >= 5, timeout=2000)
    assert controller.stop(wait_ms=2000)

    frames, _ = ring.read_since(-1)
    assert len(frames) == 5
    assert {f.arbitration_id for f in frames} == {0x100, 0x101, 0x102, 0x103, 0x104}
    assert all(f.bus_name == "Test" for f in frames)


def test_open_failure_emits_error(qapp, qtbot, caplog) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name="Bad",
        interface="virtual",
        channel="x",
        bitrate=500000,
    )
    ring = RingBuffer(8)
    controller = BusController(cfg, ring)

    def boom() -> can.BusABC:
        raise RuntimeError("simulated driver failure")

    controller.worker._open_bus = boom  # type: ignore[method-assign]

    with qtbot.waitSignal(controller.error, timeout=2000, raising=True) as blocker:
        controller.start()
    assert "simulated driver failure" in blocker.args[0]
    assert controller.stop(wait_ms=2000)
    (record,) = [r for r in caplog.records if r.name == "ithildin.core.bus"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Failed to open bus 'Bad'"
    assert record.exc_info is not None


class _ShutdownFails(VirtualBus):
    def shutdown(self) -> None:
        super().shutdown()
        raise RuntimeError("driver gone")


class _RecvFails(VirtualBus):
    def recv(self, timeout: float | None = None) -> can.Message | None:
        raise can.CanOperationError("adapter gone")


def test_shutdown_failure_is_logged(qapp, qtbot, caplog, virtual_channel: str) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(name="Test", interface="virtual", channel=virtual_channel, bitrate=500000)
    controller = BusController(cfg, RingBuffer(8))
    controller.worker._open_bus = lambda: _ShutdownFails(channel=virtual_channel)  # type: ignore[method-assign]

    with qtbot.waitSignal(controller.started, timeout=2000):
        controller.start()
    assert controller.stop(wait_ms=2000)

    (record,) = [r for r in caplog.records if r.name == "ithildin.core.bus"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Shutdown of bus 'Test' failed"
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "driver gone"


def test_capture_error_logs_traceback(qapp, qtbot, caplog, virtual_channel: str) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(name="Test", interface="virtual", channel=virtual_channel, bitrate=500000)
    controller = BusController(cfg, RingBuffer(8))
    controller.worker._open_bus = lambda: _RecvFails(channel=virtual_channel)  # type: ignore[method-assign]

    with qtbot.waitSignal(controller.error, timeout=2000) as blocker:
        controller.start()
    assert "adapter gone" in blocker.args[0]
    assert controller.stop(wait_ms=2000)

    (record,) = [r for r in caplog.records if r.name == "ithildin.core.bus"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Capture error on 'Test'"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], can.CanOperationError)


class _Panic(BaseException):
    """What a panic from a vendor binding looks like: not an ``Exception``."""


def _fake_controller(monkeypatch, fake: FakeBus) -> BusController:  # type: ignore[no-untyped-def]
    use_fake_bus(monkeypatch, fake)
    cfg = BusConfig(name="Test", interface="virtual", channel="x", bitrate=500000)
    return BusController(cfg, RingBuffer(8))


def _count(signal) -> list[int]:  # type: ignore[no-untyped-def]
    calls: list[int] = []
    signal.connect(lambda *_args: calls.append(1))
    return calls


def test_stopped_emitted_when_shutdown_raises_base_exception(qapp, qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeBus()
    fake.shutdown_exc = _Panic("driver panicked")
    controller = _fake_controller(monkeypatch, fake)

    with qtbot.waitSignal(controller.started, timeout=2000):
        controller.start()
    with qtbot.waitSignal(controller.stopped, timeout=2000):
        assert controller.stop(wait_ms=2000)
    assert fake.shutdown_calls == 1


def test_recv_base_exception_reports_and_stops(qapp, qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeBus()
    fake.recv_exc = _Panic("adapter panicked")
    controller = _fake_controller(monkeypatch, fake)
    errors: list[str] = []
    controller.error.connect(errors.append)
    stops = _count(controller.stopped)

    with qtbot.waitSignal(controller.finished, timeout=2000):
        controller.start()
    assert errors == ["Capture error on 'Test': adapter panicked"]
    assert stops == [1]
    assert fake.shutdown_calls == 1


def test_open_base_exception_emits_stopped_once(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(name="Test", interface="virtual", channel="x", bitrate=500000)
    controller = BusController(cfg, RingBuffer(8))

    def panic() -> can.BusABC:
        raise _Panic("no driver")

    controller.worker._open_bus = panic  # type: ignore[method-assign]
    errors: list[str] = []
    controller.error.connect(errors.append)
    stops = _count(controller.stopped)

    with qtbot.waitSignal(controller.finished, timeout=2000):
        controller.start()
    assert errors == ["Failed to open bus 'Test': no driver"]
    assert stops == [1]


def test_stop_during_slow_open_never_emits_started(qapp, qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeBus()
    fake.open_gate = threading.Event()
    controller = _fake_controller(monkeypatch, fake)
    starts = _count(controller.started)
    stops = _count(controller.stopped)

    controller.start()
    controller.request_stop()
    with qtbot.waitSignal(controller.finished, timeout=2000):
        fake.open_gate.set()
    assert starts == []
    assert stops == [1]
    assert fake.shutdown_calls == 1


def test_finished_waits_for_the_capture_thread(qapp, qtbot, virtual_channel: str) -> None:  # type: ignore[no-untyped-def]
    """The owner deletes the controller from ``finished``, so the thread must be
    gone by then. A blocking slot on the worker's own ``stopped`` holds the
    thread open long enough to check without racing."""
    cfg = BusConfig(name="Test", interface="virtual", channel=virtual_channel, bitrate=500000)
    controller = BusController(cfg, RingBuffer(8))
    controller.worker.stopped.connect(lambda: time.sleep(0.2), Qt.ConnectionType.DirectConnection)
    joined: list[bool] = []
    controller.finished.connect(lambda: joined.append(controller._thread.isFinished()))

    with qtbot.waitSignal(controller.started, timeout=2000):
        controller.start()
    with qtbot.waitSignal(controller.finished, timeout=2000):
        controller.request_stop()
    assert joined == [True]


@pytest.mark.parametrize(
    "interface, expected_kwarg, expected_value",
    [
        ("pcan", "state", can.BusState.PASSIVE),
        ("kvaser", "driver_mode", False),
        ("cantact", "monitor", True),
    ],
)
def test_listen_only_dispatches_backend_kwarg(
    interface: str, expected_kwarg: str, expected_value: object
) -> None:
    """``listen_only=True`` must translate to the backend's native kwarg."""
    from ithildin.core.bus import _apply_listen_only_kwargs

    kwargs: dict[str, object] = {}
    _apply_listen_only_kwargs(kwargs, interface)
    assert kwargs == {expected_kwarg: expected_value}


def test_listen_only_virtual_is_noop() -> None:
    """Virtual backend has no native silent mode — the app-level gate covers it."""
    from ithildin.core.bus import _apply_listen_only_kwargs

    kwargs: dict[str, object] = {}
    _apply_listen_only_kwargs(kwargs, "virtual")
    assert kwargs == {}


def test_ixxat_parses_in_schema() -> None:
    """IXXAT is a supported interface; integer channels are allowed."""
    cfg = BusConfig(
        name="IXXAT_1",
        interface="ixxat",
        channel=0,
        bitrate=500_000,
    )
    assert cfg.interface == "ixxat"
    assert cfg.channel == 0


def test_ixxat_listen_only_is_rejected_at_schema_level() -> None:
    """python-can's IXXAT classic backend has no silent kwarg, so the
    schema rejects the combo rather than letting it degrade silently to
    an ACKing bus."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BusConfig(
            name="IXXAT_1",
            interface="ixxat",
            channel=0,
            bitrate=500_000,
            listen_only=True,
        )
