# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""CANable / CANtact / candleLight adapters, through python-can's gs_usb backend.

python-can's ``GsUsbBus`` has no listen-only mode, never notices an
unplugged adapter, stamps frames with the adapter's own microsecond
counter instead of epoch time, and reaches only the first CAN channel of
an adapter: the ``gs_usb`` library sends every control request to channel
0 and hands over every received frame whatever its channel. :class:`GsUsbBus`
fixes those and adds a TX flush and opening an adapter by its USB serial.

Every CAN channel of an adapter can be its own bus. A USB device is
claimed once, so the buses on one adapter share one handle
(:class:`_Adapter`): control requests carry the bus's channel in
``wValue``, and the one bulk-IN endpoint is read by one bus at a time,
which files each frame for the bus of the channel it names.

``core.bus`` imports this module on the capture thread only: pyusb and the
bundled libusb are vendor work the GUI thread must not do.
"""

from __future__ import annotations

import contextlib
import errno
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import can
import can.interfaces.gs_usb
import libusb_package
import usb.core
import usb.util
from gs_usb.constants import (
    CAN_EFF_FLAG,
    CAN_ERR_FLAG,
    CAN_RTR_FLAG,
    GS_CAN_MODE_HW_TIMESTAMP,
    GS_CAN_MODE_LISTEN_ONLY,
    GS_CAN_MODE_LOOP_BACK,
    GS_CAN_MODE_NORMAL,
    GS_CAN_MODE_ONE_SHOT,
)
from gs_usb.gs_usb import GsUsb
from gs_usb.gs_usb_frame import (
    GS_USB_FRAME_SIZE,
    GS_USB_FRAME_SIZE_HW_TIMESTAMP,
    GS_USB_NONE_ECHO_ID,
    GsUsbFrame,
)
from gs_usb.gs_usb_structures import DeviceBitTiming, DeviceCapability, DeviceInfo, DeviceMode

from ithildin.core.bus import ADAPTER_NOT_ATTACHED

_PROBE_INTERVAL_S = 1.0
"""How often a silent bus asks the adapter whether it is still there."""

_WRAP_S = 2**32 / 1e6
"""The adapter's 32-bit microsecond counter wraps every 71.6 min."""

_MIN_READ_S = 0.001

_CLOSE_LOCK_WAIT_S = 2.0
"""How long a closing bus waits for :data:`GS_USB_LOCK` before it lets go
of its channel without it."""

_QUEUE_DEPTH = 4096
"""Frames held for a channel whose bus isn't reading; the oldest go first."""

# The gs_usb protocol (Linux gs_usb driver / candleLight): vendor requests to
# interface 0 whose wValue is the CAN channel, except DEVICE_CONFIG's.
_REQ_OUT = 0x41
_REQ_IN = 0xC1
_BREQ_BITTIMING = 1
_BREQ_MODE = 2
_BREQ_BT_CONST = 4
_BREQ_DEVICE_CONFIG = 5
_MODE_RESET = 0
_MODE_START = 1
_EP_IN = 0x81
_EP_OUT = 0x02
_READ_SIZE = 64
"""One full-speed packet: a 20 or 24 byte frame ends the read short."""
_DRIVER_MODES = (
    GS_CAN_MODE_LISTEN_ONLY
    | GS_CAN_MODE_LOOP_BACK
    | GS_CAN_MODE_ONE_SHOT
    | GS_CAN_MODE_HW_TIMESTAMP
)
"""Mode flags this driver starts a channel with, when the firmware has them
(as gs_usb's ``start()``)."""
_LIBUSB_ERROR_PIPE = -9


GS_USB_LOCK = threading.Lock()
"""Held while opening a bus, while the prober reads serials and while a
bus lets go of its channel. Opens read adapters other than their own (a
pinned open reads every serial), and WinUSB grants an adapter to one handle
at a time: an open that collides with another thread's read fails. Wait
for it with a timeout."""

