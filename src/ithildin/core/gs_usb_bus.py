# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""CANable / CANtact / candleLight adapters, through python-can's gs_usb backend.

python-can's ``GsUsbBus`` has no listen-only mode, never notices an
unplugged adapter and stamps frames with the adapter's own microsecond
counter instead of epoch time. :class:`GsUsbBus` fixes those and adds a TX
flush.

``core.bus`` imports this module on the capture thread only: pyusb and the
bundled libusb are vendor work the GUI thread must not do.
"""

from __future__ import annotations

import contextlib
import time

import can
import can.interfaces.gs_usb
import libusb_package
import usb.core
import usb.util
from gs_usb.constants import GS_CAN_MODE_HW_TIMESTAMP, GS_CAN_MODE_LISTEN_ONLY, GS_CAN_MODE_NORMAL
from gs_usb.gs_usb import GsUsb

_PROBE_INTERVAL_S = 1.0
"""How often a silent bus asks the adapter whether it is still there."""

_WRAP_S = 2**32 / 1e6
"""The adapter's 32-bit microsecond counter wraps every 71.6 min."""

_MIN_READ_S = 0.001


class GsUsbBus(can.interfaces.gs_usb.GsUsbBus):
    """A gs_usb adapter, by its index in ``GsUsb.scan()``.

    Adds over python-can's bus: listen-only (refused when the firmware
    lacks it), ``recv`` raising ``CanOperationError("gs_usb adapter lost:
    …")`` from the ``USBError`` within about a second of an unplug, epoch
    timestamps, and ``flush_tx_buffer`` as a controller restart.
    """

    def __init__(self, channel: int, bitrate: int, listen_only: bool = False) -> None:
        # Replicates python-can's constructor, whose start() takes no mode:
        # starting there and restarting listen-only would ACK in between.
        # First load the bundled libusb into pyusb; gs_usb's lookups reuse it.
        libusb_package.get_libusb1_backend()
        devs = GsUsb.scan()
        if channel >= len(devs):
            raise can.CanInitializationError(
                f"gs_usb adapter {channel} not found ({len(devs)} connected)"
            )
        dev = devs[channel]
        try:
            capability = dev.device_capability
        except NotImplementedError as exc:
            # libusb's NOT_SUPPORTED: no WinUSB driver is bound to the adapter.
            raise can.CanInitializationError(
                "No WinUSB driver is bound to this adapter; bind WinUSB with Zadig"
            ) from exc
        # start() would silently drop a mode the firmware lacks.
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
        self._flags = GS_CAN_MODE_HW_TIMESTAMP | (
            GS_CAN_MODE_LISTEN_ONLY if listen_only else GS_CAN_MODE_NORMAL
        )
        dev.set_timing(**self._timing)
        dev.start(self._flags)
        self.gs_usb = dev
        self.channel_info = f"gs_usb {channel}"
        self._can_protocol = can.CanProtocol.CAN_20
        self._hw_timestamps = bool(dev.device_flags & GS_CAN_MODE_HW_TIMESTAMP)
        self._ts_offset: float | None = None
        self._last_alive = time.perf_counter()
        can.BusABC.__init__(self, channel=channel)

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        # libusb waits forever on a 0 ms timeout, and BusABC.recv passes on
        # what is left of its own timeout, which can round to 0 ms.
        wait = max(timeout or 0.0, _MIN_READ_S)
        # Not time.monotonic: on Windows before 3.13 it ticks in 15.6 ms
        # steps, so BusABC.recv's few-ms reads would all seem to end early.
        started = time.perf_counter()
        msg, filtered = super()._recv_internal(wait)
        now = time.perf_counter()
        if msg is not None:
            self._last_alive = now
            msg.timestamp = self._host_time(msg.timestamp)
        # gs_usb's read swallows every USB error, an unplugged adapter's
        # too, so a read that ends early has failed: without asking the
        # adapter, recv would spin out its timeout and never report the loss.
        elif now - started < wait / 2 or now - self._last_alive >= _PROBE_INTERVAL_S:
            self._probe()
        return msg, filtered

    def _probe(self) -> None:
        try:
            _ = self.gs_usb.device_info
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
        self.gs_usb.stop()
        self.gs_usb.set_timing(**self._timing)
        self.gs_usb.start(self._flags)
        # start() resets the USB device; don't trust its clock to carry on.
        self._ts_offset = None

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        # A periodic task blocked on the send lock while the bus closed sends
        # once more (BusWorker._close_bus). pyusb would reopen the adapter
        # for it and queue the frame on the stopped controller.
        if self._is_shutdown:
            raise can.CanOperationError("gs_usb bus is shut down")
        super().send(msg, timeout)

    def shutdown(self) -> None:
        if self._is_shutdown:
            return
        can.BusABC.shutdown(self)
        self.gs_usb.stop()
        # WinUSB grants a device to one handle at a time, so release this
        # bus's own, or the fresh handle below and the next open are refused.
        usb.util.dispose_resources(self.gs_usb.gs_usb)
        # Like python-can, start and stop the adapter once more through a
        # fresh handle, or opening it again fails (python-can #1790). In this
        # bus's own mode, so a listen-only bus never ACKs, and found by USB
        # address, so after an unplug no other adapter is touched.
        with contextlib.suppress(usb.core.USBError):
            dev = GsUsb.find(bus=self.gs_usb.bus, address=self.gs_usb.address)
            if dev is not None:
                dev.set_timing(**self._timing)
                dev.start(self._flags)
                dev.stop()
