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

The worker also judges the bus's health. A receive error the backend can
recover from (error passive, bus-off) is classified instead of ending the
capture, a 0.5 s tick probes the adapter and measures the error-frame rate,
and ``health(state, detail)`` fires when the result changes. Error frames
are sampled into the ring, at most ``1 / _ERR_SAMPLE_S`` per second, each
carrying how many it stands for.

A ``BusController`` wraps the QThread + worker pair to give the rest of the
app one thing to start/stop.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import sys
import threading
import time
from functools import partial
from typing import Any, cast

import can
from can.broadcastmanager import ThreadBasedCyclicSendTask
from PySide6.QtCore import QDeadlineTimer, QObject, Qt, QThread, Signal, Slot

from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import HEALTH_RANK, BusHealth, FrameRecord
from ithildin.workspace.schema import BusConfig

_log = logging.getLogger(__name__)

_RECV_TIMEOUT_S = 0.1
"""Worst-case shutdown latency. Bus polls recv() at this cadence and checks
the stop flag between iterations."""

_HEALTH_TICK_S = 0.5
_ERR_SAMPLE_S = 0.1
_ERR_FLOOD_PER_S = 50
_CLEAN_TICKS = 2
"""Consecutive clean ticks before a degraded bus reads healthy again."""
_STICKY_CLEAR_S = 1.0
"""A data frame this long after the last error frame clears a SocketCAN
controller state."""

# SocketCAN error frames: class bits in the ID, controller state in data[1].
_CAN_ERR_CRTL = 0x04
_CAN_ERR_ACK = 0x20
_CAN_ERR_BUSOFF = 0x40
_CAN_ERR_RESTARTED = 0x100
_CRTL_WARNING = 0x0C
_CRTL_PASSIVE = 0x30
_CRTL_ACTIVE = 0x40

_Level = tuple[BusHealth, str]
_OK: _Level = (BusHealth.OK, "")
_LOST: _Level = (BusHealth.LOST, "")
_PCAN_WARNING: _Level = (BusHealth.ERRORS, "error warning")

# python-can's IXXAT recv raises these after a poll without data, while the
# controller status shows them (canlib_vcinpl.py, _recv_internal).
_IXXAT_LEVELS: dict[str, _Level] = {
    "Error warning limit exceeded": (BusHealth.ERRORS, "error warning"),
    "Bus coupling error": (BusHealth.ERRORS, "bus coupling error"),
    "Bus off status": (BusHealth.BUS_OFF, ""),
    "Data overrun occurred": _OK,
}

# Kvaser canERR_HARDWARE / canERR_NOCARD.
_KVASER_LOST_CODES = (-15, -26)


class _AdapterLostError(Exception):
    """The health probe found the adapter gone; ends the capture as a failure."""


class _OpenCancelledError(Exception):
    """The bus was stopped while its open waited for the driver."""