_ADAPTERS: dict[tuple[int, int], _Adapter] = {}
"""Every adapter a bus of this process holds, by USB (bus, address)."""
_ADAPTERS_LOCK = threading.Lock()


class GsUsbBus(can.interfaces.gs_usb.GsUsbBus):
    """CAN channel ``device_channel`` of a gs_usb adapter, found by its USB
    ``serial`` or else by its index ``channel`` in ``GsUsb.scan()``.

    Adds over python-can's bus: opening by serial (the index follows plug
    order), any CAN channel of a multi-channel adapter (several at once,
    sharing the adapter), listen-only (refused when the firmware lacks it),
    a start the device stalls refused as ``CanInitializationError``,
    ``recv`` raising ``CanOperationError("gs_usb adapter lost: …")`` from
    the ``USBError`` within about a second of an unplug, epoch timestamps,
    and ``flush_tx_buffer`` as a controller restart. Construct it holding
    :data:`GS_USB_LOCK`.
    """

    def __init__(
        self,
        channel: int,
        bitrate: int,
        listen_only: bool = False,
        serial: str | None = None,
        device_channel: int = 0,
    ) -> None:
        # Replicates python-can's constructor, whose start() takes no mode:
        # starting there and restarting listen-only would ACK in between.
        # First load the bundled libusb into pyusb; gs_usb's lookups reuse it.
        libusb_package.get_libusb1_backend()
        devs = GsUsb.scan()
        if serial is not None:
            dev = _with_serial(devs, serial)
        elif channel >= len(devs):
            raise can.CanInitializationError(
                f"gs_usb adapter {channel} not found ({len(devs)} connected)"
            )
        else:
            dev = devs[channel]
        adapter = _join(dev, device_channel)
        try:
            capability = _winusb_bound(lambda: adapter.capability(device_channel))
            # A start would silently drop a mode the firmware lacks.
            if listen_only and not capability.feature & GS_CAN_MODE_LISTEN_ONLY:
                raise can.CanInitializationError("This adapter's firmware has no listen-only mode")
            timing = can.BitTiming.from_sample_point(
                f_clock=capability.fclk_can, bitrate=bitrate, sample_point=87.5
            )
            prop_seg = 1
            self._timing = {
                "prop_seg": prop_seg,
                "phase_seg1": timing.tseg1 - prop_seg,
                "phase_seg2": timing.tseg2,
                "sjw": timing.sjw,
                "brp": timing.brp,
            }
            self._feature = int(capability.feature)
            self._flags = GS_CAN_MODE_HW_TIMESTAMP | (
                GS_CAN_MODE_LISTEN_ONLY if listen_only else GS_CAN_MODE_NORMAL
            )
            adapter.set_timing(device_channel, self._timing)
            try:
                self._started = adapter.start(device_channel, self._flags, self._feature)
            except usb.core.USBError as exc:
                if not _stalled(exc):
                    raise
                mode = "listen-only" if listen_only else "normal"
                hint = "" if listen_only else " Open the bus listen-only to watch it."
                raise can.CanInitializationError(
                    f"The gs_usb adapter refused to start channel {device_channel} in "
                    f"{mode} mode (it stalled the request): the device does not allow "
                    f"that on this channel, or not right now.{hint}"
                ) from exc
        except BaseException:
            _leave(adapter, device_channel, rearm=None, take_lock=False)
            raise
        self._adapter = adapter
        self._device_channel = device_channel
        self.gs_usb = adapter.dev
        name = serial if serial is not None else channel
        suffix = f" ch{device_channel}" if device_channel else ""
        self.channel_info = f"gs_usb {name}{suffix}"
        self._can_protocol = can.CanProtocol.CAN_20
        self._hw_timestamps = bool(self._started & GS_CAN_MODE_HW_TIMESTAMP)
        self._ts_offset: float | None = None
        self._last_alive = time.perf_counter()
        can.BusABC.__init__(self, channel=channel)

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        # libusb waits forever on a 0 ms timeout, and BusABC.recv passes on
        # what is left of its own timeout, which can round to 0 ms.
        wait = max(timeout or 0.0, _MIN_READ_S)
        try:
            frame = self._adapter.recv(self._device_channel, wait)
        except usb.core.USBError:
            # A read that fails rather than times out, as an unplugged
            # adapter's does: ask the adapter, so a loss is reported, not
            # read as silence.
            self._probe()
            return None, False
        # Not time.monotonic: on Windows before 3.13 it ticks in 15.6 ms
        # steps, so BusABC.recv's few-ms reads would all seem to end early.
        now = time.perf_counter()
        if frame is None:
            if now - self._last_alive >= _PROBE_INTERVAL_S:
                self._probe()
            return None, False
        self._last_alive = now
        return self._message(frame), False

    def _message(self, frame: Any) -> can.Message:
        return can.Message(
            timestamp=self._host_time(frame.timestamp),
            arbitration_id=frame.arbitration_id,
            is_extended_id=frame.is_extended_id,
            is_remote_frame=frame.is_remote_frame,
            is_error_frame=frame.is_error_frame,
            channel=self.channel_info,
            dlc=frame.can_dlc,
            data=bytearray(frame.data)[: frame.can_dlc],
            is_rx=frame.echo_id == GS_USB_NONE_ECHO_ID,
        )

    def _probe(self) -> None:
        try:
            self._adapter.probe()
        except usb.core.USBError as exc:
            raise can.CanOperationError(f"gs_usb adapter lost: {exc}") from exc
        self._last_alive = time.perf_counter()

    def _host_time(self, device_time: float) -> float:
        now = time.time()
        if not self._hw_timestamps:
            return now
        if self._ts_offset is None:
            self._ts_offset = now - device_time
        # Take the counter wrap that lands nearest the host clock, which
        # holds over any silence, unlike counting wraps between frames.
        self._ts_offset += round((now - device_time - self._ts_offset) / _WRAP_S) * _WRAP_S
        return device_time + self._ts_offset

    def flush_tx_buffer(self) -> None:
        """Restart the controller, which empties its mailboxes and the firmware queue."""
        self._adapter.stop(self._device_channel)
        self._adapter.set_timing(self._device_channel, self._timing)
        self._started = self._adapter.start(self._device_channel, self._flags, self._feature)
        # The start of a lone channel resets the USB device; don't trust its
        # clock to carry on.
        self._ts_offset = None

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        # A periodic task blocked on the send lock while the bus closed sends
        # once more (BusWorker._close_bus). pyusb would reopen the adapter
        # for it and queue the frame on the stopped controller.
        if self._is_shutdown:
            raise can.CanOperationError("gs_usb bus is shut down")
        can_id = msg.arbitration_id
        if msg.is_extended_id:
            can_id |= CAN_EFF_FLAG
        if msg.is_remote_frame:
            can_id |= CAN_RTR_FLAG
        if msg.is_error_frame:
            can_id |= CAN_ERR_FLAG
        frame = GsUsbFrame(can_id, list(bytes(msg.data)))
        frame.can_dlc = msg.dlc
        try:
            self._adapter.send(self._device_channel, frame)
        except usb.core.USBError as exc:
            raise can.CanOperationError("The message could not be sent") from exc

    def shutdown(self) -> None:
        if self._is_shutdown:
            return
        can.BusABC.shutdown(self)
        _leave(
            self._adapter,
            self._device_channel,
            rearm=(self._timing, self._started),
            take_lock=True,
        )


