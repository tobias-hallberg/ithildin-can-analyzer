# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.create_bus_dialog.CreateCanBusDialog``.

The dialog runs its own ``AdapterProber`` on open. We patch
``adapter_prober._detect_configs`` so no real vendor module is imported
during tests — the synchronously-seeded virtual device is enough for the
form logic, and a stub feeds hardware devices where a test needs them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from can.interfaces.pcan.basic import PCAN_BITRATES

from ithildin.ui import adapter_prober as prober_mod
from ithildin.ui.adapter_enrich import ChannelInfo, DeviceInfo
from ithildin.ui.create_bus_dialog import CreateCanBusDialog
from ithildin.workspace.schema import BusConfig, channel_keys
from tests.conftest import make_app_context


@pytest.fixture(autouse=True)
def _no_real_probe(monkeypatch):  # type: ignore[no-untyped-def]
    """Keep the dialog's prober from importing real vendor backends, and
    enable dev mode so the virtual loopback the form tests rely on is
    seeded. The hidden-in-normal-use case has its own test below."""
    monkeypatch.setattr(prober_mod, "_detect_configs", lambda _backend: [])
    monkeypatch.setenv("ITHILDIN_DEV", "1")


def _make_dialog(qtbot, existing=frozenset(), existing_buses=(), hung_backends=None):  # type: ignore[no-untyped-def]
    """``existing_buses``: ``(interface, channel, hw_id)`` of the declared buses."""
    keys = frozenset().union(*(channel_keys(*bus) for bus in existing_buses))
    dlg = CreateCanBusDialog(
        existing_names=set(existing), existing_keys=keys, hung_backends=hung_backends
    )
    qtbot.addWidget(dlg)
    dlg.show()
    return dlg


