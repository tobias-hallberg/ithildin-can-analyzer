# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""python-can capture worker.

Threading model: a ``BusWorker`` is a ``QObject`` moved onto its own
``QThread``. The worker drives a polling-timeout ``recv()`` loop so shutdown
is just a flag flip — no cross-thread ``Bus.shutdown()`` race, no relying on
backend-specific behaviour to interrupt a blocking C call.

Transmits come from any thread (TX window one-shots, cyclic tasks, Replay,
MCP). Every touch of the python-can bus other than ``recv()`` takes the
worker's send lock, so senders, task creation, the TX flush and shutdown
never overlap.

A ``BusController`` wraps the QThread + worker pair to give the rest of the
app one thing to start/stop.
"""

from __future__ import annotations

import logging
import threading
from functools import partial
from typing import Any, cast

import can
from can.broadcastmanager import ThreadBasedCyclicSendTask
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord
from ithildin.workspace.schema import BusConfig

_log = logging.getLogger(__name__)

_RECV_TIMEOUT_S = 0.1
"""Worst-case shutdown latency. Bus polls recv() at this cadence and checks
the stop flag between iterations."""


class BusWorker(QObject):
    """Owns a python-can ``BusABC`` and pushes frames into a ``RingBuffer``."""

    started = Signal()
    stopped = Signal()
    error = Signal(str)
    frame_count = Signal(int)
    cyclic_failed = Signal(int, str)  # arbitration_id, error

    def __init__(
        self,
        name: str,
        config: BusConfig,
        ring: RingBuffer,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._config = config
        self._ring = ring
        self._stop = False
        self._flush_requested = False
        self._bus: can.BusABC | None = None
        self._send_lock = threading.Lock()
        self._tasks: set[ThreadBasedCyclicSendTask] = set()

    @property
    def name(self) -> str:
        return self._name

    @Slot()
    def start_capture(self) -> None:
        try:
            bus = self._open_bus()
        except Exception as exc:
            _log.warning("Failed to open bus %r", self._name, exc_info=True)
            self.error.emit(f"Failed to open bus {self._name!r}: {exc}")
            self.stopped.emit()
            return
        with self._send_lock:
            self._bus = bus

        self.started.emit()
        count = 0
        try:
            while not self._stop:
                if self._flush_requested:
                    self._flush_requested = False
                    with self._send_lock:
                        self._flush_tx(bus)
                msg = bus.recv(timeout=_RECV_TIMEOUT_S)
                if msg is None:
                    continue
                rec = FrameRecord(
                    bus_name=self._name,
                    timestamp=msg.timestamp,
                    arbitration_id=msg.arbitration_id,
                    data=bytes(msg.data),
                    is_extended_id=msg.is_extended_id,
                    is_fd=getattr(msg, "is_fd", False),
                    is_error_frame=msg.is_error_frame,
                    is_remote_frame=msg.is_remote_frame,
                    is_rx=bool(getattr(msg, "is_rx", True)),
                )
                self._ring.append(rec)
                count += 1
                if count % 100 == 0:
                    self.frame_count.emit(count)
        except Exception as exc:
            _log.warning("Capture error on %r", self._name, exc_info=True)
            self.error.emit(f"Capture error on {self._name!r}: {exc}")
        finally:
            self.frame_count.emit(count)
            self._close_bus()
            self.stopped.emit()

    @Slot()
    def request_stop(self) -> None:
        """Cross-thread safe: sets a bool flag and stops the cyclic tasks.

        The capture loop checks the flag between ``recv()`` polls (every
        ``_RECV_TIMEOUT_S`` worst case). The tasks are stopped here too,
        without the send lock, so periodic TX ends even while the worker
        is stuck in a vendor call.
        """
        self._stop = True
        for task in list(self._tasks):
            task.stop()

    def request_flush(self) -> None:
        """Discard the adapter's queued transmits, on the worker thread.

        Cross-thread safe: sets a flag the capture loop services under the
        send lock. A Kvaser handle must not be touched from another thread
        while the worker reads on it.
        """
        self._flush_requested = True

    def send(self, msg: can.Message) -> None:
        """Send one frame on this bus. Callable from any thread.

        Serialised with every other sender by the bus's send lock. Raises
        ``RuntimeError`` once the bus is closing.
        """
        if self._stop:
            raise RuntimeError(f"bus {self._name!r} not open")
        with self._send_lock:
            bus = self._bus
            if bus is None:
                raise RuntimeError(f"bus {self._name!r} not open")
            bus.send(msg)

    def send_periodic(self, msg: can.Message, period_s: float) -> ThreadBasedCyclicSendTask:
        """Start a periodic transmit and return its task. Callable from any thread.

        The app builds the task itself for every backend instead of calling
        ``bus.send_periodic``: IXXAT's hardware scheduler can't be stopped
        twice, has no ``modify_data`` and doesn't echo into the Trace. The
        first send error stops the task and emits ``cyclic_failed``.
        """
        if self._stop:
            raise RuntimeError(f"bus {self._name!r} not open")
        with self._send_lock:
            bus = self._bus
            if bus is None:
                raise RuntimeError(f"bus {self._name!r} not open")
            task = ThreadBasedCyclicSendTask(bus, self._send_lock, msg, period_s, autostart=False)
            task.on_error = partial(self._on_cyclic_error, task)
            self._tasks.add(task)
            # Started under the lock so _close_bus can't run between
            # registering the task and starting it.
            task.start()
        return task

    def stop_periodic(self, task: ThreadBasedCyclicSendTask) -> None:
        """Stop a task from :meth:`send_periodic`. Idempotent."""
        task.stop()
        self._tasks.discard(task)

    def _on_cyclic_error(self, task: ThreadBasedCyclicSendTask, exc: Exception) -> bool:
        # A task we stopped can still raise once, from a send that raced the
        # stop; that is not a failure worth reporting.
        if not task.stopped:
            self.cyclic_failed.emit(task.arbitration_id, str(exc))
        return False

    def _flush_tx(self, bus: can.BusABC) -> None:
        """Discard queued transmits. Worker thread only, send lock held.

        IXXAT is left out on purpose: its flush waits INFINITE.
        """
        if self._config.listen_only:
            return
        try:
            if self._config.interface == "pcan":
                cast(Any, bus).reset()
            elif self._config.interface == "kvaser":
                bus.flush_tx_buffer()
        except Exception:
            _log.warning("TX flush on %r failed", self._name, exc_info=True)

    def _close_bus(self) -> None:
        """Stop every task and shut the bus down. Never raises.

        The tasks aren't joined: a sleeping task can't be woken and its
        period may be up to 60 s. A task that wakes later sees ``stopped``
        and exits, but one already blocked on the send lock makes one last
        send on the shut-down bus once the lock is free. Its error isn't
        reported, since the task is stopped.
        """
        # Senders check this before taking the lock, so none of them waits
        # out a slow vendor shutdown: the GUI thread starting a task while
        # a worker that failed on its own is still closing, above all.
        self._stop = True
        with self._send_lock:
            tasks, self._tasks = self._tasks, set()
            for task in tasks:
                task.stop()
            bus, self._bus = self._bus, None
            if bus is None:
                return
            # PCAN's Uninitialize already discards the queue; Kvaser would
            # spend up to 100 ms of shutdown trying to send it.
            if self._config.interface == "kvaser":
                self._flush_tx(bus)
            try:
                bus.shutdown()
            except Exception:
                _log.warning("Shutdown of bus %r failed", self._name, exc_info=True)

    def _open_bus(self) -> can.BusABC:
        kwargs: dict[str, Any] = {
            "interface": self._config.interface,
            "channel": self._config.channel,
            "bitrate": self._config.bitrate,
            # Loop the app's own transmits (TX window, Replay, MCP) back into
            # this bus's recv() so they appear in the Trace tagged TX. Without
            # this, a single-bus setup shows no evidence of its own sends.
            "receive_own_messages": True,
        }
        if self._config.fd:
            kwargs["fd"] = True
        if self._config.listen_only:
            _apply_listen_only_kwargs(kwargs, self._config.interface)
        return can.Bus(**kwargs)


def _apply_listen_only_kwargs(kwargs: dict[str, Any], interface: str) -> None:
    """Translate ``listen_only=True`` to the backend's native kwarg.

    Each python-can backend names its silent/passive mode differently:
    PCAN takes ``state=BusState.PASSIVE``, Kvaser takes ``driver_mode=False``
    (``DRIVER_MODE_SILENT``), cantact takes ``monitor=True``. The
    ``virtual`` backend has no transceiver and intentionally falls through
    with no kwarg — the app-level guard in ``BusReader.send_message`` covers
    that case. ``socketcan`` and ``ixxat`` never reach here: they have no
    python-can silent kwarg, so ``BusConfig`` rejects them at validation
    (see ``LISTEN_ONLY_INTERFACES``).
    """
    if interface == "pcan":
        kwargs["state"] = can.BusState.PASSIVE
    elif interface == "kvaser":
        kwargs["driver_mode"] = False
    elif interface == "cantact":
        kwargs["monitor"] = True


class BusController(QObject):
    """Lifecycle wrapper: owns one ``QThread`` and one ``BusWorker``."""

    error = Signal(str)
    started = Signal()
    stopped = Signal()
    frame_count = Signal(int)
    cyclic_failed = Signal(int, str)

    def __init__(
        self,
        config: BusConfig,
        ring: RingBuffer,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = BusWorker(config.name, config, ring)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.start_capture)
        # DirectConnection so the worker thread itself calls thread.quit()
        # synchronously when capture stops. Otherwise the queued slot would
        # be delivered to the main thread, which is blocked in thread.wait()
        # during BusController.stop().
        self._worker.stopped.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._worker.error.connect(self.error)
        self._worker.started.connect(self.started)
        self._worker.stopped.connect(self.stopped)
        self._worker.frame_count.connect(self.frame_count)
        self._worker.cyclic_failed.connect(self.cyclic_failed)

    @property
    def name(self) -> str:
        return self._worker.name

    @property
    def worker(self) -> BusWorker:
        return self._worker

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait_ms: int = 2000) -> bool:
        """Signal stop and wait for the thread to exit. Returns whether it did."""
        self._worker.request_stop()
        return self._thread.wait(wait_ms)