class _Adapter:
    """One claimed gs_usb adapter, shared by the buses open on its CAN channels.

    One thread at a time reads the bulk-IN endpoint; a frame of another
    channel goes to that channel's queue, and its bus takes it from there.
    """

    def __init__(self, dev: GsUsb) -> None:
        self.dev = dev
        self.usb = dev.gs_usb
        self.key: tuple[int, int] = (dev.bus, dev.address)
        self.serial: str | None = None
        self.channel_count = 1
        self._users: set[int] = set()
        self._frames: dict[int, deque[Any]] = {}
        self._reading = False
        self._cv = threading.Condition()
        """Guards ``_users``, ``_frames`` and ``_reading``."""
        self._running: dict[int, int] = {}
        """Started channel → the mode flags it started with."""
        self._control = threading.Lock()
        """Guards ``_running`` across a start or stop, so a lone channel's
        USB reset never lands on another channel's session."""
        self._write = threading.Lock()

    # ---- Channels ----

    def add_user(self, channel: int) -> None:
        with self._cv:
            if channel in self._users:
                raise can.CanInitializationError(
                    f"gs_usb adapter channel {channel} is already open by another bus"
                )
            self._users.add(channel)
            self._frames[channel] = deque(maxlen=_QUEUE_DEPTH)

    def remove_user(self, channel: int) -> bool:
        """Drop ``channel``'s bus; ``True`` when it was the last one."""
        with self._cv:
            self._users.discard(channel)
            self._frames.pop(channel, None)
            return not self._users

    def capability(self, channel: int) -> Any:
        data = self.usb.ctrl_transfer(_REQ_IN, _BREQ_BT_CONST, channel, 0, 40)
        return DeviceCapability.unpack(bytes(data))

    def set_timing(self, channel: int, timing: dict[str, int]) -> None:
        _set_timing(self.usb, channel, timing)

    def start(self, channel: int, flags: int, feature: int) -> int:
        """Start ``channel`` with those of ``flags`` the firmware and this
        driver both have, and return them. Raises the request's ``USBError``."""
        flags &= feature & _DRIVER_MODES
        with self._control:
            _start(self.usb, channel, flags, alone=not self._running)
            self._running[channel] = flags
        return flags

    def stop(self, channel: int) -> None:
        """Stop ``channel`` if it runs (a refused start left it stopped)."""
        with self._control:
            if self._running.pop(channel, None) is not None:
                _stop(self.usb, channel)

    def probe(self) -> None:
        """Raise the ``USBError`` of an adapter that no longer answers."""
        self.usb.ctrl_transfer(_REQ_IN, _BREQ_DEVICE_CONFIG, 0, 0, 12)

    # ---- Frames ----

    def recv(self, channel: int, timeout: float) -> Any:
        """The next frame of ``channel`` within ``timeout`` seconds, else ``None``.

        Raises the read's ``USBError``, other than a timeout's.
        """
        deadline = time.perf_counter() + timeout
        with self._cv:
            while True:
                queued = self._frames.get(channel)
                if queued:
                    return queued.popleft()
                if not self._reading:
                    self._reading = True
                    break
                left = deadline - time.perf_counter()
                if left <= 0:
                    return None
                self._cv.wait(left)
        try:
            while True:
                frame = self._read(max(deadline - time.perf_counter(), _MIN_READ_S))
                if frame is None:
                    return None
                if frame.channel == channel:
                    return frame
                with self._cv:
                    # A channel no bus has open (closed meanwhile) drops it.
                    queue = self._frames.get(frame.channel)
                    if queue is not None:
                        queue.append(frame)
                        self._cv.notify_all()
                if time.perf_counter() >= deadline:
                    return None
        finally:
            with self._cv:
                self._reading = False
                self._cv.notify_all()

    def _read(self, timeout: float) -> Any:
        try:
            data = bytes(self.usb.read(_EP_IN, _READ_SIZE, max(1, round(timeout * 1000))))
        except usb.core.USBTimeoutError:
            return None
        # A channel started without hardware timestamps sends 20 bytes.
        hw_timestamp = len(data) >= GS_USB_FRAME_SIZE_HW_TIMESTAMP
        size = GS_USB_FRAME_SIZE_HW_TIMESTAMP if hw_timestamp else GS_USB_FRAME_SIZE
        if len(data) < size:
            return None
        frame = GsUsbFrame()
        GsUsbFrame.unpack_into(frame, data[:size], hw_timestamp)
        return frame

    def send(self, channel: int, frame: Any) -> None:
        frame.channel = channel
        hw_timestamp = bool(self._running.get(channel, 0) & GS_CAN_MODE_HW_TIMESTAMP)
        with self._write:
            self.usb.write(_EP_OUT, frame.pack(hw_timestamp))

    # ---- Release ----

    def release(self, rearm: tuple[int, dict[str, int], int] | None) -> None:
        """Let go of the handle; with ``rearm`` (channel, timing, flags), start
        and stop that channel once more through a fresh one."""
        # WinUSB grants a device to one handle at a time, so release this
        # adapter's own, or the fresh handle below and the next open are refused.
        usb.util.dispose_resources(self.usb)
        if rearm is None:
            return
        channel, timing, flags = rearm
        # Like python-can, start and stop the adapter once more through a
        # fresh handle, or opening it again fails (python-can #1790). In the
        # last bus's own channel and mode, so a listen-only bus never ACKs,
        # and found by USB address, so after an unplug no other adapter is
        # touched. That handle is released too: on macOS a few leaked
        # handles leave every later read failing with access denied.
        with contextlib.suppress(usb.core.USBError):
            dev = GsUsb.find(bus=self.key[0], address=self.key[1])
            if dev is not None:
                try:
                    _set_timing(dev.gs_usb, channel, timing)
                    _start(dev.gs_usb, channel, flags, alone=True)
                    _stop(dev.gs_usb, channel)
                finally:
                    usb.util.dispose_resources(dev.gs_usb)