def test_seeds_virtual_device_and_selects_channel(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    assert dlg._current_device() is not None
    assert dlg._current_device().interface == "virtual"
    # Virtual device has two channels → pills shown, first free selected.
    assert dlg._channel is not None
    assert dlg._channel.channel == "ithildin_virtual"


def test_loopback_hidden_in_normal_mode_shows_empty_hint(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch
) -> None:
    """With dev mode off and no hardware, the virtual loopback is not
    seeded: the device list is empty, the empty-state hint is shown, and
    neither create button is enabled."""
    monkeypatch.delenv("ITHILDIN_DEV", raising=False)
    dlg = _make_dialog(qtbot)
    qtbot.waitUntil(lambda: dlg._prober is None, timeout=2000)  # probe done
    assert dlg._device_combo.count() == 0
    assert dlg._current_device() is None
    assert dlg._empty_hint.isVisible()
    assert not dlg._info_strip.isVisible()
    assert not dlg._create_btn.isEnabled()
    assert not dlg._connect_btn.isEnabled()


def test_empty_name_disables_create(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    dlg._name_edit.setText("")
    assert not dlg._create_btn.isEnabled()
    assert not dlg._connect_btn.isEnabled()
    assert not dlg._name_error.isHidden()


def test_duplicate_name_disables_create(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot, existing={"Powertrain"})
    dlg._name_edit.setText("Powertrain")
    assert not dlg._create_btn.isEnabled()
    assert not dlg._name_error.isHidden()
    dlg._name_edit.setText("Chassis")
    assert dlg._create_btn.isEnabled()
    assert dlg._name_error.isHidden()


def test_already_added_channel_is_skipped(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    # Virtual device has two channels; mark the first as already declared.
    dlg = _make_dialog(qtbot, existing_buses=[("virtual", "ithildin_virtual", None)])
    assert dlg._channel is not None
    assert dlg._channel.channel == "ithildin_virtual_1"


def test_all_channels_added_disables_create(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(
        qtbot,
        existing_buses=[
            ("virtual", "ithildin_virtual", None),
            ("virtual", "ithildin_virtual_1", None),
        ],
    )
    assert dlg._channel is None
    assert not dlg._create_btn.isEnabled()
    assert dlg._channel_box.isVisible()


def test_auto_name_suggestion_stops_after_manual_edit(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    assert dlg._name_edit.text()  # virtual device suggested a name
    dlg._name_edit.setText("MyBus")
    dlg._on_name_edited("MyBus")  # simulates textEdited (user typing)
    # Re-selecting the device must not clobber the manual value.
    dlg._on_device_changed(0)
    assert dlg._name_edit.text() == "MyBus"


def test_create_emits_config_with_field_defaults(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    dlg._name_edit.setText("Bench")
    captured: list[BusConfig] = []
    dlg.bus_declared.connect(lambda cfg, _dbcs: captured.append(cfg))
    connect_seen: list[BusConfig] = []
    dlg.bus_declared_and_connect.connect(lambda cfg, _dbcs: connect_seen.append(cfg))

    dlg._on_create()

    assert len(captured) == 1 and connect_seen == []
    cfg = captured[0]
    assert cfg.name == "Bench"
    assert cfg.interface == "virtual"
    assert cfg.channel == "ithildin_virtual"
    assert cfg.bitrate == 500_000
    assert cfg.fd is False
    assert cfg.listen_only is False
    assert cfg.connect_on_startup is False


def test_listen_only_toggle_flips_state(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    dlg._name_edit.setText("Bench")
    assert dlg._listen_only.is_on() is False
    dlg._listen_only._switch.setChecked(True)
    assert dlg._listen_only.is_on() is True

    captured: list[BusConfig] = []
    dlg.bus_declared.connect(lambda cfg, _dbcs: captured.append(cfg))
    dlg._on_create()
    assert captured[0].listen_only is True


def test_create_emits_picked_dbcs(qapp, qtbot, example_dbc_path) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    dlg._name_edit.setText("Bench")
    dlg._dbc_picker._add_path(example_dbc_path)
    qtbot.waitUntil(
        lambda: dlg._dbc_picker.is_ready() and bool(dlg._dbc_picker.results()),
        timeout=4000,
    )
    payload: list = []
    dlg.bus_declared.connect(lambda cfg, dbcs: payload.append((cfg, dbcs)))

    dlg._on_create()

    assert len(payload) == 1
    _cfg, dbcs = payload[0]
    assert [p for p, _decoder in dbcs] == [example_dbc_path]


def test_failed_dbc_blocks_create(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A DBC that fails to parse disables Create until it's removed, so the
    bus can't be created having silently dropped a database the user added."""
    dlg = _make_dialog(qtbot)
    dlg._name_edit.setText("Bench")
    assert dlg._create_btn.isEnabled()  # ready before adding anything

    bad = tmp_path / "broken.dbc"
    bad.write_text("this is not a dbc", encoding="utf-8")
    dlg._dbc_picker._add_path(bad)
    qtbot.waitUntil(lambda: dlg._dbc_picker._entries[bad].state == "error", timeout=4000)
    assert not dlg._create_btn.isEnabled()
    assert not dlg._connect_btn.isEnabled()

    dlg._dbc_picker.remove_path(bad)
    assert dlg._create_btn.isEnabled()


def test_ixxat_locks_listen_only_off(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    ixxat = DeviceInfo(
        interface="ixxat",
        vendor="IXXAT",
        model="USB-to-CAN",
        channels=(ChannelInfo(channel="0", label="0"),),
    )
    dlg._add_device(ixxat)
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)
    assert dlg._current_device().interface == "ixxat"
    assert not dlg._listen_only._switch.isEnabled()
    assert dlg._listen_only.is_on() is False


def test_socketcan_locks_listen_only_off(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """socketcan silent mode is OS-level only; the toggle must lock off so the
    app never claims enforcement it can't deliver."""
    dlg = _make_dialog(qtbot)
    sc = DeviceInfo(
        interface="socketcan",
        vendor="SocketCAN",
        model="can0",
        channels=(ChannelInfo(channel="can0", label="can0"),),
    )
    dlg._add_device(sc)
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)
    assert dlg._current_device().interface == "socketcan"
    assert not dlg._listen_only._switch.isEnabled()
    assert dlg._listen_only.is_on() is False


def test_info_strip_omits_unprobed_fields(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """A device that probed neither status nor capabilities (e.g. kvaser)
    shows only the static transport label plus firmware if known — never a
    faked ``available`` / ``CAN 2.0``."""
    dlg = _make_dialog(qtbot)
    dev = DeviceInfo(
        interface="kvaser",
        vendor="Kvaser",
        model="Leaf Pro HS v2",
        serial="84621",
        firmware="3.18.0.123",
        transport="USB",
        channels=(ChannelInfo(channel=0, label="ch0"),),
    )
    dlg._add_device(dev)
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)
    assert dlg._info_strip.isVisible()
    text = dlg._info_strip.text()
    assert "USB" in text
    assert "fw 3.18.0.123" in text
    assert "●" not in text  # no status dot
    assert "available" not in text
    assert "CAN 2.0" not in text


def test_info_strip_hidden_when_no_real_data(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """A device with no probed status/firmware/capabilities (only the static
    transport label) has nothing worth a strip — it must be hidden, not show
    a bare ``USB``."""
    dlg = _make_dialog(qtbot)
    dev = DeviceInfo(
        interface="kvaser",
        vendor="Kvaser",
        model="Leaf Light",
        serial="111",
        transport="USB",
        channels=(ChannelInfo(channel=0, label="ch0"),),
    )
    dlg._add_device(dev)
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)
    assert dlg._current_device().interface == "kvaser"
    assert not dlg._info_strip.isVisible()


def test_info_strip_shows_probed_status_and_capabilities(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """A pcan-style device with real status + capabilities renders them."""
    dlg = _make_dialog(qtbot)
    dev = DeviceInfo(
        interface="pcan",
        vendor="PEAK-System",
        model="PCAN-USB FD",
        transport="USB",
        capabilities=("CAN 2.0", "CAN FD"),
        status="available",
        channels=(ChannelInfo(channel="PCAN_USBBUS1", label="PCAN_USBBUS1"),),
    )
    dlg._add_device(dev)
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)
    text = dlg._info_strip.text()
    assert "●" in text and "available" in text
    assert "USB" in text
    assert "CAN 2.0 / CAN FD" in text


def _baud_choices(dlg: CreateCanBusDialog) -> list[int]:
    return [dlg._baud_combo.itemData(i) for i in range(dlg._baud_combo.count())]


def _select_new_device(dlg: CreateCanBusDialog, device: DeviceInfo) -> None:
    dlg._add_device(device)
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)


_KVASER = DeviceInfo(
    interface="kvaser",
    vendor="Kvaser",
    model="Leaf Light v2",
    serial="12345",
    channels=(ChannelInfo(channel=0, label="ch0"),),
)


def test_kvaser_baud_choices_exclude_800k(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """CANlib has no 800 kbit/s preset, and a Kvaser bus refuses to open on
    anything else; a choice no longer offered falls back to 500 kbit/s."""
    dlg = _make_dialog(qtbot)
    assert 800_000 in _baud_choices(dlg)
    dlg._baud_combo.setCurrentIndex(_baud_choices(dlg).index(800_000))

    _select_new_device(dlg, _KVASER)

    assert _baud_choices(dlg) == [125_000, 250_000, 500_000, 1_000_000]
    assert dlg._baud_combo.currentData() == 500_000


def test_baud_choice_kept_while_still_offered(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    dlg._baud_combo.setCurrentIndex(_baud_choices(dlg).index(1_000_000))

    _select_new_device(dlg, _KVASER)
    assert dlg._baud_combo.currentData() == 1_000_000

    dlg._device_combo.setCurrentIndex(0)  # back to the virtual device
    assert _baud_choices(dlg) == [125_000, 250_000, 500_000, 800_000, 1_000_000]
    assert dlg._baud_combo.currentData() == 1_000_000

    dlg._name_edit.setText("Bench")
    captured: list[BusConfig] = []
    dlg.bus_declared.connect(lambda cfg, _dbcs: captured.append(cfg))
    dlg._on_create()
    assert captured[0].bitrate == 1_000_000


def test_pcan_baud_choices_are_pcan_presets(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """A PCAN bus refuses to open on a bitrate PCAN-Basic has no preset for."""
    dlg = _make_dialog(qtbot)

    _select_new_device(
        dlg,
        DeviceInfo(
            interface="pcan",
            vendor="PEAK-System",
            model="PCAN-USB",
            channels=(ChannelInfo(channel="PCAN_USBBUS1", label="PCAN_USBBUS1"),),
        ),
    )

    assert 800_000 in _baud_choices(dlg)
    assert set(_baud_choices(dlg)) <= PCAN_BITRATES.keys()


def test_in_use_channel_pill_is_disabled(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    dev = DeviceInfo(
        interface="virtual",
        vendor="Vendor",
        model="Multi",
        serial="multi-1",
        channels=(
            ChannelInfo(channel="a", label="ch0", status="in-use"),
            ChannelInfo(channel="b", label="ch1", status="free"),
        ),
    )
    dlg._add_device(dev)
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)
    # The free channel is auto-selected, not the in-use one.
    assert dlg._channel is not None and dlg._channel.channel == "b"


def test_close_cancels_in_flight_probe(qapp, qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Closing the dialog cancels its probe and drops our references, so a
    lingering worker thread can't call back into a destroyed dialog."""
    import threading

    gate = threading.Event()

    def blocking(_backend):  # type: ignore[no-untyped-def]
        gate.wait(2.0)
        return []

    monkeypatch.setattr(prober_mod, "_detect_configs", blocking)
    dlg = CreateCanBusDialog(existing_names=set())
    qtbot.addWidget(dlg)
    prober = dlg._prober
    thread = dlg._prober_thread
    assert prober is not None and thread is not None

    dlg.done(0)  # cancel while the worker is blocked in the first backend
    assert dlg._prober is None
    assert prober._cancelled is True

    gate.set()  # let the worker unblock and the thread tear down cleanly
    # A cancelled probe is only guaranteed to exit within one backend timeout,
    # so budget from that ceiling rather than a bare number: on a loaded runner
    # the pool worker can be scheduled late enough to burn the whole thing.
    qtbot.waitUntil(
        lambda: (prober, thread) not in prober_mod._INFLIGHT_PROBES,
        timeout=int(prober_mod.BACKEND_TIMEOUT_S * 1000) + 5000,
    )


def test_create_and_connect_opens_one_bus(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: Create & connect declares then opens the bus."""
    ctx = make_app_context(tmp_path)
    dlg = _make_dialog(qtbot)
    dlg._name_edit.setText("LiveBus")

    # Mirror the left-dock wiring: declare into the project, then open.
    def _declare_and_connect(cfg: BusConfig, _dbcs: object) -> None:
        ctx.project.workspace.bus = [*ctx.project.workspace.bus, cfg]
        ctx.bus_reader.open(cfg)

    dlg.bus_declared_and_connect.connect(_declare_and_connect)

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        dlg._on_create_and_connect()

    assert "LiveBus" in ctx.bus_reader.bus_names()
    ctx.bus_reader.close()


# ---- Adapter identity ----


def _pcan_device(*channels: tuple[str, str | None]) -> DeviceInfo:
    return DeviceInfo(
        interface="pcan",
        vendor="PEAK-System",
        model="PCAN-USB",
        channels=tuple(ChannelInfo(channel=ch, label=ch, hw_id=hw_id) for ch, hw_id in channels),
    )


def test_create_pins_the_bus_to_the_chosen_adapter(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot)
    dlg._add_device(_pcan_device(("PCAN_USBBUS2", "0000002a.0")))
    dlg._device_combo.setCurrentIndex(dlg._device_combo.count() - 1)
    dlg._name_edit.setText("Bench")
    captured: list[BusConfig] = []
    dlg.bus_declared.connect(lambda cfg, _dbcs: captured.append(cfg))

    dlg._on_create()

    assert captured[0].hw_id == "0000002a.0"
    assert "hw_id" not in captured[0].model_dump()


def test_a_pinned_adapter_is_taken_wherever_it_enumerates(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = _make_dialog(qtbot, existing_buses=[("pcan", "PCAN_USBBUS1", "0000002a.0")])
    moved = ChannelInfo(channel="PCAN_USBBUS2", label="2", hw_id="0000002a.0")
    other = ChannelInfo(channel="PCAN_USBBUS3", label="3", hw_id="0000002b.0")
    unidentified = ChannelInfo(channel="PCAN_USBBUS3", label="3")
    on_its_channel = ChannelInfo(channel="PCAN_USBBUS1", label="1", hw_id="0000002b.0")

    assert dlg._channel_taken("pcan", moved)
    assert not dlg._channel_taken("pcan", other)
    assert not dlg._channel_taken("pcan", unidentified)
    # The .itp holds no pins: two buses never share a configured channel.
    assert dlg._channel_taken("pcan", on_its_channel)


def test_dialog_skips_hung_backends_and_shares_new_ones(qapp, qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Each probe of a hung backend would leave one more hung thread behind."""
    import threading
    from functools import partial

    from ithildin.ui import create_bus_dialog as dialog_mod

    release = threading.Event()
    probed: list[str] = []

    def detect(backend: str) -> list[dict[str, object]]:
        probed.append(backend)
        if backend == "kvaser":
            release.wait(5)
        return []

    monkeypatch.setattr(prober_mod, "_detect_configs", detect)
    monkeypatch.setattr(
        dialog_mod, "AdapterProber", partial(prober_mod.AdapterProber, timeout_s=0.1)
    )
    hung = {"pcan"}
    try:
        dlg = _make_dialog(qtbot, hung_backends=hung)
        qtbot.waitUntil(lambda: dlg._prober is None, timeout=5000)
        assert "pcan" not in probed
        assert hung == {"pcan", "kvaser"}
    finally:
        release.set()
