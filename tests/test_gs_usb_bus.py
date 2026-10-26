# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""``core.gs_usb_bus.GsUsbBus`` against fake gs_usb adapters: no USB, no libusb."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import can
import libusb_package
import pytest
import usb.core
import usb.util
from gs_usb.constants import GS_CAN_MODE_HW_TIMESTAMP, GS_CAN_MODE_LISTEN_ONLY
from gs_usb.gs_usb_frame import GS_USB_NONE_ECHO_ID

from ithildin.app_state.bus_reader import BusReader
from ithildin.core import gs_usb_bus
from ithildin.core.bus import BusWorker
from ithildin.core.gs_usb_bus import GsUsbBus
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import BusHealth
from ithildin.workspace.schema import BusConfig

_LISTEN_ONLY = GS_CAN_MODE_LISTEN_ONLY | GS_CAN_MODE_HW_TIMESTAMP


class FakeAdapter:
    """One adapter as gs_usb's ``GsUsb`` presents it.

    * ``feature``: the firmware's feature bits; ``no_driver`` makes the
      first control transfer raise like libusb without WinUSB bound.
    * ``gone``: unplugged. Control transfers raise ``USBError`` and the
      fake ``scan`` / ``find`` skip it.
    * ``read_fails``: every read returns False at once, as gs_usb's read
      does on any USB error.
    * ``rx``: ``(can_id, data, timestamp_us)`` frames, one per read; an
      empty queue makes the read wait out its timeout.
    * ``calls``: ``(name, args)`` per set_timing / start / stop and per
      ``usb.util.dispose_resources`` of its pyusb device (``gs_usb``, the
      fake itself), in order.
    * ``sent``: the CAN ID of every frame written to the adapter.
    * ``serial``: its USB serial; ``held`` makes reading it fail as WinUSB
      does while another handle has the adapter open.
    """

    def __init__(self, address: int, feature: int = _LISTEN_ONLY, serial: str = "") -> None:
        self.bus = 1
        self.address = address
        self.feature = feature
        self.serial = serial or f"SN{address}"
        self.held = False
        self.no_driver = False
        self.gone = False
        self.read_fails = False
        self.rx: queue.SimpleQueue[tuple[int, bytes, int]] = queue.SimpleQueue()
        self.calls: list[tuple[str, object]] = []
        self.sent: list[int] = []
        self.gs_usb = self
        self.device_flags = 0
        self.reads = 0
        self.probes = 0

    @property
    def device_capability(self) -> SimpleNamespace:
        if self.no_driver:
            raise NotImplementedError("Operation not supported or unimplemented on this platform")
        return SimpleNamespace(feature=self.feature, fclk_can=48_000_000)

    @property
    def serial_number(self) -> str:
        if self.gone or self.held:
            raise usb.core.USBError("Access denied (insufficient permissions)", -3, 13)
        return self.serial

    @property
    def device_info(self) -> SimpleNamespace:
        self.probes += 1
        if self.gone:
            raise usb.core.USBError("No such device (it may have been disconnected)", -4, 19)
        return SimpleNamespace(fw_version=20, hw_version=10)

    def set_timing(
        self, prop_seg: int, phase_seg1: int, phase_seg2: int, sjw: int, brp: int
    ) -> None:
        self.calls.append(("set_timing", (prop_seg, phase_seg1, phase_seg2, sjw, brp)))

    def start(self, flags: int) -> None:
        self.device_flags = flags & self.feature
        self.calls.append(("start", flags))

    def stop(self) -> None:
        self.calls.append(("stop", None))

    def send(self, frame: Any) -> None:
        self.sent.append(frame.can_id)

    def read(self, frame: Any, timeout_ms: int) -> bool:
        self.reads += 1
        if self.read_fails:
            return False
        try:
            can_id, data, timestamp_us = self.rx.get_nowait()
        except queue.Empty:
            time.sleep(timeout_ms / 1000)
            return False
        frame.can_id = can_id
        frame.can_dlc = len(data)
        frame.data = list(data.ljust(8, b"\0"))
        frame.timestamp_us = timestamp_us
        frame.echo_id = GS_USB_NONE_ECHO_ID
        return True

    def starts(self) -> list[object]:
        return [flags for name, flags in self.calls if name == "start"]


@pytest.fixture
def adapters(monkeypatch: pytest.MonkeyPatch) -> list[FakeAdapter]:
    """The plugged-in adapters, in scan order; one to start with."""
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
    return plugged


@pytest.fixture
def make_bus(adapters: list[FakeAdapter]) -> Iterator[Callable[..., GsUsbBus]]:
    buses: list[GsUsbBus] = []

    def make(channel: int = 0, *, listen_only: bool = False) -> GsUsbBus:
        bus = GsUsbBus(channel=channel, bitrate=500_000, listen_only=listen_only)
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
    assert bus.recv(0.05) is None
    assert adapters[0].probes == 0


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
    assert adapters[0].calls[-1] == ("stop", None)


def test_shutdown_releases_its_handle_before_rearming(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    """WinUSB refuses the re-arm's fresh handle while the bus's own is open."""
    bus = make_bus()
    timing, start = adapters[0].calls

    bus.shutdown()
    assert adapters[0].calls[2:] == [
        ("stop", None),
        ("dispose", None),
        timing,
        start,
        ("stop", None),
    ]


def test_send_after_shutdown_never_reaches_the_adapter(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    bus = make_bus()
    bus.send(can.Message(arbitration_id=0x100, data=b"", is_extended_id=False))

    bus.shutdown()
    with pytest.raises(can.CanOperationError, match="shut down"):
        bus.send(can.Message(arbitration_id=0x101, data=b"", is_extended_id=False))
    assert adapters[0].sent == [0x100]


def test_shutdown_after_unplug_leaves_other_adapters_alone(adapters, make_bus) -> None:  # type: ignore[no-untyped-def]
    """Once the first adapter is gone, the second one has its scan index."""
    adapters.append(FakeAdapter(address=6))
    bus = make_bus(0)
    adapters[0].gone = True

    bus.shutdown()
    assert adapters[1].calls == []


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

    @property
    def device_capability(self) -> SimpleNamespace:
        self.claim()
        return super().device_capability


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