class BusWorker(QObject):
    """Owns a python-can ``BusABC`` and pushes frames into a ``RingBuffer``."""

    started = Signal()
    stopped = Signal()
    error = Signal(str)
    frame_count = Signal(int)
    cyclic_failed = Signal(int, str)  # arbitration_id, error
    health = Signal(str, str)  # BusHealth value, detail

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
        # Health state, touched on the worker thread only.
        self._level: _Level = _OK
        self._raised: _Level | None = None
        self._sticky: _Level = _OK
        self._sticky_t = 0.0
        self._err_tick = 0
        self._err_unsampled = 0
        self._err_sample_t = float("-inf")
        self._clean_ticks = 0
        self._next_tick = 0.0

    @property
    def name(self) -> str:
        return self._name

    @Slot()
    def start_capture(self) -> None:
        count = 0
        # BaseException at this boundary: vendor bindings can raise panics
        # and SystemExit. Report them through ``error``, so the reader sees a
        # failure, instead of letting them escape the slot.
        try:
            try:
                bus = self._open_bus()
            except _OpenCancelledError:
                return
            except BaseException as exc:
                _log.warning("Failed to open bus %r", self._name, exc_info=True)
                self.error.emit(f"Failed to open bus {self._name!r}: {exc}")
                return
            with self._send_lock:
                self._bus = bus
            # Stopped during a slow vendor open: never report it as started.
            if self._stop:
                return
            self._next_tick = time.monotonic() + _HEALTH_TICK_S
            self.started.emit()
            while not self._stop:
                if self._flush_requested:
                    self._flush_requested = False
                    with self._send_lock:
                        self._flush_tx(bus)
                try:
                    msg = bus.recv(timeout=_RECV_TIMEOUT_S)
                except can.CanOperationError as exc:
                    self._note_recv_error(bus, exc)
                    msg = None
                now = time.monotonic()
                # Before the None check, so an idle or silently dead bus
                # still ticks.
                if now >= self._next_tick:
                    self._health_tick(bus, now)
                if msg is None:
                    continue
                coalesced = 1
                if msg.is_error_frame:
                    coalesced = self._count_error_frame(msg, now)
                    if coalesced == 0:
                        continue
                elif self._sticky != _OK and now - self._sticky_t >= _STICKY_CLEAR_S:
                    self._sticky = _OK
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
                    coalesced=coalesced,
                )
                self._ring.append(rec)
                count += 1
                if count % 100 == 0:
                    self.frame_count.emit(count)
        except BaseException as exc:
            _log.warning("Capture error on %r", self._name, exc_info=True)
            self.error.emit(f"Capture error on {self._name!r}: {exc}")
        finally:
            try:
                self.frame_count.emit(count)
                self._close_bus()
            finally:
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

    # ---- Health (worker thread) ----

    def _note_recv_error(self, bus: can.BusABC, exc: can.CanOperationError) -> None:
        """Keep a recoverable receive error for the next tick; re-raise the rest."""
        level = self._recv_error_level(bus, exc)
        if level is None:
            raise exc
        if level[0] is BusHealth.LOST:
            self._set_health(level)
            raise exc
        if self._raised is None:
            _log.info("Receive error on %r: %s", self._name, exc)
            self._raised = level
        else:
            self._raised = _worst(self._raised, level)

    def _recv_error_level(self, bus: can.BusABC, exc: can.CanOperationError) -> _Level | None:
        """What a receive error says about the bus; ``None`` when it is fatal."""
        interface = self._config.interface
        if interface == "pcan":
            # PcanCanOperationError carries no code; the live status does.
            level = _pcan_level(cast(Any, bus).status())
            if level in (_OK, _PCAN_WARNING):
                # python-can never raises for error warning, so the status
                # doesn't explain this one; only an overrun is safe to read on.
                return _OK if _pcan_overrun(bus, exc) else None
            return level
        if interface == "ixxat":
            return _IXXAT_LEVELS.get(str(exc))
        if interface == "kvaser" and exc.error_code in _KVASER_LOST_CODES:
            return _LOST
        if interface == "gs_usb" and isinstance(exc.__cause__, OSError):
            # GsUsbBus's probe found the adapter gone.
            return _LOST
        return None

    def _probe(self, bus: can.BusABC) -> _Level:
        """Ask the adapter how it is. Catches a PCAN USB pull, after which
        ``recv`` only ever times out."""
        if self._config.interface == "pcan":
            return _pcan_level(cast(Any, bus).status())
        return _OK

    def _health_tick(self, bus: can.BusABC, now: float) -> None:
        self._next_tick = now + _HEALTH_TICK_S
        probe = self._probe(bus)
        if probe[0] is BusHealth.LOST:
            self._set_health(probe)
            raise _AdapterLostError("adapter disconnected")
        if probe == _PCAN_WARNING and not self._err_tick:
            # The controller holds error warning until it next transmits,
            # long after the bus is fixed (TX is disarmed by then). Only
            # errors still arriving make it count.
            probe = _OK
        flood = self._err_tick >= _ERR_FLOOD_PER_S * _HEALTH_TICK_S
        rate = (BusHealth.ERRORS, "error-frame flood") if flood else _OK
        # Ties keep the first: the probe, then a raised error, then the
        # SocketCAN state, then the rate. Keeps the detail from flapping.
        level = _worst(probe, self._raised or _OK, self._sticky, rate)
        self._err_tick = 0
        self._raised = None
        if level[0] is BusHealth.OK and self._level[0] is not BusHealth.OK:
            self._clean_ticks += 1
            if self._clean_ticks < _CLEAN_TICKS:
                return
        self._clean_ticks = 0
        if level == self._level:
            return
        worsened = HEALTH_RANK[level[0]] > HEALTH_RANK[self._level[0]]
        self._set_health(level)
        if worsened:
            # Unacknowledged frames pile up in the adapter and would burst
            # out once the bus recovers. Flushing here covers MCP and Replay
            # frames too, not only the TX window's.
            with self._send_lock:
                self._flush_tx(bus)

    def _count_error_frame(self, msg: can.Message, now: float) -> int:
        """Count an error frame; how many the ring record stands for, 0 to drop it."""
        self._err_tick += 1
        self._err_unsampled += 1
        if self._config.interface in ("socketcan", "gs_usb"):
            level = _socketcan_level(msg)
            if level is not None:
                self._sticky = level
                self._sticky_t = now
        if now - self._err_sample_t < _ERR_SAMPLE_S:
            return 0
        coalesced, self._err_unsampled = self._err_unsampled, 0
        self._err_sample_t = now
        return coalesced

    def _set_health(self, level: _Level) -> None:
        self._level = level
        # INFO: the UI reports health changes itself.
        _log.info("Bus %r health: %s", self._name, level[0].describe(level[1]))
        self.health.emit(level[0].value, level[1])

    def _flush_tx(self, bus: can.BusABC) -> None:
        """Discard queued transmits. Worker thread only, send lock held.

        IXXAT is left out on purpose: its flush waits INFINITE.
        """
        if self._config.listen_only:
            return
        try:
            if self._config.interface == "pcan":
                cast(Any, bus).reset()
            elif self._config.interface in ("kvaser", "gs_usb"):
                bus.flush_tx_buffer()
        except BaseException:
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
            # PCAN's Uninitialize and gs_usb's stop already discard the
            # queue; Kvaser would spend up to 100 ms of shutdown sending it.
            if self._config.interface == "kvaser":
                self._flush_tx(bus)
            try:
                bus.shutdown()
            except BaseException:
                _log.warning("Shutdown of bus %r failed", self._name, exc_info=True)

    def _open_bus(self) -> can.BusABC:
        if self._config.interface == "gs_usb":
            from ithildin.core.gs_usb_bus import GsUsbBus

            return GsUsbBus(
                channel=int(self._config.channel),
                bitrate=self._config.bitrate,
                listen_only=self._config.listen_only,
            )
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
        if self._config.interface == "kvaser":
            return self._open_kvaser(kwargs)
        if self._config.interface == "pcan":
            return self._open_pcan(kwargs)
        return can.Bus(**kwargs)

    def _open_pcan(self, kwargs: dict[str, Any]) -> can.BusABC:
        from can.interfaces.pcan import basic

        bitrate = kwargs["bitrate"]
        if not kwargs.get("fd") and bitrate not in basic.PCAN_BITRATES:
            # PcanBus runs any other bitrate at 500 kbit/s without a word,
            # which puts error frames on a bus running at another rate.
            rates = ", ".join(str(rate) for rate in sorted(basic.PCAN_BITRATES))
            raise can.CanInitializationError(
                f"PCAN has no preset for {bitrate} bit/s (presets: {rates} bit/s)"
            )
        driver = cast(Any, basic.PCANBasic)()
        channel = kwargs["channel"]
        handle = channel if isinstance(channel, int) else basic.PCAN_CHANNEL_NAMES[channel]
        held = _pcan_initialised(driver, handle)
        try:
            bus = can.Bus(**kwargs)
        except BaseException:
            # PcanBus leaves the channel initialised when a step after
            # CAN_Initialize fails, or when Initialize returns CAUTION (the
            # channel opened, at another bitrate), and every later open of it
            # then fails until the app restarts. Release only a channel this
            # open initialised: when Initialize itself failed, the channel may
            # be another bus's in this process.
            if not held and _pcan_initialised(driver, handle):
                _log.info("Uninitialising the PCAN channel of %r after its open failed", self._name)
                driver.Uninitialize(handle)
            raise
        try:
            _finish_pcan_open(driver, handle, bus, listen_only=self._config.listen_only)
        except BaseException:
            bus.shutdown()
            raise
        return bus

    def _open_kvaser(self, kwargs: dict[str, Any]) -> can.BusABC:
        from can.interfaces.kvaser.canlib import BITRATE_FD, BITRATE_OBJS

        presets = BITRATE_FD if kwargs.get("fd") else BITRATE_OBJS
        bitrate = kwargs["bitrate"]
        if bitrate not in presets:
            # KvaserBus hands any other bitrate to CANlib raw, with no segment
            # times, after it opened the channel. A refusal there leaks that
            # handle, which keeps init access: later opens of the channel
            # can't set its bitrate or silent mode.
            rates = ", ".join(str(rate) for rate in sorted(presets))
            raise can.CanInitializationError(
                f"Kvaser has no preset for {bitrate} bit/s (presets: {rates} bit/s)"
            )
        # A channel number shifted by an unplugged adapter must fail to open,
        # not open a Kvaser Virtual channel that looks like a live bus.
        kwargs["accept_virtual"] = False
        deadline = time.monotonic() + _KVASER_OPEN_WAIT_S
        while not KVASER_ENUM_LOCK.acquire(timeout=_RECV_TIMEOUT_S):
            if self._stop:
                raise _OpenCancelledError
            if time.monotonic() >= deadline:
                raise can.CanInitializationError(
                    f"Kvaser driver busy: another Kvaser bus or adapter scan has been "
                    f"in the driver for over {_KVASER_OPEN_WAIT_S:.0f} s"
                )
        try:
            kvaser_enum_hardware()
            return can.Bus(**kwargs)
        finally:
            KVASER_ENUM_LOCK.release()


