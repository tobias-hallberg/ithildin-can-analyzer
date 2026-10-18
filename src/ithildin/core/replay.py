# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Log-file replay engine.

Reads any python-can-supported log format and replays frames through a
caller-provided ``send`` callable with anchored timing. ``speed`` scales
inter-frame intervals (speed=2.0 plays back at twice original rate).

Drift handling: target wall-clock for frame N is anchored to a fixed
``start_real`` plus the scaled offset from the first frame's timestamp,
so loop-body cost does not accumulate.

The ``send`` callable lets the GUI route replay frames through whatever
bus the application is already using — typically ``BusReader.send_message``,
which forwards to the bus thread that owns the python-can ``Bus``. This
avoids opening a second ``Bus`` instance on a physical channel.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import can
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

SendCallable = Callable[[can.Message], None]
"""Anything that takes a single ``can.Message`` synchronously."""


class ReplayWorker(QObject):
    """Plays a log file through a caller-supplied send callable."""

    started = Signal()
    finished = Signal(int)  # total frames sent
    error = Signal(str)
    progress = Signal(int)  # frames sent so far

    def __init__(
        self,
        log_path: Path,
        send: SendCallable,
        speed: float = 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self._log_path = log_path
        self._send = send
        self._speed = speed
        self._stop = False

    @Slot()
    def start_replay(self) -> None:
        self.started.emit()
        sent = 0
        try:
            with can.LogReader(str(self._log_path)) as reader:
                start_real: float | None = None
                start_ts: float | None = None
                for msg in reader:
                    if self._stop:
                        break
                    if start_ts is None:
                        start_ts = msg.timestamp
                        start_real = time.monotonic()
                    else:
                        assert start_real is not None
                        target = start_real + (msg.timestamp - start_ts) / self._speed
                        delay = target - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                    self._send(msg)
                    sent += 1
                    if sent % 100 == 0:
                        self.progress.emit(sent)
        except Exception as exc:
            self.error.emit(f"Replay error: {exc}")
        finally:
            self.progress.emit(sent)
            self.finished.emit(sent)

    @Slot()
    def request_stop(self) -> None:
        self._stop = True


class ReplayController(QObject):
    """QThread + ReplayWorker lifecycle."""

    error = Signal(str)
    started = Signal()
    finished = Signal(int)
    progress = Signal(int)

    def __init__(
        self,
        log_path: Path,
        send: SendCallable,
        speed: float = 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sent = 0
        self._worker = ReplayWorker(log_path, send, speed)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start_replay)
        self._worker.finished.connect(self._on_worker_finished, Qt.ConnectionType.DirectConnection)
        self._worker.error.connect(self.error)
        self._worker.started.connect(self.started)
        self._worker.progress.connect(self.progress)
        self._thread.finished.connect(self._on_thread_finished)

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait_ms: int = 5000) -> bool:
        self._worker.request_stop()
        return self._thread.wait(wait_ms)

    @Slot(int)
    def _on_worker_finished(self, sent: int) -> None:
        self._sent = sent
        self._thread.quit()

    @Slot()
    def _on_thread_finished(self) -> None:
        # Consumers drop their last reference to this controller from their
        # `finished` slot, which deletes both the QThread and the worker that
        # lives on it. Join first: re-emitting straight off the worker's own
        # `finished` let that delete land while the thread was still unwinding
        # its C++ teardown, which is an access violation on Windows.
        self._thread.wait()
        self.finished.emit(self._sent)
