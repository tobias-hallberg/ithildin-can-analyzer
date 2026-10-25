# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""python-can capture worker.

Threading model: a ``BusWorker`` is a ``QObject`` moved onto its own
``QThread``. The worker drives a polling-timeout ``recv()`` loop so shutdown
is just a flag flip — no cross-thread ``Bus.shutdown()`` race, no relying on
backend-specific behaviour to interrupt a blocking C call.

A ``BusController`` wraps the QThread + worker pair to give the rest of the
app one thing to start/stop.
"""

from __future__ import annotations

import logging
from typing import Any

import can
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
        self._bus: can.BusABC | None = None

    @property
    def name(self) -> str:
        return self._name

    @Slot()
    def start_capture(self) -> None:
        try:
            self._bus = self._open_bus()
        except Exception as exc:
            _log.warning("Failed to open bus %r", self._name, exc_info=True)
            self.error.emit(f"Failed to open bus {self._name!r}: {exc}")
            self.stopped.emit()
            return

        self.started.emit()
        count = 0
        try:
            while not self._stop:
                msg = self._bus.recv(timeout=_RECV_TIMEOUT_S)
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
            if self._bus is not None:
                try:
                    self._bus.shutdown()
                except Exception:
                    _log.warning("Shutdown of bus %r failed", self._name, exc_info=True)
                self._bus = None
            self.stopped.emit()

    @Slot()
    def request_stop(self) -> None:
        """Cross-thread safe: just sets a bool flag.

        The capture loop checks it between ``recv()`` polls (every
        ``_RECV_TIMEOUT_S`` worst case).
        """
        self._stop = True

    def send(self, msg: can.Message) -> None:
        """Send one frame on this bus. Must be invoked on the worker thread.

        Higher layers (replay engine, MCP bridge) should marshal sends
        through Qt queued connections so they execute here.
        """
        if self._bus is None:
            raise RuntimeError(f"bus {self._name!r} not open")
        self._bus.send(msg)

    def send_periodic(
        self, msg: can.Message, period_s: float
    ) -> can.broadcastmanager.CyclicSendTaskABC:
        """Start a periodic cyclic transmit. Returns the python-can task handle.

        The caller owns the returned task — keeps it alive, calls
        ``modify_data(msg)`` to update the payload mid-cycle, and
        ``stop()`` to end transmission. python-can's broadcast manager
        is internally thread-safe so this is callable from any thread.
        """
        if self._bus is None:
            raise RuntimeError(f"bus {self._name!r} not open")
        return self._bus.send_periodic(msg, period_s, store_task=True)

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
