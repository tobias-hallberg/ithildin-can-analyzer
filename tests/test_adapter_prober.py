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


def test_add_kvaser_firmware_is_a_noop_without_canlib() -> None:
    """The firmware deepen degrades silently when the kvaser CANlib isn't
    importable (no Kvaser driver on CI) — it must not raise and must leave
    the config dicts untouched so enrichment still reports firmware None."""
    configs: list[dict[str, object]] = [
        {"interface": "kvaser", "channel": 0, "device_name": "Leaf"},
        {"interface": "kvaser", "channel": "weird"},
    ]
    prober_mod._add_kvaser_firmware(configs)
    assert "firmware" not in configs[0]
    assert "firmware" not in configs[1]


def test_detect_gs_usb_lists_scanned_adapters_without_opening_them(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One config per scanned adapter, by index. The fakes have no attributes,
    so reading a descriptor (which opens the device) would raise."""
    import libusb_package
    from gs_usb.gs_usb import GsUsb

    monkeypatch.setattr(libusb_package, "get_libusb1_backend", lambda: None)
    monkeypatch.setattr(GsUsb, "scan", staticmethod(lambda: [object(), object()]))
    assert prober_mod._detect_gs_usb() == [
        {"interface": "gs_usb", "channel": 0},
        {"interface": "gs_usb", "channel": 1},
    ]


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
        with qtbot.waitSignal(t.finished, timeout=2000):
            t.start()

    assert (p, t) not in prober_mod._INFLIGHT_PROBES


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
