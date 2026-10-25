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
from typing import cast

import can
from can.broadcastmanager import ThreadBasedCyclicSendTask
from PySide6.QtCore import QDeadlineTimer, QObject, QTimer, Signal, Slot

from ithildin.core.bus import BusController, BusWorker
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import BusHealth, FrameRecord
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
    # open() registered the bus; bus_started follows once the adapter is up.
    bus_opening = Signal(str)  # bus name
    bus_started = Signal(str)
    # Exactly once per open, when the name leaves bus_names(): inside
    # close(), or when the worker exits on its own. The ring's tail has
    # been fanned out by then.
    bus_stopped = Signal(str)
    # Fires only when the worker died on its own (hardware unplug,
    # backend error), just before bus_stopped. Carries the last error
    # message. Explicit close() never triggers this.
    bus_failed = Signal(str, str)  # bus_name, error_message
    # A bus whose close timed out has finally let go of its adapter; the
    # name and channel can be opened again.
    bus_released = Signal(str)
    # A periodic task died on a send error and has stopped.
    cyclic_failed = Signal(str, int, str)  # bus_name, arbitration_id, error
    # A send_message_async one-shot failed. Emitted from the send thread.
    send_failed = Signal(str, int, str)  # bus_name, arbitration_id, error
    # The worker's view of an open bus changed; see health().
    bus_health_changed = Signal(str, str, str)  # bus_name, BusHealth value, detail
    error = Signal(str)
    # ``None`` from open() when a new capture session starts: views drop
    # the previous session's data then, and only then. The new t0 (a
    # float) follows with the session's first batch. ``Signal(object)``
    # is how PySide6 carries an optional value.
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
        # Health per bus: set to OK on bus_started, absent while opening.
        # LOST outlives the failure that follows it, so the UI can say why
        # the bus went away; open(), close(name) and forget() clear it.
        self._health: dict[str, tuple[BusHealth, str]] = {}
        # Controllers whose close timed out, with the config they hold the
        # adapter for. Kept until their thread exits; open() refuses their
        # name and channel meanwhile.
        self._retiring: dict[str, tuple[BusController, BusConfig]] = {}
        # Anchor for "elapsed" time across the UI, set on the session's
        # first frame. A session ends only when close() leaves no bus open;
        # a failure keeps it, so a Reconnect continues the same time base.
        # Views read t0 lazily to convert epoch seconds into relative ones.
        self._session_t0: float | None = None
        self._session_ended = True
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

    def is_closing(self, name: str) -> bool:
        """Whether the named bus's close timed out and its thread still runs."""
        return name in self._retiring

    def closing_names(self) -> list[str]:
        """Buses whose close timed out and whose thread still runs, forgotten or not."""
        return list(self._retiring)

    def health(self, name: str) -> tuple[BusHealth, str] | None:
        """``(state, detail)`` of the named bus; ``None`` while it is opening or closed.

        A bus lost with its adapter keeps ``LOST`` after closing, until it is
        opened again, closed by name or forgotten. Any state but ``OK`` counts as
        degraded.
        Callable from any thread.
        """
        return self._health.get(name)

    @property
    def session_t0(self) -> float | None:
        """Epoch second of the first frame in the current capture session.

        ``None`` from the open() that starts a session until its first
        batch is fanned out. Kept after the session ends, so the data
        views still hold stays readable. Subtract from any
        ``FrameRecord.timestamp`` to get elapsed seconds since bus start.
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
        different ``BusConfig``. Refused, with ``error``, while a bus of
        that name or on that channel is still closing. Starts a new capture
        session (``session_t0_changed(None)``) when the last one ended.
        """
        name = config.name
        if name in self._controllers:
            return
        holder = next(
            (
                n
                for n, (_controller, c) in self._retiring.items()
                if n == name or (c.interface, c.channel) == (config.interface, config.channel)
            ),
            None,
        )
        if holder is not None:
            if holder == name:
                reason = f"Bus {name!r} is still closing: its adapter has not released the channel"
            else:
                reason = (
                    f"Bus {name!r} can't open: bus {holder!r} on the same channel is still "
                    "closing and its adapter has not released the channel"
                )
            self.error.emit(f"{reason} yet. Try again once it has, or unplug the adapter.")
            return
        if self._session_ended:
            self._session_ended = False
            self._session_t0 = None
            self.session_t0_changed.emit(None)
        ring = RingBuffer(_RING_CAPACITY)
        controller = BusController(config, ring, parent=self)
        # The slots tell controllers apart by sender(), not by bus name: a
        # late signal from a closed controller must not act on a bus reopened
        # under the same name. Bound methods, not lambdas capturing self: a
        # deleted controller would release the last reference to this
        # reader mid-destruction.
        controller.error.connect(self._on_controller_error)
        controller.started.connect(self._on_controller_started)
        controller.stopped.connect(self._on_controller_stopped)
        controller.finished.connect(self._on_controller_finished)
        controller.cyclic_failed.connect(self._on_controller_cyclic_failed)
        controller.health.connect(self._on_controller_health)
        self._controllers[name] = controller
        self._rings[name] = ring
        self._cursors[name] = -1
        self._last_configs[name] = config
        self._last_errors.pop(name, None)
        self._health.pop(name, None)
        controller.start()
        if not self._timer.isActive():
            self._timer.start()
        self.bus_opening.emit(name)

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
        """Close one bus by name, or every open bus when ``name`` is ``None``.

        Every target is asked to stop first, then all are waited on against
        one ``wait_ms`` deadline. The ring tails are fanned out and
        ``bus_stopped`` fires for each before this returns. A bus that
        misses the deadline still counts as closed, but stays closing
        (``is_closing``) until its thread exits, then ``bus_released``.
        Closing by name also clears a held LOST health. Leaving no bus open
        ends the capture session, and the next open() starts a new one;
        closing by name a bus that isn't open leaves the session alone.
        """
        targets = [(n, c) for n, c in self._controllers.items() if name in (None, n)]
        for _n, c in targets:
            c.request_stop()
        deadline = QDeadlineTimer(wait_ms)
        stuck = [(n, c) for n, c in targets if not c.wait(deadline)]
        self._fan_out()
        if name is not None:
            self._health.pop(name, None)
        for n, _c in targets:
            self._health.pop(n, None)
            self._drop(n)
        if (targets or name is None) and not self._controllers:
            self._session_ended = True
        for n, c in stuck:
            self._retiring[n] = (c, self._last_configs[n])
            _log.warning("Bus %r did not stop within %.1f s", n, wait_ms / 1000)
            self.error.emit(
                f"Bus {n!r} did not stop within {wait_ms / 1000:.1f} s: its adapter "
                "has not released the channel. It can be opened again once it does."
            )
        for n, _c in targets:
            self.bus_stopped.emit(n)

    def close_all(self, *, wait_ms: int = 2000) -> None:
        """Convenience alias for ``close(name=None)``."""
        self.close(None, wait_ms=wait_ms)

    def reopen(self, config: BusConfig) -> None:
        """Close the bus named in ``config`` and open it again, in the same session.

        For a Reconnect after a failure or bus-off: views keep their data
        and t0, where a plain close + open of the last bus would start a
        new session. Callers pass the workspace's config for the bus.
        """
        ended = self._session_ended
        self.close(config.name)
        self._session_ended = ended
        self.open(config)

    def shutdown(self, *, wait_ms: int = 2000) -> list[str]:
        """Close every bus for quit; return the names whose thread still runs.

        Waits ``wait_ms`` for the open buses, then up to ``wait_ms`` more
        for those still closing. A running thread must not be destroyed
        (Qt aborts the process), so the caller has to exit around it.
        """
        self.close(wait_ms=wait_ms)
        deadline = QDeadlineTimer(wait_ms)
        return [n for n, (c, _cfg) in self._retiring.items() if not c.wait(deadline)]

    def forget(self, name: str | None = None) -> None:
        """Drop what is remembered about a closed bus, or about every bus when
        ``name`` is ``None``: the config that Reconnect and the project
        snapshot reuse, and a held LOST health. For buses that left the
        project; call after :meth:`close`.
        """
        names = list(self._last_configs) if name is None else [name]
        for n in names:
            self._last_configs.pop(n, None)
            self._health.pop(n, None)

    def _drop(self, name: str) -> str | None:
        """Unregister a running bus; returns its last error, if any."""
        del self._controllers[name]
        self._rings.pop(name, None)
        self._cursors.pop(name, None)
        last_error = self._last_errors.pop(name, None)
        if not self._controllers:
            self._timer.stop()
        return last_error

    def _sender_if_current(self) -> BusController | None:
        """The controller that sent the signal being handled, if still registered."""
        controller = cast(BusController, self.sender())
        return controller if self._controllers.get(controller.name) is controller else None

    @Slot()
    def _on_controller_started(self) -> None:
        controller = self._sender_if_current()
        if controller is None:
            return
        self._health[controller.name] = (BusHealth.OK, "")
        self.bus_started.emit(controller.name)

    @Slot(str, str)
    def _on_controller_health(self, state: str, detail: str) -> None:
        controller = self._sender_if_current()
        if controller is None:
            return
        self._health[controller.name] = (BusHealth(state), detail)
        self.bus_health_changed.emit(controller.name, state, detail)

    @Slot(int, str)
    def _on_controller_cyclic_failed(self, message_id: int, error: str) -> None:
        controller = self._sender_if_current()
        if controller is not None:
            self.cyclic_failed.emit(controller.name, message_id, error)

    @Slot(str)
    def _on_controller_error(self, msg: str) -> None:
        controller = self._sender_if_current()
        if controller is not None:
            self._last_errors[controller.name] = msg
        self.error.emit(msg)

    @Slot()
    def _on_controller_stopped(self) -> None:
        """The worker exited on its own; close() has dealt with every other stop."""
        controller = self._sender_if_current()
        if controller is None:
            return
        name = controller.name
        self._fan_out()
        health = self._health.get(name)
        if health is not None and health[0] is not BusHealth.LOST:
            del self._health[name]
        last_error = self._drop(name)
        if last_error is not None:
            self.bus_failed.emit(name, last_error)
        self.bus_stopped.emit(name)

    @Slot()
    def _on_controller_finished(self) -> None:
        controller = cast(BusController, self.sender())
        name = controller.name
        entry = self._retiring.get(name)
        if entry is not None and entry[0] is controller:
            del self._retiring[name]
            _log.info("Bus %r released its adapter", name)
            self.bus_released.emit(name)
        controller.deleteLater()

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
