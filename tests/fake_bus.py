# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""A scriptable in-memory python-can bus for fault-path tests.

``use_fake_bus`` makes every ``BusWorker`` open the fake instead of a real
backend, so a ``BusConfig(interface="pcan" | "kvaser" | "ixxat", ...)``
exercises that backend's branches without importing a vendor module.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import can
import pytest

from ithildin.core.bus import BusWorker


class FakeBus(can.BusABC):
    """Records what the app does to the bus; fails on demand.

    * ``send_exc`` / ``recv_exc``: raised by every ``send`` / ``recv``
      while set.
    * ``rx``: scripted receive. Each queued message is returned once, each
      queued exception raised once, in order; an empty queue times out.
    * ``status_code``: what PCAN's ``status()`` reports (0 is OK);
      ``pcan_error(code)`` is the text PCAN's receive error carries.
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
        self.rx: queue.SimpleQueue[can.Message | Exception] = queue.SimpleQueue()
        self.status_code = 0
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


def pcan_error(code: int) -> str:
    return f"PCAN error 0x{code:X}."


def use_fake_bus(monkeypatch: pytest.MonkeyPatch, fake: FakeBus | None = None) -> FakeBus:
    """Make every ``BusWorker`` open ``fake`` (a new one if omitted)."""
    bus = fake if fake is not None else FakeBus()

    def _open(worker: BusWorker) -> can.BusABC:
        bus.lock = worker._send_lock
        return bus

    monkeypatch.setattr(BusWorker, "_open_bus", _open)
    return bus