def _join(dev: GsUsb, channel: int) -> _Adapter:
    """Take ``channel`` of the adapter ``dev`` is, claiming the adapter
    unless a bus of this process holds it already. Hold :data:`GS_USB_LOCK`."""
    key = (dev.bus, dev.address)
    with _ADAPTERS_LOCK:
        held = _ADAPTERS.get(key)
    adapter = held or _Adapter(dev)
    try:
        if held is None:
            adapter.channel_count = _winusb_bound(lambda: channel_count(dev, strict=True))
            with contextlib.suppress(*SERIAL_READ_ERRORS):
                adapter.serial = dev.serial_number
        if not 0 <= channel < adapter.channel_count:
            count = adapter.channel_count
            raise can.CanInitializationError(
                f"This gs_usb adapter has no channel {channel} "
                f"(it has {count} channel{'s' if count != 1 else ''})"
            )
        adapter.add_user(channel)
    except BaseException:
        if held is None:
            usb.util.dispose_resources(dev.gs_usb)
        raise
    if held is None:
        with _ADAPTERS_LOCK:
            _ADAPTERS[key] = adapter
    return adapter


def _leave(
    adapter: _Adapter,
    channel: int,
    *,
    rearm: tuple[dict[str, int], int] | None,
    take_lock: bool,
) -> None:
    """Stop ``channel`` and drop it; the last bus out releases the adapter.

    ``take_lock``: wait for :data:`GS_USB_LOCK` (with a timeout), so no open
    joins the adapter, nor reads its serial, while it is let go.
    """
    adapter.stop(channel)
    locked = take_lock and GS_USB_LOCK.acquire(timeout=_CLOSE_LOCK_WAIT_S)
    try:
        with _ADAPTERS_LOCK:
            last = adapter.remove_user(channel)
            if last and _ADAPTERS.get(adapter.key) is adapter:
                del _ADAPTERS[adapter.key]
        if last:
            adapter.release(None if rearm is None else (channel, *rearm))
    finally:
        if locked:
            GS_USB_LOCK.release()


