# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Adapter identity: ``hw_id`` formats, channel keys, user-private pins, and
the worker binding a pinned bus to its adapter wherever it enumerates."""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

import can
import pytest

from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.project import open_or_migrate
from ithildin.core import bus as bus_mod
from ithildin.core.bus import KVASER_ENUM_LOCK, BusWorker, _pin_adapter
from ithildin.core.ringbuf import RingBuffer
from ithildin.ui import adapter_prober as prober_mod
from ithildin.workspace.schema import BusConfig, channel_key, channel_keys, hw_id_of


def _pcan(channel: str, device_id: int, controller: int = 0) -> dict[str, object]:
    """A PCAN config as python-can's detection reports it (pcan.py)."""
    return {
        "interface": "pcan",
        "channel": channel,
        "device_id": device_id,
        "controller_number": controller,
        "device_name": "PCAN-USB",
    }


def _kvaser(channel: int, serial: int, dongle_channel: int = 1) -> dict[str, object]:
    return {
        "interface": "kvaser",
        "channel": channel,
        "serial": serial,
        "dongle_channel": dongle_channel,
        "device_name": "Kvaser Leaf Light v2",
    }


# ---- Formats and keys ----


@pytest.mark.parametrize(
    ("interface", "detected", "hw_id"),
    [
        ("pcan", _pcan("PCAN_USBBUS1", 0x2A, 1), "0000002a.1"),
        ("pcan", _pcan("PCAN_USBBUS1", 0xFF), None),
        ("pcan", _pcan("PCAN_USBBUS1", 0xFFFFFFFF), None),
        ("pcan", {"interface": "pcan", "channel": "PCAN_USBBUS1"}, None),
        ("kvaser", _kvaser(0, 84621, 2), "84621.2"),
        ("kvaser", _kvaser(0, 0), None),
        ("ixxat", {"channel": 1, "unique_hardware_id": "HW123456"}, "HW123456"),
        ("gs_usb", {"channel": 0, "serial": "206C3EA1"}, "206C3EA1"),
        ("gs_usb", {"channel": 0, "serial": None}, None),
        ("socketcan", {"channel": "can0"}, None),
        ("virtual", {"channel": "x"}, None),
    ],
)
def test_hw_id_of(interface: str, detected: dict[str, object], hw_id: str | None) -> None:
    assert hw_id_of(interface, detected) == hw_id


def test_channel_key_names_the_adapter_when_pinned() -> None:
    assert channel_key("pcan", "PCAN_USBBUS1", None) == ("pcan", None, "PCAN_USBBUS1")
    # pcan, kvaser and gs_usb ids name a channel wherever it enumerates.
    assert channel_key("pcan", "PCAN_USBBUS1", "0000002a.0") == ("pcan", "0000002a.0", None)
    assert channel_key("gs_usb", 0, "206C3EA1") == channel_key("gs_usb", 3, "206C3EA1")
    # An IXXAT id names the device: its channel index still counts.
    assert channel_key("ixxat", 0, "HW1") != channel_key("ixxat", 1, "HW1")


def test_buses_clash_on_the_adapter_or_the_configured_channel() -> None:
    pinned = channel_keys("pcan", "PCAN_USBBUS1", "0000002a.0")
    assert not pinned.isdisjoint(channel_keys("pcan", "PCAN_USBBUS2", "0000002a.0"))
    assert pinned.isdisjoint(channel_keys("pcan", "PCAN_USBBUS2", "0000002b.0"))
    # The .itp carries no pins, so two buses never share a configured channel.
    assert not pinned.isdisjoint(channel_keys("pcan", "PCAN_USBBUS1", "0000002b.0"))
    assert not pinned.isdisjoint(channel_keys("pcan", "PCAN_USBBUS1", None))


# ---- Pins are user-private ----


