# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""A scriptable in-memory python-can bus for fault-path tests.

``use_fake_bus`` makes every ``BusWorker`` open the fake instead of a real
backend, so a ``BusConfig(interface="pcan" | "kvaser" | "ixxat", ...)``
exercises that backend's branches without importing a vendor module.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import can
import pytest

from ithildin.core.bus import BusWorker


class FakeBus(can.BusABC):
    """Records what the app does to the bus; fails on demand.

    * ``send_exc`` / ``recv_exc``: raised by every ``send`` / ``recv``
      while set.
    * ``sent``: ``(msg, sending thread, send lock held)`` per send.
    * ``resets`` (PCAN flush) and ``flushes`` (Kvaser flush) count calls;
      ``flush_exc`` makes either raise.
    * ``recv_calls`` counts polls; once it moves past a reading taken
      after a request flag cleared, the worker has acted on the request.
    * ``shutdown_calls`` counts ``shutdown``.

    ``lock`` is the owning worker's send lock, set by ``use_fake_bus``.
    """

    def __init__(self) -> None:
        super().__init__(channel="fake")
        self.channel_info = "fake"
        self.lock: threading.Lock | None = None
        self.send_exc: Exception | None = None
        self.recv_exc: Exception | None = None
        self.flush_exc: Exception | None = None
        self.sent: list[tuple[can.Message, threading.Thread, bool]] = []
        self.resets = 0
        self.flushes = 0
        self.recv_calls = 0
        self.shutdown_calls = 0

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        if self.send_exc is not None:
            raise self.send_exc
        held = self.lock is not None and self.lock.locked()
        self.sent.append((msg, threading.current_thread(), held))

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        self.recv_calls += 1
        if self.recv_exc is not None:
            raise self.recv_exc
        time.sleep(timeout or 0.0)
        return None, False

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


def use_fake_bus(monkeypatch: pytest.MonkeyPatch, fake: FakeBus | None = None) -> FakeBus:
    """Make every ``BusWorker`` open ``fake`` (a new one if omitted)."""
    bus = fake if fake is not None else FakeBus()

    def _open(worker: BusWorker) -> can.BusABC:
        bus.lock = worker._send_lock
        return bus

    monkeypatch.setattr(BusWorker, "_open_bus", _open)
    return bus
