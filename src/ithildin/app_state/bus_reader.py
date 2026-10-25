# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Application-wide multi-bus reader.

Holds a dict of ``BusController`` keyed by bus name; one ``QTimer``
fans out frames from every ring at ~60 Hz batched into a single
``frames_received(list[FrameRecord])`` emission. ``FrameRecord`` already
carries ``bus_name`` per frame, so consumers that decode per-frame
work unchanged across multiple live buses.

Single-bus convenience properties (``bus_name``, ``ring``,
``last_config``) return the value when exactly one bus is open and
``None`` otherwise — they exist so legacy single-bus call sites
(MCP tools, simple scripts) keep working without explicit selection.
Multi-bus callers should use ``bus_names()`` / ``rings`` /
``last_configs()``.
"""

from __future__ import annotations

import logging
import threading

import can
from can.broadcastmanager import ThreadBasedCyclicSendTask
from PySide6.QtCore import QObject, QTimer, Signal

from ithildin.core.bus import BusController, BusWorker
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord
from ithildin.workspace.schema import BusConfig

_log = logging.getLogger(__name__)

_RING_CAPACITY = 1 << 16
_FANOUT_HZ = 60


class BusReadOnlyError(RuntimeError):
    """Raised when a transmit is attempted on a listen-only bus.

    The bus was opened with ``listen_only=True``; both the hardware
    (when supported) and this app-level guard refuse to send.
    """


class BusReader(QObject):
    """Multi-bus capture singleton with a 60 Hz fan-out timer."""

    frames_received = Signal(list)
    bus_started = Signal(str)  # bus name
    bus_stopped = Signal(str)
    # Fires only when the worker died on its own (hardware unplug,
    # backend error). Carries the last error message. Explicit close()
    # never triggers this — see _close_one which clears _last_errors.
    bus_failed = Signal(str, str)  # bus_name, error_message
    # A periodic task died on a send error and has stopped.
    cyclic_failed = Signal(str, int, str)  # bus_name, arbitration_id, error
    # A send_message_async one-shot failed. Emitted from the send thread.
    send_failed = Signal(str, int, str)  # bus_name, arbitration_id, error
    error = Signal(str)
    # Carries the new t0 (a float) when a session starts, or ``None``
    # when the last bus closes. ``Signal(object)`` is how PySide6
    # carries an optional value.
    session_t0_changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._controllers: dict[str, BusController] = {}
        self._rings: dict[str, RingBuffer] = {}
        self._cursors: dict[str, int] = {}
        # Most recently opened config per bus name. Outlives close() so
        # the project save snapshot can recall what hardware was used.
        self._last_configs: dict[str, BusConfig] = {}
        # Most recent error per bus, captured between open() and the
        # worker's stopped signal. Drives the bus_failed dispatch.
        self._last_errors: dict[str, str] = {}
        # Anchor for "elapsed" time across the UI. Set on the first
        # frame received while no buses had been open; reset to None
        # once the last bus closes. Views read this lazily to convert
        # python-can epoch seconds into relative seconds.
        self._session_t0: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, 1000 // _FANOUT_HZ))
        self._timer.timeout.connect(self._fan_out)

    # ---- Inspection ----

    @property
    def is_open(self) -> bool:
        return bool(self._controllers)

    @property
    def bus_name(self) -> str | None:
        """The single open bus's name when exactly one is open, else ``None``.

        Single-bus convenience for callers that pre-date the multi-bus
        refactor (MCP tools, simple read paths). Multi-bus callers
        should use :meth:`bus_names`.
        """
        # One snapshot: MCP and Replay threads read this while the GUI
        # thread opens and closes buses.
        names = list(self._controllers)
        return names[0] if len(names) == 1 else None

    def bus_names(self) -> list[str]:
        """List of currently-open bus names, in insertion order."""
        return list(self._controllers.keys())

    @property
    def ring(self) -> RingBuffer | None:
        """The single open ring when exactly one bus is open, else ``None``.

        Single-bus convenience matching :attr:`bus_name`. Multi-bus
        callers (Trace, Record) use :attr:`rings`.
        """
        rings = list(self._rings.values())
        return rings[0] if len(rings) == 1 else None

    @property
    def rings(self) -> dict[str, RingBuffer]:
        """Live ``{bus_name: RingBuffer}`` mapping for every open bus."""
        return dict(self._rings)

    @property
    def last_config(self) -> BusConfig | None:
        """The single most-recently-opened config, when exactly one was used.

        Returns ``None`` if no bus has ever been opened, or if multiple
        distinct configs have been opened in this session — in which
        case the caller should walk :meth:`last_configs`.
        """
        if len(self._last_configs) == 1:
            return next(iter(self._last_configs.values()))
        return None

    def last_configs(self) -> list[BusConfig]:
        """Every config opened in this session (one per bus name)."""
        return list(self._last_configs.values())

    def last_config_for(self, name: str) -> BusConfig | None:
        """Return the most recent config used for ``name``, or ``None``.

        Survives close() — used by the Trace banner's Reconnect action
        to reopen a bus after its worker died on its own.
        """
        return self._last_configs.get(name)

    def is_listen_only(self, name: str) -> bool:
        """Return ``True`` iff the named bus was opened in listen-only mode.

        Unknown bus names return ``False`` — callers that care about
        existence should check :meth:`bus_names` separately.
        """
        cfg = self._last_configs.get(name)
        return bool(cfg and cfg.listen_only)

    @property
    def session_t0(self) -> float | None:
        """Epoch second of the first frame in the current capture session.

        ``None`` until the first batch of frames is fanned out after
        opening a bus from a fully-closed state. Subtract from any
        ``FrameRecord.timestamp`` to get elapsed seconds since bus
        start.
        """
        return self._session_t0

    # ---- Open / close ----

    def open(self, config: BusConfig) -> None:
        """Open a bus alongside any already-open ones.

        Additive in the multi-bus refactor: previously opening another
        bus auto-closed the existing one. Now ``open`` is idempotent on
        the bus *name* — if a controller is already running under that
        name, ``open`` is a no-op (it does not reconfigure on the fly).
        Caller must ``close(name)`` first if they want to reopen with a
        different ``BusConfig``.
        """
        if config.name in self._controllers:
            return
        ring = RingBuffer(_RING_CAPACITY)
        controller = BusController(config, ring, parent=self)
        name = config.name
        controller.error.connect(lambda msg, n=name: self._on_controller_error(n, msg))
        controller.started.connect(lambda n=name: self.bus_started.emit(n))
        controller.stopped.connect(lambda n=name: self._on_controller_stopped(n))
        controller.cyclic_failed.connect(
            lambda mid, err, n=name: self.cyclic_failed.emit(n, mid, err)
        )
        self._controllers[name] = controller
        self._rings[name] = ring
        self._cursors[name] = -1
        self._last_configs[name] = config
        self._last_errors.pop(name, None)
        controller.start()
        if not self._timer.isActive():
            self._timer.start()

    def open_all(self, configs: list[BusConfig]) -> None:
        """Open every config sequentially. Failures emit ``error`` and continue.

        Used by the toolbar's Connect button to bring up an entire
        ``project.workspace.bus`` list at once. One bad config (e.g.
        adapter not present) doesn't prevent the others from coming up.
        """
        for cfg in configs:
            try:
                self.open(cfg)
            except Exception as exc:  # pragma: no cover — defensive
                self.error.emit(f"Failed to open bus {cfg.name!r}: {exc}")

    def close(self, name: str | None = None, *, wait_ms: int = 2000) -> None:
        """Close one bus by name, or every open bus when ``name`` is ``None``."""
        if name is None:
            for bus_name in list(self._controllers.keys()):
                self._close_one(bus_name, wait_ms=wait_ms)
        else:
            self._close_one(name, wait_ms=wait_ms)
        # Drain anything still pending so consumers see a final batch.
        self._fan_out()
        if not self._controllers:
            self._timer.stop()

    def close_all(self, *, wait_ms: int = 2000) -> None:
        """Convenience alias for ``close(name=None)``."""
        self.close(None, wait_ms=wait_ms)

    def _close_one(self, name: str, *, wait_ms: int) -> None:
        # Drop any pending error so the soon-to-arrive worker stopped
        # signal can't fire bus_failed for a bus the user is closing.
        self._last_errors.pop(name, None)
        controller = self._controllers.pop(name, None)
        self._rings.pop(name, None)
        self._cursors.pop(name, None)
        if controller is not None:
            controller.stop(wait_ms=wait_ms)
        if not self._controllers and self._session_t0 is not None:
            self._session_t0 = None
            self.session_t0_changed.emit(None)

    def _on_controller_error(self, name: str, msg: str) -> None:
        self._last_errors[name] = msg
        self.error.emit(msg)

    def _on_controller_stopped(self, name: str) -> None:
        """Worker exited. If close() hasn't already cleaned up, do it now.

        ``was_running=True`` here means the worker stopped on its own
        (no _close_one in between); in that case we may also emit
        ``bus_failed`` if an error was recorded for this bus.
        """
        was_running = name in self._controllers
        if was_running:
            self._controllers.pop(name, None)
            self._rings.pop(name, None)
            self._cursors.pop(name, None)
            if not self._controllers:
                self._timer.stop()
                if self._session_t0 is not None:
                    self._session_t0 = None
                    self.session_t0_changed.emit(None)
        last_error = self._last_errors.pop(name, None)
        if was_running and last_error is not None:
            self.bus_failed.emit(name, last_error)
        self.bus_stopped.emit(name)

    # ---- Send ----

    def send_message(self, msg: object, bus_name: str | None = None) -> None:
        """Send one frame on the named bus. Callable from any thread.

        ``bus_name`` is required when multiple buses are open. When
        omitted and exactly one bus is open, that bus is used —
        preserving the single-bus call shape used by MCP write tools
        and the Replay window. Raises :class:`ValueError` if the
        target is ambiguous and :class:`RuntimeError` if no bus is
        open or the named bus isn't running. Blocks for the backend
        send, serialised with every other sender on that bus.
        """
        names = list(self._controllers)
        if not names:
            raise RuntimeError("No bus open; cannot send")
        if bus_name is None:
            if len(names) > 1:
                raise ValueError(
                    f"Multiple buses open; pass bus_name to disambiguate ({sorted(names)!r})"
                )
            bus_name = names[0]
        worker = self._writable_worker(bus_name)
        if not isinstance(msg, can.Message):
            raise TypeError(f"expected can.Message, got {type(msg).__name__}")
        worker.send(msg)

    def send_message_async(self, msg: can.Message, bus_name: str) -> None:
        """Send one frame without blocking the caller (the TX window's one-shots).

        "Not open" and "listen-only" raise here, synchronously. The send
        itself runs on a short-lived thread, since a jammed adapter can
        block it; a failure there emits :attr:`send_failed`.
        """
        worker = self._writable_worker(bus_name)
        threading.Thread(
            target=self._send_detached,
            args=(worker, bus_name, msg),
            name=f"One-shot send on {bus_name}",
            daemon=True,
        ).start()

    def _send_detached(self, worker: BusWorker, bus_name: str, msg: can.Message) -> None:
        try:
            worker.send(msg)
        except Exception as exc:
            _log.warning(
                "One-shot send of 0x%X on %r failed", msg.arbitration_id, bus_name, exc_info=True
            )
            self.send_failed.emit(bus_name, msg.arbitration_id, str(exc))

    def start_periodic(
        self, msg: object, bus_name: str, period_s: float
    ) -> ThreadBasedCyclicSendTask:
        """Start a cyclic transmit on the named bus. Returns the python-can task.

        Listen-only buses raise :class:`BusReadOnlyError` without
        starting the task — same gate as :meth:`send_message`. A send
        error later stops the task and emits :attr:`cyclic_failed`.
        """
        if not isinstance(msg, can.Message):
            raise TypeError(f"expected can.Message, got {type(msg).__name__}")
        return self._writable_worker(bus_name).send_periodic(msg, period_s)

    def stop_periodic(self, bus_name: str, task: ThreadBasedCyclicSendTask) -> None:
        """Stop a task from :meth:`start_periodic`. Idempotent; never raises."""
        controller = self._controllers.get(bus_name)
        if controller is None:
            task.stop()
        else:
            controller.worker.stop_periodic(task)

    def flush_tx(self, bus_name: str) -> None:
        """Ask the bus's worker to discard the adapter's queued transmits.

        No-op for a closed bus. The worker skips IXXAT and listen-only buses.
        """
        controller = self._controllers.get(bus_name)
        if controller is not None:
            controller.worker.request_flush()

    def _writable_worker(self, bus_name: str) -> BusWorker:
        controller = self._controllers.get(bus_name)
        if controller is None:
            raise RuntimeError(f"Bus {bus_name!r} is not open")
        if self.is_listen_only(bus_name):
            raise BusReadOnlyError(f"Bus {bus_name!r} is listen-only")
        return controller.worker

    # ---- Fan-out ----

    def _fan_out(self) -> None:
        if not self._rings:
            return
        # Defensive copy so each subscriber owns its own list and can
        # mutate FrameRecord.decoded without surprising others.
        batch: list[FrameRecord] = []
        for bus_name, ring in self._rings.items():
            cursor = self._cursors.get(bus_name, -1)
            frames, _dropped = ring.read_since(cursor)
            if not frames:
                continue
            self._cursors[bus_name] = frames[-1].seq
            batch.extend(frames)
        if batch:
            if self._session_t0 is None:
                self._session_t0 = batch[0].timestamp
                self.session_t0_changed.emit(self._session_t0)
            self.frames_received.emit(batch)