def _write_project(tmp_path: Path) -> Path:
    path = tmp_path / "bench.itp"
    path.write_text(
        "\n".join(
            [
                "schema_version = 2",
                'name = "Bench"',
                "[[bus]]",
                'name = "Engine"',
                'interface = "pcan"',
                'channel = "PCAN_USBBUS1"',
                "bitrate = 500000",
                "[[bus]]",
                'name = "Body"',
                'interface = "kvaser"',
                "channel = 0",
                "bitrate = 500000",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_pins_stay_out_of_the_itp_and_come_back_from_the_user_dir(tmp_path: Path) -> None:
    user_root = tmp_path / "users"
    project, _report = open_or_migrate(_write_project(tmp_path), user_root=user_root)
    assert [b.hw_id for b in project.workspace.bus] == [None, None]

    project.workspace.bus = [
        project.workspace.bus[0].model_copy(update={"hw_id": "0000002a.0"}),
        project.workspace.bus[1],
    ]
    assert "hw_id" not in project.workspace.bus[0].model_dump()
    project.save()

    assert project.path is not None
    assert "hw_id" not in project.path.read_text(encoding="utf-8")
    assert json.loads(project.adapter_pins_path.read_text(encoding="utf-8")) == {
        "Engine": "0000002a.0"
    }
    assert project.adapter_pins_path.parent.parent == user_root.resolve()
    reopened, _report = open_or_migrate(project.path, user_root=user_root)
    assert [b.hw_id for b in reopened.workspace.bus] == ["0000002a.0", None]


def test_a_project_without_pins_writes_no_pins_file(tmp_path: Path) -> None:
    project, _report = open_or_migrate(_write_project(tmp_path), user_root=tmp_path / "users")
    project.save()
    assert not project.adapter_pins_path.exists()


def test_unreadable_pins_pin_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    user_root = tmp_path / "users"
    path = _write_project(tmp_path)
    pins = user_root / "Bench" / "adapter_pins.json"
    pins.parent.mkdir(parents=True)
    pins.write_text("{not json", encoding="utf-8")

    project, _report = open_or_migrate(path, user_root=user_root)

    assert [b.hw_id for b in project.workspace.bus] == [None, None]
    (record,) = [r for r in caplog.records if r.name == "ithildin.app_state.project"]
    assert record.levelno == logging.ERROR


# ---- The worker binds the pinned adapter ----


class _Detected:
    """Stands in for ``bus.detect_configs`` and ``can.Bus``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, configs: list[dict[str, object]]) -> None:
        self.configs = configs
        self.detect_calls: list[str] = []
        self.opened: list[dict[str, object]] = []
        monkeypatch.setattr(bus_mod, "detect_configs", self._detect)
        monkeypatch.setattr(can, "Bus", self._bus)

    def _detect(self, interface: str) -> list[dict[str, object]]:
        self.detect_calls.append(interface)
        return [c for c in self.configs if c["interface"] == interface]

    def _bus(self, **kwargs: object) -> object:
        self.opened.append(kwargs)
        return object()


def _pinned(interface: str, channel: str | int, hw_id: str | None) -> BusConfig:
    return BusConfig(
        name="Engine",
        interface=interface,  # type: ignore[arg-type]
        channel=channel,
        bitrate=500_000,
        hw_id=hw_id,
    )


def test_pin_keeps_the_configured_channel_while_it_has_the_adapter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    detected = _Detected(monkeypatch, [_pcan("PCAN_USBBUS1", 0x2A), _pcan("PCAN_USBBUS2", 0x2B)])
    kwargs: dict[str, object] = {"channel": "PCAN_USBBUS1"}

    _pin_adapter(_pinned("pcan", "PCAN_USBBUS1", "0000002a.0"), kwargs)

    assert kwargs["channel"] == "PCAN_USBBUS1"
    assert detected.detect_calls == ["pcan"]


def test_pin_follows_the_adapter_to_its_new_channel(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level(logging.INFO, logger="ithildin.core.bus")
    _Detected(monkeypatch, [_pcan("PCAN_USBBUS1", 0x2B), _pcan("PCAN_USBBUS2", 0x2A)])
    kwargs: dict[str, object] = {"channel": "PCAN_USBBUS1"}

    _pin_adapter(_pinned("pcan", "PCAN_USBBUS1", "0000002a.0"), kwargs)

    assert kwargs["channel"] == "PCAN_USBBUS2"
    assert "moved from PCAN_USBBUS1 to PCAN_USBBUS2" in caplog.text


def test_pin_refuses_a_missing_adapter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _Detected(monkeypatch, [_kvaser(0, 11111)])
    with pytest.raises(can.CanInitializationError, match="is not attached"):
        _pin_adapter(_pinned("kvaser", 0, "84621.1"), {"channel": 0})


def test_pin_refuses_an_identity_several_adapters_report(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _Detected(monkeypatch, [_pcan("PCAN_USBBUS2", 0x2A), _pcan("PCAN_USBBUS3", 0x2A)])
    with pytest.raises(can.CanInitializationError) as refused:
        _pin_adapter(_pinned("pcan", "PCAN_USBBUS1", "0000002a.0"), {"channel": "PCAN_USBBUS1"})
    assert str(refused.value) == (
        "2 adapters report the identity this bus is pinned to; "
        "give each adapter its own device ID in PCAN-View"
    )


def test_unpinned_bus_detects_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    detected = _Detected(monkeypatch, [])
    kwargs: dict[str, object] = {"channel": "PCAN_USBBUS1"}
    _pin_adapter(_pinned("pcan", "PCAN_USBBUS1", None), kwargs)
    assert detected.detect_calls == []
    assert kwargs == {"channel": "PCAN_USBBUS1"}


def test_detection_runs_on_the_calling_thread(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """python-can's own runs in a pool the interpreter joins at exit, and gives
    up on a slow driver while the driver goes on, outside any lock held here."""
    threads: list[threading.Thread] = []

    class Backend:
        @staticmethod
        def _detect_available_configs() -> list[dict[str, object]]:
            threads.append(threading.current_thread())
            return [{"channel": "PCAN_USBBUS1"}, {"interface": "pcan", "channel": "PCAN_USBBUS2"}]

    monkeypatch.setattr(bus_mod, "_get_class_for_interface", lambda _interface: Backend)

    assert bus_mod.detect_configs("pcan") == [
        {"interface": "pcan", "channel": "PCAN_USBBUS1"},
        {"interface": "pcan", "channel": "PCAN_USBBUS2"},
    ]
    assert threads == [threading.current_thread()]


@pytest.mark.parametrize(
    "error", [can.CanInterfaceNotImplementedError("no vendor module"), NotImplementedError()]
)
def test_a_backend_that_cannot_detect_has_no_channels(monkeypatch, error) -> None:  # type: ignore[no-untyped-def]
    class Backend:
        @staticmethod
        def _detect_available_configs() -> list[dict[str, object]]:
            raise NotImplementedError

    def resolve(_interface: str) -> type[Backend]:
        if isinstance(error, can.CanInterfaceNotImplementedError):
            raise error
        return Backend

    monkeypatch.setattr(bus_mod, "_get_class_for_interface", resolve)
    assert bus_mod.detect_configs("kvaser") == []


@pytest.mark.skipif(
    sys.platform != "win32", reason="python-can's IXXAT module imports on Windows only"
)
def test_ixxat_open_asks_for_the_pinned_device(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    detected = _Detected(monkeypatch, [])
    cfg = _pinned("ixxat", 0, "HW123456")

    BusWorker(cfg.name, cfg, RingBuffer(8))._open_bus()

    assert detected.opened[0]["unique_hardware_id"] == "HW123456"
    assert detected.detect_calls == []


@pytest.mark.skipif(
    sys.platform != "win32", reason="python-can's IXXAT module imports on Windows only"
)
def test_ixxat_pinned_device_missing_reads_not_attached(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from can.interfaces.ixxat.exceptions import VCIDeviceNotFoundError

    def refuse(**_kwargs: object) -> object:
        raise VCIDeviceNotFoundError("Unique HW ID HW123456 not connected or not available.")

    monkeypatch.setattr(can, "Bus", refuse)
    cfg = _pinned("ixxat", 0, "HW123456")
    with pytest.raises(can.CanInitializationError, match="is not attached"):
        BusWorker(cfg.name, cfg, RingBuffer(8))._open_bus()


def test_missing_pinned_adapter_fails_the_bus_without_opening_anything(
    qapp, qtbot, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    detected = _Detected(monkeypatch, [_pcan("PCAN_USBBUS1", 0x2B)])
    reader = BusReader()
    try:
        with qtbot.waitSignal(reader.bus_failed, timeout=5000) as blocker:
            reader.open(_pinned("pcan", "PCAN_USBBUS1", "0000002a.0"))
        assert blocker.args[0] == "Engine"
        assert "is not attached" in reader.failures()["Engine"]
        assert detected.opened == []
    finally:
        reader.close()


def test_kvaser_pin_resolves_inside_the_enumeration_lock(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Re-enumerating renumbers the channels: the number the pin picks must
    be the one ``can.Bus`` opens."""
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        bus_mod, "kvaser_enum_hardware", lambda: calls.append(("enum", KVASER_ENUM_LOCK.locked()))
    )

    def detect(interface: str) -> list[dict[str, object]]:
        calls.append(("detect", KVASER_ENUM_LOCK.locked()))
        return [_kvaser(0, 11111), _kvaser(1, 84621)]

    def open_bus(**kwargs: object) -> object:
        calls.append((f"bus {kwargs['channel']}", KVASER_ENUM_LOCK.locked()))
        return object()

    monkeypatch.setattr(bus_mod, "detect_configs", detect)
    monkeypatch.setattr(can, "Bus", open_bus)
    cfg = _pinned("kvaser", 0, "84621.1")

    BusWorker(cfg.name, cfg, RingBuffer(8))._open_bus()

    assert calls == [("enum", True), ("detect", True), ("bus 1", True)]


def test_kvaser_pin_waits_for_a_probe_holding_the_lock(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A dock probe mid-enumeration and a reconnect's pin never interleave."""
    in_probe = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def enum() -> None:
        if threading.current_thread().name == "probe":
            in_probe.set()
            release.wait(5)
        order.append(f"enum {threading.current_thread().name}")

    def detect(interface: str) -> list[dict[str, object]]:
        order.append(f"detect {threading.current_thread().name}")
        return [_kvaser(0, 84621)]

    monkeypatch.setattr(bus_mod, "kvaser_enum_hardware", enum)
    monkeypatch.setattr(bus_mod, "detect_configs", detect)
    monkeypatch.setattr(can, "Bus", lambda **_kwargs: object())
    monkeypatch.setattr(prober_mod, "_add_kvaser_channel_data", lambda _configs: None)
    cfg = _pinned("kvaser", 0, "84621.1")
    probe = threading.Thread(target=prober_mod._detect_configs, args=("kvaser",), name="probe")
    worker = threading.Thread(
        target=BusWorker(cfg.name, cfg, RingBuffer(8))._open_bus, name="worker"
    )
    try:
        probe.start()
        assert in_probe.wait(5)
        worker.start()
        worker.join(0.3)
        assert worker.is_alive()
        assert order == []
    finally:
        release.set()
        probe.join(5)
        worker.join(5)
    assert order == ["enum probe", "detect probe", "enum worker", "detect worker"]
