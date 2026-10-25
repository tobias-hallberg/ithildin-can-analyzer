# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.bus.BusController`` against python-can's virtual backend."""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import uuid

import can
import pytest
from can.interfaces.virtual import VirtualBus
from PySide6.QtCore import Qt

from ithildin.core import bus as bus_mod
from ithildin.core.bus import KVASER_ENUM_LOCK, BusController, BusWorker
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
    "interface, expected",
    [
        ("pcan", {"state": can.BusState.PASSIVE}),
        # Silent mode only sticks on the handle with init access: one handle.
        (
            "kvaser",
            {"driver_mode": False, "single_handle": True, "receive_own_messages": False},
        ),
    ],
)
def test_listen_only_dispatches_backend_kwarg(interface: str, expected: dict[str, object]) -> None:
    """``listen_only=True`` must translate to the backend's native kwargs."""
    from ithildin.core.bus import _apply_listen_only_kwargs

    kwargs: dict[str, object] = {"receive_own_messages": True}
    _apply_listen_only_kwargs(kwargs, interface)
    assert kwargs == {"receive_own_messages": True, **expected}


def _kvaser_worker(bitrate: int, *, fd: bool = False, listen_only: bool = False) -> BusWorker:
    cfg = BusConfig(
        name="K", interface="kvaser", channel=0, bitrate=bitrate, fd=fd, listen_only=listen_only
    )
    return BusWorker(cfg.name, cfg, RingBuffer(8))


class _KvaserOpenSpy:
    """Stands in for ``can.Bus`` and ``kvaser_enum_hardware``, recording the
    order of calls and whether each ran with the enumeration lock held."""

    def __init__(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self.calls: list[tuple[str, bool]] = []
        self.kwargs: dict[str, object] = {}
        monkeypatch.setattr(bus_mod, "kvaser_enum_hardware", self._enum)
        monkeypatch.setattr(can, "Bus", self._bus)

    def _enum(self) -> None:
        self.calls.append(("enum", KVASER_ENUM_LOCK.locked()))

    def _bus(self, **kwargs: object) -> object:
        self.calls.append(("bus", KVASER_ENUM_LOCK.locked()))
        self.kwargs = kwargs
        return object()


def test_kvaser_open_reenumerates_under_the_lock_and_refuses_virtual(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = _KvaserOpenSpy(monkeypatch)

    _kvaser_worker(bitrate=500_000)._open_bus()

    assert spy.calls == [("enum", True), ("bus", True)]
    assert spy.kwargs["accept_virtual"] is False
    assert spy.kwargs["bitrate"] == 500_000
    assert not KVASER_ENUM_LOCK.locked()


def test_kvaser_listen_only_opens_one_silent_handle(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = _KvaserOpenSpy(monkeypatch)

    _kvaser_worker(bitrate=500_000, listen_only=True)._open_bus()

    assert spy.kwargs["driver_mode"] is False
    assert spy.kwargs["single_handle"] is True
    assert spy.kwargs["receive_own_messages"] is False


@pytest.mark.parametrize("bitrate, fd", [(800_000, False), (33_333, False), (250_000, True)])
def test_kvaser_open_refuses_non_preset_bitrate_before_any_driver_call(  # type: ignore[no-untyped-def]
    qapp, monkeypatch, bitrate: int, fd: bool
) -> None:
    """KvaserBus would open the channel before refusing the bitrate, and leak
    that handle."""
    spy = _KvaserOpenSpy(monkeypatch)

    with pytest.raises(can.CanInitializationError, match=f"no preset for {bitrate} bit/s"):
        _kvaser_worker(bitrate=bitrate, fd=fd)._open_bus()
    assert spy.calls == []


def test_kvaser_fd_open_takes_an_fd_preset(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spy = _KvaserOpenSpy(monkeypatch)

    _kvaser_worker(bitrate=2_000_000, fd=True)._open_bus()

    assert spy.kwargs["bitrate"] == 2_000_000


def test_kvaser_open_fails_as_busy_while_the_lock_stays_held(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A probe or open hung in the driver keeps the lock: a later open fails
    instead of queueing behind it for good."""
    spy = _KvaserOpenSpy(monkeypatch)
    monkeypatch.setattr(bus_mod, "_KVASER_OPEN_WAIT_S", 0.3)

    with KVASER_ENUM_LOCK, pytest.raises(can.CanInitializationError, match="Kvaser driver busy"):
        _kvaser_worker(bitrate=500_000)._open_bus()
    assert spy.calls == []


def test_stopping_a_kvaser_bus_queued_for_the_lock_ends_it_quietly(
    qapp, qtbot, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Well before the open's own deadline, and with no error: the user
    stopped it."""
    spy = _KvaserOpenSpy(monkeypatch)
    cfg = BusConfig(name="K", interface="kvaser", channel=0, bitrate=500_000)
    controller = BusController(cfg, RingBuffer(8))
    errors: list[str] = []
    controller.error.connect(errors.append)
    starts = _count(controller.started)
    stops = _count(controller.stopped)

    with KVASER_ENUM_LOCK:
        controller.start()
        with qtbot.waitSignal(controller.finished, timeout=5000):
            controller.request_stop()
    assert errors == []
    assert starts == []
    assert stops == [1]
    assert spy.calls == []


class _Canlib32:
    """canlib32.dll as ctypes sees it: it exports only ``canEnumHardwareEx``,
    not ``canEnumHardware``."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.calls = 0

    def canEnumHardwareEx(self, _count: object) -> int:
        self.calls += 1
        return self.status


def _fake_canlib32(monkeypatch, dll: _Canlib32 | OSError) -> list[str]:  # type: ignore[no-untyped-def]
    names: list[str] = []

    def load(name: str) -> _Canlib32:
        names.append(name)
        if isinstance(dll, OSError):
            raise dll
        return dll

    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    return names


@pytest.mark.skipif(sys.platform != "win32", reason="CANlib re-enumerates by itself on Linux")
def test_kvaser_enum_hardware_calls_canlib(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dll = _Canlib32()
    names = _fake_canlib32(monkeypatch, dll)

    bus_mod.kvaser_enum_hardware()

    assert names == ["canlib32"]
    assert dll.calls == 1


@pytest.mark.skipif(sys.platform != "win32", reason="CANlib re-enumerates by itself on Linux")
def test_kvaser_enum_hardware_logs_a_failed_enumeration(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    _fake_canlib32(monkeypatch, _Canlib32(status=-3))

    bus_mod.kvaser_enum_hardware()

    (record,) = [r for r in caplog.records if r.name == "ithildin.core.bus"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Kvaser canEnumHardwareEx failed with status -3"


@pytest.mark.skipif(sys.platform != "win32", reason="CANlib re-enumerates by itself on Linux")
def test_kvaser_enum_hardware_without_canlib_is_a_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    names = _fake_canlib32(monkeypatch, OSError("canlib32 not found"))

    bus_mod.kvaser_enum_hardware()

    assert names == ["canlib32"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows re-enumerates through canlib32")
def test_kvaser_enum_hardware_does_nothing_off_windows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    names = _fake_canlib32(monkeypatch, _Canlib32())

    bus_mod.kvaser_enum_hardware()

    assert names == []


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
