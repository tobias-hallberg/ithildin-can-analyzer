# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""``core.gs_usb_bus.GsUsbBus`` against fake gs_usb adapters: no USB, no libusb."""

from __future__ import annotations

import queue
import struct
import threading
import time
from array import array
from collections.abc import Callable, Iterator
from typing import Any

import can
import libusb_package
import pytest
import usb.core
import usb.util
from gs_usb.constants import GS_CAN_MODE_HW_TIMESTAMP, GS_CAN_MODE_LISTEN_ONLY
from gs_usb.gs_usb_frame import GS_USB_NONE_ECHO_ID, GsUsbFrame

from ithildin.app_state.bus_reader import BusReader
from ithildin.core import gs_usb_bus
from ithildin.core.bus import BusWorker
from ithildin.core.gs_usb_bus import GsUsbBus
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import BusHealth
from ithildin.workspace.schema import BusConfig

_LISTEN_ONLY = GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP

# gs_usb vendor requests (bRequest), as the Linux driver and candleLight number them.
_BITTIMING, _MODE, _BT_CONST, _DEVICE_CONFIG = 1, 2, 4, 5
_STALL = ("Pipe error", -9, 32)  # libusb's LIBUSB_ERROR_PIPE / EPIPE


class FakeAdapter:
    """One adapter, both as gs_usb's ``GsUsb`` and as its pyusb device.

    It speaks the gs_usb protocol at the USB level: vendor control requests
    with the CAN channel in ``wValue`` (DEVICE_CONFIG is per device), and
    one bulk-IN endpoint whose frames carry their channel.

    * ``channels``: CAN channels it reports (DEVICE_CONFIG's icount + 1).
    * ``feature``: the firmware's feature bits; ``no_driver`` makes every
      control transfer raise like libusb without WinUSB bound.
    * ``gone``: unplugged. Control transfers raise ``USBError`` and the
      fake ``scan`` / ``find`` skip it.
    * ``read_fails``: every read raises a ``USBError`` at once.
    * ``rx``: ``(can_id, data, timestamp_us[, channel])`` frames, one per
      read; an empty queue makes the read wait out its timeout.
    * ``refuse_normal``: channels whose normal-mode start the device stalls
      (the KRAKA CCU's charger bus always, its BMS bus while charging).
    * ``refuse_start``: every start fails, access denied.
    * ``calls``: ``(name, args)`` per bit timing / start / stop request and
      per ``usb.util.dispose_resources`` of it, in order.
    * ``requests``: ``(name, channel)`` per per-channel control request.
    * ``resets``: USB resets, which a first start does on Windows / Linux.
    * ``sent``: ``(can_id, channel)`` of every frame written to the adapter.
    * ``serial``: its USB serial; ``held`` makes reading it fail as WinUSB
      does while another handle has the adapter open.
    """

    def __init__(
        self, address: int, feature: int = _LISTEN_ONLY, serial: str = "", channels: int = 1
    ) -> None:
        self.bus = 1
        self.address = address
        self.feature = feature
        self.serial = serial or f"SN{address}"
        self.channels = channels
        self.held = False
        self.no_driver = False
        self.gone = False
        self.read_fails = False
        self.refuse_normal: set[int] = set()
        self.refuse_start = False
        self.rx: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self.calls: list[tuple[str, object]] = []
        self.requests: list[tuple[str, int]] = []
        self.sent: list[tuple[int, int]] = []
        self.modes: dict[int, int] = {}
        self.gs_usb = self
        self.reads = 0
        self.probes = 0
        self.resets = 0

    # ---- pyusb device ----

    def reset(self) -> None:
        self.resets += 1

    def is_kernel_driver_active(self, interface: int) -> bool:
        return False

    def ctrl_transfer(
        self, request_type: int, request: int, value: int, index: int, data: Any = None
    ) -> Any:
        if self.no_driver:
            raise NotImplementedError("Operation not supported or unimplemented on this platform")
        if self.gone:
            raise usb.core.USBError("No such device (it may have been disconnected)", -4, 19)
        if request == _DEVICE_CONFIG:
            self.probes += 1
            return array("B", struct.pack("<4B2I", 0, 0, 0, self.channels - 1, 2, 1))
        if value >= self.channels:
            raise usb.core.USBError(*_STALL)
        if request == _BT_CONST:
            self.requests.append(("bt_const", value))
            consts = (self.feature, 48_000_000, 1, 16, 1, 8, 4, 1, 1024, 1)
            return array("B", struct.pack("<10I", *consts))
        if request == _BITTIMING:
            self.requests.append(("set_timing", value))
            self.calls.append(("set_timing", struct.unpack("<5I", bytes(data))))
            return len(data)
        if request == _MODE:
            mode, flags = struct.unpack("<II", bytes(data))
            if mode == 0:
                self.modes.pop(value, None)
                self.requests.append(("stop", value))
                self.calls.append(("stop", None))
                return len(data)
            if self.refuse_start:
                raise usb.core.USBError("Access denied (insufficient permissions)", -3, 13)
            if value in self.refuse_normal and not flags & GS_CAN_MODE_LISTEN_ONLY:
                raise usb.core.USBError(*_STALL)
            self.modes[value] = flags
            self.requests.append(("start", value))
            self.calls.append(("start", flags))
            return len(data)
        raise usb.core.USBError(*_STALL)

    def read(self, endpoint: int, size: int, timeout: int) -> array[int]:
        self.reads += 1
        if self.read_fails:
            raise usb.core.USBError("No such device (it may have been disconnected)", -4, 19)
        try:
            can_id, data, timestamp_us, *rest = self.rx.get_nowait()
        except queue.Empty:
            time.sleep(timeout / 1000)
            raise usb.core.USBTimeoutError("Operation timed out", -7, 60) from None
        channel = rest[0] if rest else 0
        frame = GsUsbFrame(can_id, list(data))
        frame.echo_id = GS_USB_NONE_ECHO_ID
        frame.channel = channel
        frame.timestamp_us = timestamp_us
        return array("B", frame.pack(bool(self.modes.get(channel, 0) & GS_CAN_MODE_HW_TIMESTAMP)))

    def write(self, endpoint: int, data: bytes, timeout: int | None = None) -> int:
        _, can_id, _, channel = struct.unpack_from("<2I2B", data)
        self.sent.append((can_id, channel))
        return len(data)

    # ---- GsUsb ----

    @property
    def serial_number(self) -> str:
        if self.gone or self.held:
            raise usb.core.USBError("Access denied (insufficient permissions)", -3, 13)
        return self.serial

    def starts(self) -> list[object]:
        return [flags for name, flags in self.calls if name == "start"]