def _worst(*levels: _Level) -> _Level:
    return max(levels, key=lambda level: HEALTH_RANK[level[0]])


def _pcan_level(code: int) -> _Level:
    """Map a PCAN-Basic status code to a health level."""
    from can.interfaces.pcan import basic

    # The handle errors are values of one field, not bits: 0x0400 and
    # 0x0800 in that field are HWINUSE and NETINUSE.
    if (code & basic.PCAN_ERROR_ILLHANDLE) in (
        basic.PCAN_ERROR_ILLHW,
        basic.PCAN_ERROR_ILLNET,
        basic.PCAN_ERROR_ILLCLIENT,
    ):
        return _LOST
    if code & (
        basic.PCAN_ERROR_NODRIVER
        | basic.PCAN_ERROR_REGTEST
        | basic.PCAN_ERROR_INITIALIZE
        | basic.PCAN_ERROR_ILLOPERATION
    ):
        return _LOST
    if code & basic.PCAN_ERROR_BUSOFF:
        return (BusHealth.BUS_OFF, "")
    if code & basic.PCAN_ERROR_BUSPASSIVE:
        return (BusHealth.ERRORS, "error passive")
    if code & (basic.PCAN_ERROR_BUSLIGHT | basic.PCAN_ERROR_BUSHEAVY):
        return _PCAN_WARNING
    return _OK