def held_adapter(dev: GsUsb) -> tuple[str | None, int] | None:
    """``(serial, channel count)`` of an adapter a bus of this process holds,
    else ``None``. Touches no USB: WinUSB refuses a second handle on it."""
    with _ADAPTERS_LOCK:
        adapter = _ADAPTERS.get((dev.bus, dev.address))
    return None if adapter is None else (adapter.serial, adapter.channel_count)


def channel_count(dev: GsUsb, *, strict: bool = False) -> int:
    """How many CAN channels the adapter has (DEVICE_CONFIG's icount + 1).

    1 when it can't be read, unless ``strict``. Opens the device.
    """
    try:
        data = dev.gs_usb.ctrl_transfer(_REQ_IN, _BREQ_DEVICE_CONFIG, 0, 0, 12)
        return int(DeviceInfo.unpack(bytes(data)).icount) + 1
    except (*SERIAL_READ_ERRORS, struct.error):
        if strict:
            raise
        return 1


def _set_timing(usbdev: Any, channel: int, timing: dict[str, int]) -> None:
    bit_timing = DeviceBitTiming(
        timing["prop_seg"], timing["phase_seg1"], timing["phase_seg2"], timing["sjw"], timing["brp"]
    )
    usbdev.ctrl_transfer(_REQ_OUT, _BREQ_BITTIMING, channel, 0, bit_timing.pack())


