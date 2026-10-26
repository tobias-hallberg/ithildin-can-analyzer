# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.adapter_prober.AdapterProber``.

The prober's two important guarantees:

1. The Ithildin Virtual loopback is emitted at the end **in dev mode**
   (``ITHILDIN_DEV``) so devs have a no-hardware path; in normal use it
   is hidden. Most tests here run with dev mode on (autouse fixture);
   ``test_virtual_loopback_hidden_when_not_dev_mode`` covers the off case.
2. A backend whose import hangs or raises does not wedge the dock —
   it produces a ``backend_failed`` signal and the prober moves on.

We patch ``adapter_prober._detect_configs`` per-backend so we can
simulate fast / slow / failing vendor calls without touching real
python-can backends.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt, QThread

from ithildin.ui import adapter_prober as prober_mod
from ithildin.ui.adapter_enrich import DeviceInfo
from ithildin.ui.adapter_prober import (
    AdapterChoice,
    AdapterProber,
    register_prober,
)


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):  # type: ignore[no-untyped-def]
    """Most tests assert the loopback is emitted, which is dev-mode only."""
    monkeypatch.setenv("ITHILDIN_DEV", "1")


def _has_virtual(rows: list) -> bool:
    """The synthetic virtual loopback is emitted at the end in dev mode."""
    return any(getattr(r, "interface", None) == "virtual" for r in rows)


def _stub_per_backend(behaviours: dict[str, object]):
    """Build a ``_detect_configs`` stub that dispatches by backend name.

    ``behaviours[name]`` may be a callable, an exception instance, or a
    list of config dicts to return.
    """

    def fake(backend: str) -> list[dict[str, object]]:
        beh = behaviours.get(backend, [])
        if isinstance(beh, BaseException):
            raise beh
        if callable(beh):
            return list(beh(backend))
        return list(beh)  # type: ignore[arg-type]

    return fake


def test_virtual_loopback_emitted_even_when_no_backend_finds_anything(qtbot) -> None:  # type: ignore[no-untyped-def]
    found: list[AdapterChoice] = []
    failed: list[tuple[str, str]] = []

    p = AdapterProber()
    p.adapter_found.connect(found.append)
    p.backend_failed.connect(lambda b, r: failed.append((b, r)))

    with patch.object(prober_mod, "_detect_configs", _stub_per_backend({})):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    assert _has_virtual(found)
    assert failed == []