def _pcan_initialised(driver: Any, handle: Any) -> bool:
    """Whether this process has the PCAN channel initialised: GetStatus
    reports PCAN_ERROR_INITIALIZE for a channel missing from the calling
    process's list."""
    from can.interfaces.pcan import basic

    return not driver.GetStatus(handle) & basic.PCAN_ERROR_INITIALIZE


def _finish_pcan_open(driver: Any, handle: Any, bus: Any, *, listen_only: bool) -> None:
    """Make a PCAN channel PcanBus just opened safe to read, or raise
    CanInitializationError."""
    from can.interfaces.pcan import basic

    # PcanBus doesn't check the status-message type (MSGTYPE 0x80), so a
    # status message can read as a data frame. Bus state comes from the
    # health tick's status() instead.
    result = driver.SetValue(handle, basic.PCAN_ALLOW_STATUS_FRAMES, basic.PCAN_PARAMETER_OFF)
    if result != basic.PCAN_ERROR_OK:
        text = bus._get_formatted_error(result)
        raise can.CanInitializationError(f"PCAN driver refused to turn off status frames: {text}")
    # Those queued since CAN_Initialize stay queued. Reset empties the queues
    # without touching the controller, and nothing has been sent yet.
    result = driver.Reset(handle)
    if result != basic.PCAN_ERROR_OK:
        text = bus._get_formatted_error(result)
        raise can.CanInitializationError(f"PCAN driver couldn't empty the receive queue: {text}")
    if not listen_only:
        return
    # PcanBus ignores the result of setting listen-only.
    result, value = driver.GetValue(handle, basic.PCAN_LISTEN_ONLY)
    if result != basic.PCAN_ERROR_OK:
        text = bus._get_formatted_error(result)
        raise can.CanInitializationError(f"PCAN driver couldn't confirm listen-only mode: {text}")
    if value != basic.PCAN_PARAMETER_ON:
        raise can.CanInitializationError(
            "PCAN channel did not enter listen-only mode, so it would ACK on the wire"
        )


