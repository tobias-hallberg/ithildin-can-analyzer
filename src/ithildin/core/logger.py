# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tee logger.

Pulls captured frames from one or more ``RingBuffer`` instances and writes
them through python-can's ``Logger`` (auto-format from the file extension —
.mf4, .blf, .asc, .csv, .log, .txt, .trc via python-can / asammdf).

Runs in its own thread so a slow disk does not back-pressure capture or the
GUI. Stop semantics match ``BusWorker``: a flag flip plus ``thread.wait``.

An optional ``admit`` predicate filters frames at write-time. Owners pass a
small closure that consults the live subscription state; when it returns
False the frame's ring cursor is still advanced (otherwise the next poll
would re-read it forever) but the frame isn't written, and a parallel
``dropped_count`` signal lets the UI surface what was skipped.

The rings follow the open buses through ``set_rings``: a ring that leaves
or is replaced (a reconnect) is drained to its end first, so one file
spans bus loss and Disconnect / Connect. Frames a ring overwrote before
they were written count as ``lost_count``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import can
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from ithildin.core.ringbuf import RingBuffer

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ithildin.core.types import FrameRecord

_log = logging.getLogger(__name__)

_IDLE_SLEEP_S = 0.05


class TeeWorker(QObject):
    """Drains ring buffers into a python-can ``Logger``."""

    started = Signal()
    stopped = Signal()
    error = Signal(str)
    frame_count = Signal(int)
    dropped_count = Signal(int)
    lost_count = Signal(int)

    def __init__(
        self,
        path: Path,
        rings: Mapping[str, RingBuffer],
        admit: Callable[[FrameRecord], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._rings = dict(rings)
        self._cursors: dict[str, int] = {}
        self._stop = False
        # Single-reference attributes; the GUI thread swaps them via
        # ``set_admit`` / ``set_rings`` (always a new dict) while the worker
        # thread reads each once per pass. Both ops are atomic under the
        # GIL — no lock required. ``_applied`` is the rings dict the worker
        # last took over.
        self._admit: Callable[[FrameRecord], bool] | None = admit
        self._wanted = self._applied = self._rings
        # Worker thread only.
        self._count = 0
        self._dropped = 0
        self._lost = 0

    def set_admit(self, admit: Callable[[FrameRecord], bool] | None) -> None:
        self._admit = admit

    def set_rings(self, rings: Mapping[str, RingBuffer]) -> None:
        self._wanted = dict(rings)

    @Slot()
    def start_logging(self) -> None:
        try:
            writer = can.Logger(str(self._path))
        except Exception as exc:
            _log.warning("Failed to open log file %s", self._path, exc_info=True)
            self.error.emit(f"Failed to open log file {self._path!s}: {exc}")
            self.stopped.emit()
            return

        # The backlog still in the rings is recorded; what they overwrote
        # before Start isn't lost to this recording.
        self._cursors = {
            name: max(-1, ring.total_seq - ring.capacity - 1) for name, ring in self._rings.items()
        }
        self.started.emit()
        try:
            while not self._stop:
                self._rebind(writer)
                wrote_any = False
                for name, ring in self._rings.items():
                    if self._drain(name, ring, writer):
                        wrote_any = True
                if not wrote_any:
                    time.sleep(_IDLE_SLEEP_S)
            # Flush remaining frames once more before closing.
            self._rebind(writer)
            for name, ring in self._rings.items():
                self._drain(name, ring, writer)
        except Exception as exc:
            _log.warning("Logger error writing %s", self._path, exc_info=True)
            self.error.emit(f"Logger error: {exc}")
        finally:
            try:
                writer.stop()
            except BaseException:
                _log.error("Failed to finalize log file %s", self._path, exc_info=True)
            finally:
                self.frame_count.emit(self._count)
                self.dropped_count.emit(self._dropped)
                self.lost_count.emit(self._lost)
                self.stopped.emit()

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    def _rebind(self, writer: can.Listener) -> None:
        """Take over the rings from ``set_rings``. A ring that left or was
        replaced is final once its bus stopped, so it is drained first."""
        wanted = self._wanted
        if wanted is self._applied:
            return
        for name, ring in self._rings.items():
            if wanted.get(name) is not ring:
                self._drain(name, ring, writer)
        self._cursors = {
            name: self._cursors[name] if self._rings.get(name) is ring else -1
            for name, ring in wanted.items()
        }
        self._rings = self._applied = wanted

    def _drain(self, name: str, ring: RingBuffer, writer: can.Listener) -> bool:
        """Write ``ring``'s pending frames; returns whether any was written."""
        frames, lost = ring.read_since(self._cursors[name])
        if lost:
            self._lost += lost
            self.lost_count.emit(self._lost)
        admit = self._admit
        wrote = False
        for f in frames:
            self._cursors[name] = f.seq
            if admit is not None and not admit(f):
                self._dropped += 1
                if self._dropped % 100 == 0:
                    self.dropped_count.emit(self._dropped)
                continue
            msg = can.Message(
                timestamp=f.timestamp,
                arbitration_id=f.arbitration_id,
                data=f.data,
                is_extended_id=f.is_extended_id,
                is_fd=f.is_fd,
                is_error_frame=f.is_error_frame,
                is_remote_frame=f.is_remote_frame,
                channel=name,
            )
            writer.on_message_received(msg)
            self._count += 1
            wrote = True
            if self._count % 100 == 0:
                self.frame_count.emit(self._count)
        return wrote


class TeeLogger(QObject):
    """Owns the QThread + TeeWorker for a single log destination."""

    error = Signal(str)
    started = Signal()
    # After the thread has exited and been joined, so a consumer may drop
    # its last reference to the logger from this signal.
    stopped = Signal()
    frame_count = Signal(int)
    dropped_count = Signal(int)
    lost_count = Signal(int)

    def __init__(
        self,
        path: Path,
        rings: Mapping[str, RingBuffer],
        admit: Callable[[FrameRecord], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = TeeWorker(path, rings, admit=admit)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start_logging)
        self._worker.stopped.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._worker.error.connect(self.error)
        self._worker.started.connect(self.started)
        self._worker.frame_count.connect(self.frame_count)
        self._worker.dropped_count.connect(self.dropped_count)
        self._worker.lost_count.connect(self.lost_count)
        self._thread.finished.connect(self._on_thread_finished)

    def set_admit(self, admit: Callable[[FrameRecord], bool] | None) -> None:
        self._worker.set_admit(admit)

    def set_rings(self, rings: Mapping[str, RingBuffer]) -> None:
        """Record from these rings from now on: the open buses' current ones."""
        self._worker.set_rings(rings)

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait_ms: int = 5000) -> bool:
        self._worker.request_stop()
        return self._thread.wait(wait_ms)

    @Slot()
    def _on_thread_finished(self) -> None:
        # Relaying the worker's own ``stopped`` let the consumer delete the
        # QThread while it was still unwinding (see ReplayController).
        self._thread.wait()
        self.stopped.emit()
