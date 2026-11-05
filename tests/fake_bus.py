# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""A scriptable in-memory python-can bus for fault-path tests.

``use_fake_bus`` makes every ``BusWorker`` open the fake instead of a real
backend, so a ``BusConfig(interface="pcan" | "kvaser" | "ixxat", ...)``
exercises that backend's branches without importing a vendor module.
``FakePcanBasic`` stands in for the PCAN-Basic driver under python-can's
real ``PcanBus``, for tests of ``BusWorker._open_pcan`` on a host without
PEAK's driver. ``LiveReader`` stands in for open buses where only their
names matter.
"""

from __future__ import annotations

import ctypes
import queue
import threading
from typing import Any

import can
import pytest
from can.interfaces.pcan import basic
from can.interfaces.pcan import pcan as pcan_mod

from ithildin.app_state.bus_reader import BusReader
from ithildin.core.bus import BusWorker
from ithildin.core.types import BusHealth


class FakeBus(can.BusABC):
    """Records what the app does to the bus; fails on demand.

    * ``send_exc`` / ``recv_exc``: raised by every ``send`` / ``recv``
      while set.
    * ``open_gate``: opening waits for this event (a slow vendor open).
    * ``block``: every ``recv`` waits for this event (a hung vendor call).
    * ``rx``: scripted receive. Each queued message is returned once, each
      queued exception raised once, in order; an empty queue times out.
    * ``status_code``: what PCAN's ``status()`` reports (0 is OK);
      ``pcan_error(code)`` is the text PCAN's receive error carries.
    * ``sent``: ``(msg, sending thread, send lock held)`` per send.
    * ``resets`` (PCAN flush) and ``flushes`` (Kvaser flush) count calls;
      ``flush_exc`` makes either raise.
    * ``recv_calls`` counts polls; once it moves past a reading taken
      after a request flag cleared, the worker has acted on the request.
    * ``shutdown_calls`` counts ``shutdown``; ``shutdown_exc`` makes it
      raise after counting.

    ``lock`` is the owning worker's send lock, set by ``use_fake_bus``.
    """

    def __init__(self) -> None:
        super().__init__(channel="fake")
        self.channel_info = "fake"
        self.lock: threading.Lock | None = None
        self.send_exc: Exception | None = None
        self.recv_exc: BaseException | None = None
        self.open_gate: threading.Event | None = None
        self.block: threading.Event | None = None
        self.rx: queue.SimpleQueue[can.Message | Exception] = queue.SimpleQueue()
        self.status_code = 0
        self.flush_exc: Exception | None = None
        self.sent: list[tuple[can.Message, threading.Thread, bool]] = []
        self.resets = 0
        self.flushes = 0
        self.recv_calls = 0
        self.shutdown_calls = 0
        self.shutdown_exc: BaseException | None = None

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        if self.send_exc is not None:
            raise self.send_exc
        held = self.lock is not None and self.lock.locked()
        self.sent.append((msg, threading.current_thread(), held))

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        self.recv_calls += 1
        if self.block is not None:
            self.block.wait()
        if self.recv_exc is not None:
            raise self.recv_exc
        try:
            item = self.rx.get(timeout=timeout or 0.0)
        except queue.Empty:
            return None, False
        if isinstance(item, Exception):
            raise item
        return item, False

    def status(self) -> int:
        return self.status_code

    def _get_formatted_error(self, error: int) -> str:
        return pcan_error(error)

    def reset(self) -> bool:
        if self.flush_exc is not None:
            raise self.flush_exc
        self.resets += 1
        return True

    def flush_tx_buffer(self) -> None:
        if self.flush_exc is not None:
            raise self.flush_exc
        self.flushes += 1

    def _send_periodic_internal(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the app builds its own cyclic tasks")

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        super().shutdown()
        if self.shutdown_exc is not None:
            raise self.shutdown_exc


def pcan_error(code: int) -> str:
    return f"PCAN error 0x{code:X}."


def use_fake_bus(monkeypatch: pytest.MonkeyPatch, fake: FakeBus | None = None) -> FakeBus:
    """Make every ``BusWorker`` open ``fake`` (a new one if omitted)."""
    bus = fake if fake is not None else FakeBus()

    def _open(worker: BusWorker) -> can.BusABC:
        bus.lock = worker._send_lock
        if bus.open_gate is not None:
            bus.open_gate.wait()
        return bus

    monkeypatch.setattr(BusWorker, "_open_bus", _open)
    return bus


class FakePcanBasic:
    """PCAN-Basic for one process, under python-can's real ``PcanBus``.

    Like the driver it keeps the channels this process has initialised:
    ``Initialize`` refuses one already initialised, ``GetStatus`` reports
    ``PCAN_ERROR_INITIALIZE`` for one that isn't. ``init_result`` is what
    ``Initialize`` returns otherwise (CAUTION initialises too); ``refused``
    maps a parameter to the error ``SetValue`` returns for it, ``raises`` to
    the exception it raises, and ``unreadable`` to the error ``GetValue``
    returns for it. Listen-only set before ``Initialize`` is read back after
    it, until ``Uninitialize``. ``received`` is the receive queue: ``Read``
    pops it, ``Reset`` empties it. ``api_version`` is the PCAN-Basic version
    it reports.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.api_version = b"4.6.0.0"
        self.initialised: set[int] = set()
        self.init_result = basic.PCAN_ERROR_OK
        self.refused: dict[int, int] = {}
        self.raises: dict[int, BaseException] = {}
        self.unreadable: dict[int, int] = {}
        self.values: dict[int, object] = {}
        self.received: list[basic.TPCANMsg] = []
        self.calls: list[tuple[str, int]] = []
        self.uninitialised: list[int] = []
        monkeypatch.setattr(basic, "PCANBasic", lambda: self)
        monkeypatch.setattr(pcan_mod, "PCANBasic", lambda: self)

    def Initialize(self, channel: ctypes.c_ushort, btr0btr1: ctypes.c_ushort, *_: object) -> int:
        self.calls.append(("Initialize", btr0btr1.value))
        if channel.value in self.initialised:
            return basic.PCAN_ERROR_INITIALIZE
        if self.init_result in (basic.PCAN_ERROR_OK, basic.PCAN_ERROR_CAUTION):
            self.initialised.add(channel.value)
        return self.init_result

    def SetValue(self, _channel: ctypes.c_ushort, parameter: ctypes.c_ubyte, value: object) -> int:
        self.calls.append(("SetValue", parameter.value))
        if parameter.value in self.raises:
            raise self.raises[parameter.value]
        status = self.refused.get(parameter.value, basic.PCAN_ERROR_OK)
        if status == basic.PCAN_ERROR_OK:
            self.values[parameter.value] = value
        return status

    def GetValue(self, _channel: ctypes.c_ushort, parameter: ctypes.c_ubyte) -> tuple[int, object]:
        if parameter.value == basic.PCAN_API_VERSION.value:
            return basic.PCAN_ERROR_OK, self.api_version
        if parameter.value in self.unreadable:
            return self.unreadable[parameter.value], 0
        return basic.PCAN_ERROR_OK, self.values.get(parameter.value, 0)

    def Read(self, _channel: ctypes.c_ushort) -> tuple[int, basic.TPCANMsg, basic.TPCANTimestamp]:
        if not self.received:
            return basic.PCAN_ERROR_QRCVEMPTY, basic.TPCANMsg(), basic.TPCANTimestamp()
        return basic.PCAN_ERROR_OK, self.received.pop(0), basic.TPCANTimestamp()

    def Reset(self, _channel: ctypes.c_ushort) -> int:
        self.received.clear()
        return basic.PCAN_ERROR_OK

    def GetStatus(self, channel: ctypes.c_ushort) -> int:
        if channel.value in self.initialised:
            return basic.PCAN_ERROR_OK
        return basic.PCAN_ERROR_INITIALIZE

    def Uninitialize(self, channel: ctypes.c_ushort) -> int:
        self.uninitialised.append(channel.value)
        if channel.value not in self.initialised:
            return basic.PCAN_ERROR_INITIALIZE
        self.initialised.discard(channel.value)
        self.values.clear()
        return basic.PCAN_ERROR_OK

    def GetErrorText(self, error: int, _language: int = 0) -> tuple[int, bytes]:
        return basic.PCAN_ERROR_OK, f"PCAN error 0x{error:X}".encode()


class LiveReader(BusReader):
    """A ``BusReader`` on which ``names`` read as open and live, with no worker
    behind them, so an MCP send naming one gets past the bus checks."""

    def __init__(self, *names: str) -> None:
        super().__init__()
        self._live = names

    def bus_names(self) -> list[str]:
        return [*super().bus_names(), *self._live]

    def health(self, name: str) -> tuple[BusHealth, str] | None:
        return (BusHealth.OK, "") if name in self._live else super().health(name)