def _pcan_overrun(bus: can.BusABC, exc: can.CanOperationError) -> bool:
    """Whether a PCAN receive error reports an overrun: frames were lost, but
    the next read works."""
    from can.interfaces.pcan import basic

    # The error holds only the driver's text for the code, made by this same
    # formatter (one line per bit when the code combines several).
    pcan = cast(Any, bus)
    return any(
        pcan._get_formatted_error(code) in str(exc)
        for code in (basic.PCAN_ERROR_OVERRUN, basic.PCAN_ERROR_QOVERRUN)
    )


def _socketcan_level(msg: can.Message) -> _Level | None:
    """The controller state a SocketCAN error frame reports, if any."""
    err_class = msg.arbitration_id
    crtl = msg.data[1] if len(msg.data) > 1 else 0
    if err_class & _CAN_ERR_BUSOFF:
        return (BusHealth.BUS_OFF, "")
    if err_class & _CAN_ERR_RESTARTED or (err_class & _CAN_ERR_CRTL and crtl & _CRTL_ACTIVE):
        return _OK
    if err_class & _CAN_ERR_CRTL and crtl & _CRTL_PASSIVE:
        return (BusHealth.ERRORS, "error passive")
    if err_class & _CAN_ERR_ACK:
        return (BusHealth.ERRORS, "no ACK")
    if err_class & _CAN_ERR_CRTL and crtl & _CRTL_WARNING:
        return (BusHealth.ERRORS, "error warning")
    return None