def _start(usbdev: Any, channel: int, flags: int, *, alone: bool) -> None:
    """Start ``channel``.

    ``alone``: no other channel of the device runs. Only then reset the
    device first, like gs_usb's ``start()`` (so it can be restarted): the
    reset ends every channel's session. Never on macOS, where after that
    reset libusb reports a kernel driver on the adapter's interface, and the
    detach that then takes needs root.
    """
    if alone:
        if sys.platform != "darwin":
            usbdev.reset()
        if sys.platform != "win32" and usbdev.is_kernel_driver_active(0):
            usbdev.detach_kernel_driver(0)
    usbdev.ctrl_transfer(_REQ_OUT, _BREQ_MODE, channel, 0, DeviceMode(_MODE_START, flags).pack())


def _stop(usbdev: Any, channel: int) -> None:
    with contextlib.suppress(usb.core.USBError):
        usbdev.ctrl_transfer(_REQ_OUT, _BREQ_MODE, channel, 0, DeviceMode(_MODE_RESET, 0).pack())


def _stalled(exc: usb.core.USBError) -> bool:
    """The device refused the request (a STALL), rather than failing to answer."""
    return bool(exc.errno == errno.EPIPE or exc.backend_error_code == _LIBUSB_ERROR_PIPE)


def _winusb_bound[T](request: Callable[[], T]) -> T:
    """Run a first request to an adapter, explaining libusb's NOT_SUPPORTED."""
    try:
        return request()
    except NotImplementedError as exc:
        # libusb's NOT_SUPPORTED: no WinUSB driver is bound to the adapter.
        raise can.CanInitializationError(
            "No WinUSB driver is bound to this adapter; bind WinUSB with Zadig"
        ) from exc


SERIAL_READ_ERRORS = (usb.core.USBError, ValueError, NotImplementedError)
"""What reading a gs_usb adapter's serial raises when it can't be read: the
adapter is open elsewhere (WinUSB grants one handle at a time), unplugged,
or has no WinUSB driver."""


def _with_serial(devs: list[GsUsb], serial: str) -> GsUsb:
    """The one scanned adapter with this USB serial, or ``CanInitializationError``.

    Reads every adapter's serial: clones can share one, and opening the
    first would bind whichever enumerated first. An adapter a bus of this
    process holds answers from what its open read.
    """
    found: list[GsUsb] = []
    for dev in devs:
        held = held_adapter(dev)
        if held is not None:
            # Never a second handle on it, nor a release of its bus's.
            if held[0] == serial:
                found.append(dev)
            continue
        with contextlib.suppress(*SERIAL_READ_ERRORS):
            if dev.serial_number == serial:
                found.append(dev)
                continue
        # Reading the serial opened the device; another bus may want it.
        usb.util.dispose_resources(dev.gs_usb)
    if len(found) == 1:
        return found[0]
    for dev in found:
        if held_adapter(dev) is None:
            usb.util.dispose_resources(dev.gs_usb)
    if not found:
        raise can.CanInitializationError(f"its pinned gs_usb adapter {ADAPTER_NOT_ATTACHED}")
    raise can.CanInitializationError(
        f"{len(found)} adapters report the identity this bus is pinned to"
    )