@pytest.fixture
def adapters(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[FakeAdapter]]:
    """The plugged-in adapters, in scan order; one to start with.

    Every test lets go of every adapter it opened: a bus left open would
    hand its adapter to the next test's open of the same USB address.
    """
    plugged = [FakeAdapter(address=5)]

    class FakeGsUsb:
        @staticmethod
        def scan() -> list[FakeAdapter]:
            return [a for a in plugged if not a.gone]

        @staticmethod
        def find(bus: int, address: int) -> FakeAdapter | None:
            return next(
                (a for a in plugged if not a.gone and (a.bus, a.address) == (bus, address)), None
            )

    monkeypatch.setattr(gs_usb_bus, "GsUsb", FakeGsUsb)
    monkeypatch.setattr(libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(
        usb.util, "dispose_resources", lambda dev: dev.calls.append(("dispose", None))
    )
    yield plugged
    leaked = dict(gs_usb_bus._ADAPTERS)
    gs_usb_bus._ADAPTERS.clear()
    assert not leaked, f"adapters still held after the test: {list(leaked)}"


@pytest.fixture
def make_bus(adapters: list[FakeAdapter]) -> Iterator[Callable[..., GsUsbBus]]:
    buses: list[GsUsbBus] = []

    def make(
        channel: int = 0,
        *,
        listen_only: bool = False,
        device_channel: int = 0,
        serial: str | None = None,
    ) -> GsUsbBus:
        bus = GsUsbBus(
            channel=channel,
            bitrate=500_000,
            listen_only=listen_only,
            serial=serial,
            device_channel=device_channel,
        )
        buses.append(bus)
        return bus

    yield make
    for bus in buses:
        bus.shutdown()


# ---- Opening ----


def test_listen_only_starts_once_in_listen_only_mode(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    make_bus(listen_only=True)
    assert [name for name, _ in adapters[0].calls] == ["set_timing", "start"]
    assert adapters[0].starts() == [_LISTEN_ONLY]


def test_listen_only_refused_without_firmware_support(adapters) -> None:  # type: ignore[no-untyped-def]
    adapters[0].feature = GS_CAN_MODE_HW_TIMESTAMP
    with pytest.raises(can.CanInitializationError, match="no listen-only mode"):
        GsUsbBus(channel=0, bitrate=500_000, listen_only=True)
    assert adapters[0].starts() == []


def test_normal_mode_starts_with_hardware_timestamps(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    make_bus()
    assert adapters[0].starts() == [GS_CAN_MODE_HW_TIMESTAMP]


def test_missing_winusb_driver_is_explained(adapters) -> None:  # type: ignore[no-untyped-def]
    adapters[0].no_driver = True
    with pytest.raises(can.CanInitializationError, match="WinUSB"):
        GsUsbBus(channel=0, bitrate=500_000)


def test_missing_adapter_is_refused(adapters) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(can.CanInitializationError, match="adapter 1 not found"):
        GsUsbBus(channel=1, bitrate=500_000)


def test_worker_opens_the_subclass(adapters) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(name="CAN_1", interface="gs_usb", channel=0, bitrate=500_000, listen_only=True)
    bus = BusWorker("CAN_1", cfg, RingBuffer(8))._open_bus()
    try:
        assert isinstance(bus, GsUsbBus)
        assert adapters[0].starts() == [_LISTEN_ONLY]
    finally:
        bus.shutdown()


# ---- Adapter loss ----


def test_unplug_raises_without_spinning(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus()
    adapters[0].gone = adapters[0].read_fails = True

    started = time.monotonic()
    with pytest.raises(can.CanOperationError, match="gs_usb adapter lost") as info:
        bus.recv(1.0)
    assert time.monotonic() - started < 0.5
    assert adapters[0].reads == 1
    assert isinstance(info.value.__cause__, usb.core.USBError)


def test_timed_out_read_does_not_probe(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus()
    opened = adapters[0].probes  # the open reads DEVICE_CONFIG for the channel count
    assert bus.recv(0.05) is None
    assert adapters[0].probes == opened


def test_silent_bus_is_probed(monkeypatch, adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gs_usb_bus, "_PROBE_INTERVAL_S", 0.0)
    bus = make_bus()
    assert bus.recv(0.01) is None
    assert adapters[0].probes >= 1


def test_silent_probe_finds_a_lost_adapter(monkeypatch, adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    """Reads that keep timing out normally still notice the adapter is gone."""
    monkeypatch.setattr(gs_usb_bus, "_PROBE_INTERVAL_S", 0.0)
    bus = make_bus()
    adapters[0].gone = True
    with pytest.raises(can.CanOperationError, match="gs_usb adapter lost"):
        bus.recv(0.01)


def test_unplug_fails_the_bus_as_lost(qapp, qtbot, adapters) -> None:  # type: ignore[no-untyped-def]
    reader = BusReader()
    try:
        cfg = BusConfig(name="CAN_1", interface="gs_usb", channel=0, bitrate=500_000)
        with qtbot.waitSignal(reader.bus_started, timeout=2000):
            reader.open(cfg)
        adapters[0].rx.put((0x123, b"\x01", 1000))
        qtbot.waitUntil(lambda: reader.rings["CAN_1"].total_seq >= 1, timeout=2000)

        with qtbot.waitSignal(reader.bus_failed, timeout=3000) as blocker:
            adapters[0].gone = True
            adapters[0].read_fails = True
        assert "gs_usb adapter lost" in blocker.args[1]
        assert reader.health("CAN_1") == (BusHealth.LOST, "")
    finally:
        assert reader.shutdown() == []


# ---- Timestamps ----


def test_timestamps_are_epoch_across_the_counter_wrap(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus()
    adapters[0].rx.put((0x100, b"\x01", 0xFFFF_FF00))
    adapters[0].rx.put((0x100, b"\x02", 0x100))

    first = bus.recv(1.0)
    second = bus.recv(1.0)
    assert first is not None and second is not None
    assert first.timestamp == pytest.approx(time.time(), abs=1.0)
    assert second.timestamp - first.timestamp == pytest.approx(512e-6, abs=1e-6)


def test_without_hardware_timestamps_frames_get_host_time(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    adapters[0].feature = GS_CAN_MODE_LISTEN_ONLY
    bus = make_bus()
    adapters[0].rx.put((0x100, b"\x01", 0x100))

    msg = bus.recv(1.0)
    assert msg is not None
    assert msg.timestamp == pytest.approx(time.time(), abs=1.0)


# ---- Flush and shutdown ----


def test_flush_restarts_in_the_same_mode(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus()
    timing, start = adapters[0].calls

    bus.flush_tx_buffer()
    assert adapters[0].calls[2:] == [("stop", None), timing, start]


def test_listen_only_shutdown_never_leaves_listen_only(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus(listen_only=True)

    bus.shutdown()
    bus.shutdown()
    # Opened, then started and stopped once more after closing.
    assert adapters[0].starts() == [_LISTEN_ONLY, _LISTEN_ONLY]
    assert adapters[0].calls[-2:] == [("stop", None), ("dispose", None)]


def test_shutdown_releases_its_handle_before_rearming(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    """WinUSB refuses the re-arm's fresh handle while the bus's own is open,
    and on macOS a few leaked handles leave every later read access-denied."""
    bus = make_bus()
    timing, start = adapters[0].calls

    bus.shutdown()
    assert adapters[0].calls[2:] == [
        ("stop", None),
        ("dispose", None),
        timing,
        start,
        ("stop", None),
        ("dispose", None),
    ]


def test_shutdown_releases_the_rearm_handle_when_the_rearm_fails(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus()

    adapters[0].refuse_start = True
    bus.shutdown()
    assert adapters[0].calls[-1] == ("dispose", None)


def test_send_after_shutdown_never_reaches_the_adapter(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus()
    bus.send(can.Message(arbitration_id=0x100, data=b"", is_extended_id=False))

    bus.shutdown()
    with pytest.raises(can.CanOperationError, match="shut down"):
        bus.send(can.Message(arbitration_id=0x101, data=b"", is_extended_id=False))
    assert adapters[0].sent == [(0x100, 0)]


def test_shutdown_after_unplug_leaves_other_adapters_alone(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    """Once the first adapter is gone, the second one has its scan index."""
    adapters.append(FakeAdapter(address=6))
    bus = make_bus(0)
    adapters[0].gone = True

    bus.shutdown()
    assert adapters[1].calls == []


@pytest.mark.parametrize(("platform", "resets"), [("darwin", 0), ("win32", 3)])
def test_adapter_is_never_usb_reset_on_macos(  # type: ignore[no-untyped-def]
    adapters, monkeypatch, platform: str, resets: int
) -> None:
    """After the reset macOS libusb reports a kernel driver on the adapter,
    and the detach gs_usb's start() then attempts needs root."""
    monkeypatch.setattr(gs_usb_bus.sys, "platform", platform)
    bus = GsUsbBus(channel=0, bitrate=500_000)
    bus.flush_tx_buffer()
    bus.shutdown()
    assert adapters[0].resets == resets


# ---- Opening by serial ----


def test_pinned_bus_opens_its_adapter_at_any_scan_index(adapters) -> None:  # type: ignore[no-untyped-def]
    """The index follows plug order; the serial stays with the adapter."""
    adapters.insert(0, FakeAdapter(address=6))
    bus = GsUsbBus(channel=0, bitrate=500_000, serial="SN5")
    try:
        assert bus.gs_usb is adapters[1]
        assert adapters[1].starts() == [GS_CAN_MODE_HW_TIMESTAMP]
        # The serial read opened the other adapter too; it is let go.
        assert adapters[0].calls == [("dispose", None)]
    finally:
        bus.shutdown()


def test_pinned_bus_skips_an_adapter_another_bus_holds(adapters) -> None:  # type: ignore[no-untyped-def]
    adapters[0].held = True
    adapters.append(FakeAdapter(address=6))
    bus = GsUsbBus(channel=0, bitrate=500_000, serial="SN6")
    try:
        assert bus.gs_usb is adapters[1]
        assert adapters[0].starts() == []
    finally:
        bus.shutdown()


def test_pinned_bus_refuses_when_its_adapter_is_missing(adapters) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(can.CanInitializationError, match="is not attached"):
        GsUsbBus(channel=0, bitrate=500_000, serial="SN9")
    assert adapters[0].starts() == []


def test_pinned_bus_refuses_a_serial_two_adapters_report(adapters) -> None:  # type: ignore[no-untyped-def]
    """Clones can share a serial: opening the first would bind whichever
    enumerated first."""
    adapters.append(FakeAdapter(address=6, serial="SN5"))
    with pytest.raises(can.CanInitializationError, match="2 adapters report"):
        GsUsbBus(channel=0, bitrate=500_000, serial="SN5")
    assert [a.starts() for a in adapters] == [[], []]
    assert [a.calls for a in adapters] == [[("dispose", None)], [("dispose", None)]]


class _ExclusiveAdapter(FakeAdapter):
    """WinUSB's one handle at a time: a thread's first USB request opens the
    adapter for that thread until it disposes of it, and a request from
    another thread meanwhile is refused."""

    def __init__(self, address: int) -> None:
        super().__init__(address)
        self.owner: threading.Thread | None = None

    def claim(self) -> None:
        if self.owner not in (None, threading.current_thread()):
            raise usb.core.USBError("Access denied (insufficient permissions)", -3, 13)
        self.owner = threading.current_thread()

    @property
    def serial_number(self) -> str:
        self.claim()
        # A descriptor read is a few control transfers: long enough for an
        # open on another thread to run into it.
        time.sleep(0.05)
        return self.serial

    def ctrl_transfer(
        self, request_type: int, request: int, value: int, index: int, data: Any = None
    ) -> Any:
        self.claim()
        return super().ctrl_transfer(request_type, request, value, index, data)


def test_pinned_opens_in_parallel_never_collide_on_a_serial_read(monkeypatch, adapters) -> None:  # type: ignore[no-untyped-def]
    """Each pinned open reads the other adapter's serial too; one open's read
    must not make the other open fail (both buses connecting at startup)."""
    adapters[:] = [_ExclusiveAdapter(address=5), _ExclusiveAdapter(address=6)]

    def dispose(dev: _ExclusiveAdapter) -> None:
        if dev.owner is threading.current_thread():
            dev.owner = None

    monkeypatch.setattr(usb.util, "dispose_resources", dispose)
    opened: dict[str, object] = {}

    def open_bus(worker: BusWorker) -> None:
        try:
            opened[worker.name] = worker._open_bus()
        except can.CanError as exc:
            opened[worker.name] = exc

    threads = [
        threading.Thread(
            target=open_bus,
            args=(
                BusWorker(
                    serial,
                    BusConfig(
                        name=serial, interface="gs_usb", channel=0, bitrate=500_000, hw_id=serial
                    ),
                    RingBuffer(8),
                ),
            ),
        )
        for serial in ("SN6", "SN5")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    try:
        assert {name: getattr(bus, "gs_usb", bus) for name, bus in opened.items()} == {
            "SN5": adapters[0],
            "SN6": adapters[1],
        }
    finally:
        for bus in opened.values():
            if isinstance(bus, GsUsbBus):
                bus.shutdown()


def test_worker_opens_a_pinned_adapter_by_serial(adapters) -> None:  # type: ignore[no-untyped-def]
    adapters.insert(0, FakeAdapter(address=6))
    cfg = BusConfig(name="CAN_1", interface="gs_usb", channel=0, bitrate=500_000, hw_id="SN5")
    bus = BusWorker("CAN_1", cfg, RingBuffer(8))._open_bus()
    try:
        assert bus.gs_usb is adapters[1]  # type: ignore[attr-defined]
    finally:
        bus.shutdown()


# ---- Multi-channel adapters (e.g. the KRAKA CCU: ch0 BMS bus, ch1 charger bus) ----


@pytest.fixture
def two_channel(adapters: list[FakeAdapter]) -> FakeAdapter:
    adapters[0].channels = 2
    return adapters[0]


def _frame(bus: GsUsbBus, timeout: float = 1.0) -> int:
    msg = bus.recv(timeout)
    assert msg is not None
    return msg.arbitration_id


def test_both_channels_open_at_once_on_one_handle(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    bms = make_bus(listen_only=True)
    charger = make_bus(listen_only=True, device_channel=1)
    assert bms.gs_usb is charger.gs_usb is two_channel
    assert two_channel.modes == {0: _LISTEN_ONLY, 1: _LISTEN_ONLY}
    # Neither open let go of the device: there is one claimed handle.
    assert ("dispose", None) not in two_channel.calls


def test_control_requests_carry_the_channel(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    make_bus(listen_only=True, device_channel=1)
    assert two_channel.requests == [("bt_const", 1), ("set_timing", 1), ("start", 1)]


def test_second_channel_never_usb_resets_the_first(monkeypatch, two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    """A USB reset ends every channel's session on the device (the CCU drops
    all channels to off), so only the first start of a handle resets."""
    monkeypatch.setattr(gs_usb_bus.sys, "platform", "win32")
    make_bus(listen_only=True)
    make_bus(listen_only=True, device_channel=1).flush_tx_buffer()
    assert two_channel.resets == 1


def test_frames_reach_the_bus_of_their_channel(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    bms = make_bus(listen_only=True)
    charger = make_bus(listen_only=True, device_channel=1)
    for can_id, channel in [(0x101, 1), (0x200, 0), (0x102, 1), (0x201, 0)]:
        two_channel.rx.put((can_id, b"\x01", 1000 + can_id, channel))

    # The charger bus reads first and files the BMS frames for the BMS bus.
    assert [_frame(charger), _frame(charger)] == [0x101, 0x102]
    assert [_frame(bms), _frame(bms)] == [0x200, 0x201]
    assert bms.recv(0.02) is None and charger.recv(0.02) is None


def test_both_channels_read_from_their_own_threads(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    bms = make_bus(listen_only=True)
    charger = make_bus(listen_only=True, device_channel=1)
    got: dict[int, list[int]] = {0: [], 1: []}

    def read(bus: GsUsbBus, channel: int) -> None:
        deadline = time.monotonic() + 3
        while len(got[channel]) < 50 and time.monotonic() < deadline:
            msg = bus.recv(0.05)
            if msg is not None:
                got[channel].append(msg.arbitration_id)

    threads = [
        threading.Thread(target=read, args=(bms, 0)),
        threading.Thread(target=read, args=(charger, 1)),
    ]
    for thread in threads:
        thread.start()
    for i in range(50):
        two_channel.rx.put((0x100 + i, b"", i, 0))
        two_channel.rx.put((0x400 + i, b"", i, 1))
    for thread in threads:
        thread.join(5)
    assert got == {0: [0x100 + i for i in range(50)], 1: [0x400 + i for i in range(50)]}


def test_closing_one_channel_leaves_the_other_running(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    bms = make_bus(listen_only=True)
    charger = make_bus(listen_only=True, device_channel=1)

    bms.shutdown()
    assert two_channel.requests[-1] == ("stop", 0)
    assert ("dispose", None) not in two_channel.calls
    assert two_channel.modes == {1: _LISTEN_ONLY}
    # A late frame of the closed channel is dropped, not handed to the other.
    two_channel.rx.put((0x200, b"", 1, 0))
    two_channel.rx.put((0x101, b"", 2, 1))
    assert _frame(charger) == 0x101


def test_last_channel_out_releases_the_device(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    bms = make_bus(listen_only=True)
    charger = make_bus(listen_only=True, device_channel=1)
    bms.shutdown()
    before = len(two_channel.requests)

    charger.shutdown()
    # Stopped and released, then re-armed listen-only on its own channel
    # through a fresh handle that is released too.
    assert two_channel.requests[before:] == [
        ("stop", 1),
        ("set_timing", 1),
        ("start", 1),
        ("stop", 1),
    ]
    assert [name for name, _ in two_channel.calls[-6:]] == [
        "stop",
        "dispose",
        "set_timing",
        "start",
        "stop",
        "dispose",
    ]
    assert two_channel.starts()[-1] == _LISTEN_ONLY


def test_device_reopens_after_both_channels_closed(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    make_bus(listen_only=True).shutdown()
    charger = make_bus(listen_only=True, device_channel=1)
    two_channel.rx.put((0x101, b"", 2, 1))
    assert _frame(charger) == 0x101


def test_sends_go_out_on_their_channel(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    make_bus(listen_only=True)
    bus = make_bus(device_channel=1)
    bus.send(can.Message(arbitration_id=0x123, data=b"\x01", is_extended_id=False))
    assert two_channel.sent == [(0x123, 1)]


def test_a_channel_the_adapter_lacks_is_refused(two_channel) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(can.CanInitializationError, match="has no channel 2"):
        GsUsbBus(channel=0, bitrate=500_000, listen_only=True, device_channel=2)
    assert two_channel.calls == [("dispose", None)]


def test_a_channel_open_twice_is_refused(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    make_bus(listen_only=True, device_channel=1)
    with pytest.raises(can.CanInitializationError, match="channel 1 is already open"):
        GsUsbBus(channel=0, bitrate=500_000, listen_only=True, device_channel=1)
    assert two_channel.modes == {1: _LISTEN_ONLY}


def test_stalled_start_is_a_clear_open_error(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    """The CCU stalls a normal-mode start of its charger bus (always) and of
    its BMS bus while charging. The open fails and the device is let go."""
    two_channel.refuse_normal = {1}
    with pytest.raises(can.CanInitializationError, match="refused to start channel 1") as info:
        GsUsbBus(channel=0, bitrate=500_000, device_channel=1)
    assert "listen-only" in str(info.value)
    assert isinstance(info.value.__cause__, usb.core.USBError)
    assert two_channel.calls[-1] == ("dispose", None)
    # Nothing was left claimed: the same channel opens listen-only next.
    make_bus(listen_only=True, device_channel=1)
    assert two_channel.modes == {1: _LISTEN_ONLY}


def test_stalled_start_leaves_the_other_channel_open(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    two_channel.refuse_normal = {1}
    bms = make_bus(listen_only=True)
    with pytest.raises(can.CanInitializationError, match="refused"):
        GsUsbBus(channel=0, bitrate=500_000, device_channel=1)
    assert ("dispose", None) not in two_channel.calls
    two_channel.rx.put((0x200, b"", 1, 0))
    assert _frame(bms) == 0x200


def test_refused_channel_fails_the_bus_open(qapp, qtbot, two_channel) -> None:  # type: ignore[no-untyped-def]
    two_channel.refuse_normal = {1}
    reader = BusReader()
    try:
        cfg = BusConfig(name="Charger", interface="gs_usb", channel="0:1", bitrate=500_000)
        with qtbot.waitSignal(reader.bus_failed, timeout=3000) as blocker:
            reader.open(cfg)
        assert blocker.args[0] == "Charger"
        assert "refused to start channel 1" in blocker.args[1]
    finally:
        reader.close()


def test_worker_opens_both_channels_and_routes_frames(qapp, qtbot, two_channel) -> None:  # type: ignore[no-untyped-def]
    reader = BusReader()
    try:
        for name, channel in [("BMS", 0), ("Charger", "0:1")]:
            cfg = BusConfig(
                name=name, interface="gs_usb", channel=channel, bitrate=500_000, listen_only=True
            )
            with qtbot.waitSignal(reader.bus_started, timeout=2000):
                reader.open(cfg)
        two_channel.rx.put((0x101, b"\x01", 10, 1))
        two_channel.rx.put((0x200, b"\x02", 20, 0))
        qtbot.waitUntil(
            lambda: reader.rings["BMS"].total_seq >= 1 and reader.rings["Charger"].total_seq >= 1,
            timeout=2000,
        )
        assert reader.rings["BMS"].total_seq == reader.rings["Charger"].total_seq == 1

        reader.close("BMS")
        two_channel.rx.put((0x102, b"\x03", 30, 1))
        qtbot.waitUntil(lambda: reader.rings["Charger"].total_seq >= 2, timeout=2000)
        assert two_channel.modes == {1: _LISTEN_ONLY}
    finally:
        reader.close()


def test_pinned_second_channel_opens_while_the_first_holds_the_adapter(
    two_channel, make_bus
) -> None:  # type: ignore[no-untyped-def]
    """WinUSB refuses a serial read through a second handle while a bus holds
    the adapter; the held adapter's serial is known from its own open."""
    make_bus(listen_only=True, serial="SN5")
    two_channel.held = True
    charger = make_bus(listen_only=True, serial="SN5", device_channel=1)
    assert charger.gs_usb is two_channel
    assert two_channel.modes == {0: _LISTEN_ONLY, 1: _LISTEN_ONLY}


def test_worker_opens_a_pinned_second_channel(adapters, two_channel) -> None:  # type: ignore[no-untyped-def]
    adapters.insert(0, FakeAdapter(address=6))
    cfg = BusConfig(
        name="Charger",
        interface="gs_usb",
        channel="1:1",
        bitrate=500_000,
        listen_only=True,
        hw_id="SN5:1",
    )
    bus = BusWorker("Charger", cfg, RingBuffer(8))._open_bus()
    try:
        assert bus.gs_usb is two_channel  # type: ignore[attr-defined]
        assert two_channel.modes == {1: _LISTEN_ONLY}
    finally:
        bus.shutdown()


def test_single_channel_adapter_is_addressed_on_channel_0_only(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus(listen_only=True)
    bus.flush_tx_buffer()
    bus.shutdown()
    assert {channel for _, channel in adapters[0].requests} == {0}
    with pytest.raises(can.CanInitializationError, match="has no channel 1"):
        GsUsbBus(channel=0, bitrate=500_000, listen_only=True, device_channel=1)


def test_unplug_fails_both_channels(two_channel, make_bus) -> None:  # type: ignore[no-untyped-def]
    bms = make_bus(listen_only=True)
    charger = make_bus(listen_only=True, device_channel=1)
    two_channel.gone = two_channel.read_fails = True
    for bus in (bms, charger):
        with pytest.raises(can.CanOperationError, match="gs_usb adapter lost"):
            bus.recv(1.0)