def _apply_listen_only_kwargs(kwargs: dict[str, Any], interface: str) -> None:
    """Translate ``listen_only=True`` to the backend's native kwarg.

    Each python-can backend names its silent/passive mode differently:
    PCAN takes ``state=BusState.PASSIVE``, Kvaser takes ``driver_mode=False``
    (``DRIVER_MODE_SILENT``) on a single handle; gs_usb never gets here,
    since ``GsUsbBus`` takes ``listen_only=`` itself. The ``virtual``
    backend has no transceiver and intentionally falls through with no
    kwarg — the app-level guard in ``BusReader.send_message`` covers that
    case.
    ``socketcan`` and ``ixxat`` never reach here: they have no python-can
    silent kwarg, so ``BusConfig`` rejects them at validation (see
    ``LISTEN_ONLY_INTERFACES``).
    """
    if interface == "pcan":
        kwargs["state"] = can.BusState.PASSIVE
    elif interface == "kvaser":
        # python-can sets silent mode on its write handle, a second handle
        # without init access, so CANlib ignores it and the adapter still
        # ACKs. With one handle it lands on the handle that has init access.
        # receive_own_messages would only make python-can warn on a single
        # handle, and a listen-only bus never transmits.
        kwargs.update(driver_mode=False, single_handle=True, receive_own_messages=False)


KVASER_ENUM_LOCK = threading.Lock()
"""Held across :func:`kvaser_enum_hardware` and every use of the channel
numbers it produces: a probe's detection, an open. Re-enumerating renumbers
the channels, so one between a detection and a ``canOpenChannel`` would
open a different adapter. Wait for it with a timeout: a vendor call that
hangs keeps holding it."""

_KVASER_OPEN_WAIT_S = 10.0
"""Longest a Kvaser open queues for :data:`KVASER_ENUM_LOCK` behind other
opens and probes before it fails as driver busy."""


def kvaser_enum_hardware() -> None:
    """Have CANlib re-enumerate the Kvaser devices. Windows only.

    Without it, CANlib on Windows keeps the channel list from its start, so
    an adapter plugged in later never appears and a replugged one can't be
    reopened until the app restarts. Hold :data:`KVASER_ENUM_LOCK`.
    """
    if sys.platform != "win32":
        return
    # Importing python-can's wrapper runs canInitializeLibrary, which CANlib
    # needs before any other call.
    importlib.import_module("can.interfaces.kvaser.canlib")
    try:
        canlib32 = ctypes.WinDLL("canlib32")
    except OSError:
        return
    # canlib.h's canEnumHardware isn't exported by the DLL, only this form.
    status = canlib32.canEnumHardwareEx(ctypes.byref(ctypes.c_int()))
    if status < 0:
        _log.warning("Kvaser canEnumHardwareEx failed with status %d", status)


class BusController(QObject):
    """Lifecycle wrapper: owns one ``QThread`` and one ``BusWorker``."""

    error = Signal(str)
    started = Signal()
    stopped = Signal()
    # The thread has exited and been joined; deleting the controller is safe.
    finished = Signal()
    frame_count = Signal(int)
    cyclic_failed = Signal(int, str)
    health = Signal(str, str)

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
        # during BusReader.close().
        self._worker.stopped.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._worker.error.connect(self.error)
        self._worker.started.connect(self.started)
        self._worker.stopped.connect(self.stopped)
        self._worker.frame_count.connect(self.frame_count)
        self._worker.cyclic_failed.connect(self.cyclic_failed)
        self._worker.health.connect(self.health)
        self._thread.finished.connect(self._on_thread_finished)

    @property
    def name(self) -> str:
        return self._worker.name

    @property
    def worker(self) -> BusWorker:
        return self._worker

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        """Ask the worker to stop without waiting for it."""
        self._worker.request_stop()

    def wait(self, deadline: QDeadlineTimer) -> bool:
        """Wait for the thread to exit, up to ``deadline``. Returns whether it did."""
        return self._thread.wait(deadline)

    def stop(self, wait_ms: int = 2000) -> bool:
        """Signal stop and wait for the thread to exit. Returns whether it did."""
        self.request_stop()
        return self.wait(QDeadlineTimer(wait_ms))

    @Slot()
    def _on_thread_finished(self) -> None:
        # The owner deletes the controller, and with it the QThread, from
        # ``finished``. Join first: QThread.finished is emitted while the
        # thread is still unwinding (see ReplayController).
        self._thread.wait()
        self.finished.emit()