def test_virtual_loopback_hidden_when_not_dev_mode(qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """In normal use the loopback is not emitted — a probe that finds no
    hardware finishes with no devices at all."""
    monkeypatch.delenv("ITHILDIN_DEV", raising=False)
    devices: list[DeviceInfo] = []

    p = AdapterProber()
    p.device_found.connect(devices.append)

    with patch.object(prober_mod, "_detect_configs", _stub_per_backend({})):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    assert devices == []


def test_failing_backend_emits_backend_failed_and_continues(qtbot) -> None:  # type: ignore[no-untyped-def]
    found: list[AdapterChoice] = []
    failed: list[tuple[str, str]] = []

    p = AdapterProber()
    p.adapter_found.connect(found.append)
    p.backend_failed.connect(lambda b, r: failed.append((b, r)))

    behaviours = {
        "pcan": RuntimeError("driver missing"),
        "kvaser": [
            {"interface": "kvaser", "channel": 0},
        ],
    }
    with patch.object(prober_mod, "_detect_configs", _stub_per_backend(behaviours)):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    failed_names = [name for name, _ in failed]
    assert "pcan" in failed_names
    # kvaser still produced a result, and the virtual loopback still arrived.
    interfaces = [a.interface for a in found]
    assert "kvaser" in interfaces
    assert _has_virtual(found)


def test_per_backend_timeout_fires_within_limit(qtbot) -> None:  # type: ignore[no-untyped-def]
    """A hung backend probe must time out, not block the whole run.

    Use a short timeout so this test stays fast. Make ``pcan`` hang by
    sleeping past it; the other backends return immediately.
    """
    failed: list[tuple[str, str]] = []
    found: list[AdapterChoice] = []

    def _hang(_backend: str) -> list[dict[str, object]]:
        time.sleep(2.0)
        return []

    p = AdapterProber(timeout_s=0.2)
    p.adapter_found.connect(found.append)
    p.backend_failed.connect(lambda b, r: failed.append((b, r)))

    behaviours = {"pcan": _hang}
    with patch.object(prober_mod, "_detect_configs", _stub_per_backend(behaviours)):
        start = time.monotonic()
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()
        elapsed = time.monotonic() - start

    assert ("pcan", "timeout") in failed
    assert _has_virtual(found)
    # 0.2 s timeout × 3 backends ≤ 1 s total. Generous upper bound.
    assert elapsed < 1.5


def test_duplicate_channels_within_a_backend_are_deduped(qtbot) -> None:  # type: ignore[no-untyped-def]
    found: list[AdapterChoice] = []
    p = AdapterProber()
    p.adapter_found.connect(found.append)

    behaviours = {
        "pcan": [
            {"interface": "pcan", "channel": "PCAN_USBBUS1"},
            {"interface": "pcan", "channel": "PCAN_USBBUS1"},
            {"interface": "pcan", "channel": "PCAN_USBBUS2"},
        ],
    }
    with patch.object(prober_mod, "_detect_configs", _stub_per_backend(behaviours)):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    pcan_channels = [a.channel for a in found if a.interface == "pcan"]
    assert pcan_channels == ["PCAN_USBBUS1", "PCAN_USBBUS2"]


@pytest.mark.parametrize("bad_channel", [None, 3.14, ("tuple",)])
def test_skip_configs_with_unsupported_channel_type(qtbot, bad_channel) -> None:  # type: ignore[no-untyped-def]
    found: list[AdapterChoice] = []
    p = AdapterProber()
    p.adapter_found.connect(found.append)

    behaviours = {
        "pcan": [{"interface": "pcan", "channel": bad_channel}],
    }
    with patch.object(prober_mod, "_detect_configs", _stub_per_backend(behaviours)):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    pcan_seen = [a for a in found if a.interface == "pcan"]
    assert pcan_seen == []
    assert _has_virtual(found)


def test_device_found_emits_enriched_grouped_device(qtbot) -> None:  # type: ignore[no-untyped-def]
    devices: list[DeviceInfo] = []
    p = AdapterProber()
    p.device_found.connect(devices.append)

    behaviours = {
        "kvaser": [
            {
                "interface": "kvaser",
                "channel": 0,
                "device_name": "Leaf Pro",
                "serial": 84621,
                "dongle_channel": 1,
            },
            {
                "interface": "kvaser",
                "channel": 1,
                "device_name": "Leaf Pro",
                "serial": 84621,
                "dongle_channel": 2,
            },
        ],
    }
    with patch.object(prober_mod, "_detect_configs", _stub_per_backend(behaviours)):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    kvaser = [d for d in devices if d.interface == "kvaser"]
    assert len(kvaser) == 1
    assert kvaser[0].serial == "84621"
    assert len(kvaser[0].channels) == 2
    # The virtual device is always emitted too.
    assert _has_virtual(devices)


def test_add_kvaser_channel_data_is_a_noop_without_canlib(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The deepen degrades silently when python-can's kvaser wrapper can't be
    imported — it must not raise and must leave the config dicts untouched
    so enrichment still keeps the channel and reports firmware None."""
    monkeypatch.setitem(sys.modules, "can.interfaces.kvaser", None)
    configs: list[dict[str, object]] = [
        {"interface": "kvaser", "channel": 0, "device_name": "Leaf"},
        {"interface": "kvaser", "channel": "weird"},
    ]
    prober_mod._add_kvaser_channel_data(configs)
    assert configs == [
        {"interface": "kvaser", "channel": 0, "device_name": "Leaf"},
        {"interface": "kvaser", "channel": "weird"},
    ]


def test_add_kvaser_channel_data_reads_card_type_and_firmware(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Channel 1 is a Virtual channel (canHWTYPE_VIRTUAL) with no firmware;
    channel 2's query fails, which leaves its dict alone."""
    from can.interfaces.kvaser import canlib, constants

    def get_channel_data(channel: int, item: int, buf, _size: int) -> int:  # type: ignore[no-untyped-def]
        if channel == 2:
            raise RuntimeError("Specified device not found")
        # buf is a ctypes.byref(); _obj is the buffer it points at.
        if item == constants.canCHANNELDATA_CARD_TYPE:
            buf._obj.value = 1 if channel == 1 else 48
        elif item == constants.canCHANNELDATA_CARD_FIRMWARE_REV and channel == 0:
            buf._obj[:] = [123, 0, 18, 3]
        return 0

    monkeypatch.setattr(canlib, "canGetChannelData", get_channel_data, raising=False)
    configs: list[dict[str, object]] = [
        {"interface": "kvaser", "channel": 0},
        {"interface": "kvaser", "channel": 1},
        {"interface": "kvaser", "channel": 2},
    ]
    prober_mod._add_kvaser_channel_data(configs)
    assert configs == [
        {"interface": "kvaser", "channel": 0, "card_type": 48, "firmware": "3.18.0.123"},
        {"interface": "kvaser", "channel": 1, "card_type": 1},
        {"interface": "kvaser", "channel": 2},
    ]


def test_kvaser_probe_reenumerates_and_detects_under_the_lock(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Re-enumerating renumbers the channels, so a bus opening in between
    must not see a half-done probe: the whole probe holds the lock."""
    from ithildin.core import bus as bus_mod

    calls: list[tuple[str, bool]] = []

    def record(step: str):  # type: ignore[no-untyped-def]
        def call(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            calls.append((step, bus_mod.KVASER_ENUM_LOCK.locked()))
            return [{"interface": "kvaser", "channel": 0}] if step == "detect" else []

        return call

    monkeypatch.setattr(bus_mod, "kvaser_enum_hardware", record("enum"))
    monkeypatch.setattr(bus_mod, "detect_configs", record("detect"))
    monkeypatch.setattr(prober_mod, "_add_kvaser_channel_data", record("channel data"))

    assert prober_mod._detect_configs("kvaser") == [{"interface": "kvaser", "channel": 0}]
    assert calls == [("enum", True), ("detect", True), ("channel data", True)]
    assert not bus_mod.KVASER_ENUM_LOCK.locked()


def test_kvaser_probe_reports_the_driver_busy_while_the_lock_stays_held(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Behind an open hung in the driver, the probe fails on its own before
    the backend timeout, so its thread ends instead of staying parked."""
    from ithildin.core import bus as bus_mod

    assert prober_mod._PROBE_LOCK_WAIT_S < prober_mod.BACKEND_TIMEOUT_S
    enumerated: list[bool] = []
    monkeypatch.setattr(bus_mod, "kvaser_enum_hardware", lambda: enumerated.append(True))
    monkeypatch.setattr(prober_mod, "_PROBE_LOCK_WAIT_S", 0.05)

    with bus_mod.KVASER_ENUM_LOCK, pytest.raises(RuntimeError, match="Kvaser driver busy"):
        prober_mod._detect_configs("kvaser")
    assert enumerated == []


def test_gs_usb_probe_reports_the_driver_busy_while_a_bus_opens(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Its serial reads would make that open fail, so it waits, but gives up
    before the backend timeout."""
    import libusb_package
    from gs_usb.gs_usb import GsUsb

    from ithildin.core.gs_usb_bus import GS_USB_LOCK

    scanned: list[bool] = []
    monkeypatch.setattr(libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(GsUsb, "scan", staticmethod(lambda: scanned.append(True) or []))
    monkeypatch.setattr(prober_mod, "_PROBE_LOCK_WAIT_S", 0.05)

    with GS_USB_LOCK, pytest.raises(RuntimeError, match="gs_usb driver busy"):
        prober_mod._detect_gs_usb()
    assert scanned == []


class _ScannedGsUsb:
    """A scanned gs_usb adapter whose serial read opens the device, as
    pyusb's does; ``held`` makes it fail like WinUSB with the adapter open
    elsewhere."""

    def __init__(self, serial: str, *, held: bool = False) -> None:
        self._serial = serial
        self.held = held
        self.gs_usb = self
        self.open = False

    @property
    def serial_number(self) -> str:
        import usb.core

        if self.held:
            raise usb.core.USBError("Access denied (insufficient permissions)", -3, 13)
        self.open = True
        return self._serial


def test_detect_gs_usb_reads_each_serial_and_lets_go_of_the_adapter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One config per scanned adapter, by index, with its serial; an adapter a
    bus holds open has none. Every handle the reads opened is released."""
    import libusb_package
    import usb.util
    from gs_usb.gs_usb import GsUsb

    adapters = [_ScannedGsUsb("206C3EA1"), _ScannedGsUsb("0045003C", held=True)]
    monkeypatch.setattr(libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(GsUsb, "scan", staticmethod(lambda: adapters))
    monkeypatch.setattr(usb.util, "dispose_resources", lambda dev: setattr(dev, "open", False))
    assert prober_mod._detect_gs_usb() == [
        {"interface": "gs_usb", "channel": 0, "serial": "206C3EA1"},
        {"interface": "gs_usb", "channel": 1, "serial": None},
    ]
    assert [a.open for a in adapters] == [False, False]


def test_gs_usb_is_scanned_on_windows_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """On Linux the kernel driver owns the adapter and it shows up as SocketCAN."""
    scanned: list[bool] = []
    monkeypatch.setattr(prober_mod, "_detect_gs_usb", lambda: scanned.append(True) or [])
    assert prober_mod._detect_configs("gs_usb") == []
    assert scanned == ([True] if sys.platform == "win32" else [])


def test_adapter_choice_back_compat_equality() -> None:
    """A hand-built choice (device=None) compares equal to legacy code."""
    a = AdapterChoice(label="x", interface="virtual", channel="c")
    b = AdapterChoice(label="x", interface="virtual", channel="c")
    assert a == b
    assert a.device is None


def test_cancel_short_circuits_run_but_still_finishes(qtbot) -> None:  # type: ignore[no-untyped-def]
    """A cancelled probe emits no devices but still fires finished so its
    owner's teardown chain runs."""
    devices: list[DeviceInfo] = []
    p = AdapterProber()
    p.device_found.connect(devices.append)
    p.cancel()  # cancel before the loop starts

    with patch.object(prober_mod, "_detect_configs", _stub_per_backend({})):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    # Cancelled before any backend → not even the virtual device is emitted.
    assert devices == []


def test_register_prober_retains_until_thread_finished(qtbot) -> None:  # type: ignore[no-untyped-def]
    """A registered probe is held until its thread ends, then released —
    this is what stops a transient owner's close from tearing the thread
    down mid-run."""
    p = AdapterProber()
    t = QThread()
    p.moveToThread(t)
    t.started.connect(p.run)
    p.finished.connect(t.quit, Qt.ConnectionType.DirectConnection)
    p.finished.connect(p.deleteLater)
    t.finished.connect(t.deleteLater)

    with patch.object(prober_mod, "_detect_configs", _stub_per_backend({})):
        register_prober(p, t)
        assert (p, t) in prober_mod._INFLIGHT_PROBES
        t.start()
        # Not a waitSignal on t.finished: its queued delivery is lost when
        # t.deleteLater runs first.
        qtbot.waitUntil(lambda: (p, t) not in prober_mod._INFLIGHT_PROBES, timeout=5000)


def test_hung_probe_helper_is_daemon(qtbot) -> None:  # type: ignore[no-untyped-def]
    """A timed-out probe thread is abandoned; it must not hold up interpreter exit."""
    import threading

    release = threading.Event()

    def _hang(_backend: str) -> list[dict[str, object]]:
        release.wait()
        return []

    p = AdapterProber(timeout_s=0.1)
    before = set(threading.enumerate())
    try:
        with patch.object(prober_mod, "_detect_configs", _stub_per_backend({"pcan": _hang})):
            with qtbot.waitSignal(p.finished, timeout=2000):
                p.run()
        (helper,) = [t for t in threading.enumerate() if t.name == "probe-pcan" and t not in before]
        assert helper.daemon
    finally:
        release.set()


def test_skipped_backend_is_not_probed(qtbot) -> None:  # type: ignore[no-untyped-def]
    """A backend an earlier probe timed out on still has a hung thread:
    probing it again would hang another."""
    probed: list[str] = []
    done: list[str] = []

    def record(backend: str) -> list[dict[str, object]]:
        probed.append(backend)
        return []

    p = AdapterProber(skip_backends=frozenset({"pcan"}))
    p.backend_done.connect(done.append)
    with patch.object(prober_mod, "_detect_configs", record):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    assert "pcan" not in probed and "pcan" not in done
    assert "kvaser" in probed and "kvaser" in done


def test_two_ixxat_devices_each_get_their_channel_0(qtbot) -> None:  # type: ignore[no-untyped-def]
    found: list[AdapterChoice] = []
    p = AdapterProber()
    p.adapter_found.connect(found.append)

    behaviours = {
        "ixxat": [
            {"interface": "ixxat", "channel": 0, "unique_hardware_id": "HW100001"},
            {"interface": "ixxat", "channel": 0, "unique_hardware_id": "HW200002"},
        ],
    }
    with patch.object(prober_mod, "_detect_configs", _stub_per_backend(behaviours)):
        with qtbot.waitSignal(p.finished, timeout=2000):
            p.run()

    assert [(a.channel, a.hw_id) for a in found if a.interface == "ixxat"] == [
        (0, "HW100001"),
        (0, "HW200002"),
    ]
