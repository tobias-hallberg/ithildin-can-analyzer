# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for the TX engine (``core.transmit``)."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import can
import pytest

from ithildin.app_state.bus_reader import BusReader, BusReadOnlyError
from ithildin.app_state.database import Database
from ithildin.core.transmit import (
    TxMessage,
    TxScheduler,
    TxSetup,
    build_signal_spec,
    encode_tx_message,
    implied_selectors,
    inactive_signals,
    infer_kind,
    multiplexer_choices,
    signal_range,
)
from ithildin.ui.transmit.controls import TxSlider, control_for_signal
from ithildin.workspace.schema import BusConfig
from tests.fake_bus import use_fake_bus

# ---------------------------------------------------------------------------
# Shared encoder
# ---------------------------------------------------------------------------


def test_encode_tx_message_falls_back_to_initial_and_zero(example_dbc_path: Path) -> None:
    """An override for one signal still encodes a full payload."""
    import cantools

    db = cantools.database.load_file(str(example_dbc_path))
    msg = db.get_message_by_name("EngineCommand")
    data = encode_tx_message(db, msg, {"EngineSpeed": 800.0})
    # 800 rpm at scale 0.125 → raw 6400 = 0x1900 in the first 16 LE bits.
    assert data[0] == 0x00
    assert data[1] == 0x19


def test_encode_tx_message_matches_mcp_path(example_dbc_path: Path) -> None:
    """Regression guard: shared encoder agrees with what MCP's send_signal builds."""
    import cantools

    db = cantools.database.load_file(str(example_dbc_path))
    msg = db.get_message_by_name("EngineCommand")
    # The MCP code builds an identical {signal: value, others: initial-or-0} dict
    # before calling encode_message; verify the helper produces the same bytes.
    signal_values: dict[str, float] = {"EngineSpeed": 800.0}
    for sig in msg.signals:
        if sig.name in signal_values:
            continue
        initial = getattr(sig, "initial", None)
        signal_values[sig.name] = float(initial) if initial is not None else 0.0
    expected = db.encode_message(
        msg.frame_id, signal_values, scaling=True, padding=True, strict=False
    )
    assert encode_tx_message(db, msg, {"EngineSpeed": 800.0}) == bytes(expected)


# ---------------------------------------------------------------------------
# Kind heuristic
# ---------------------------------------------------------------------------


def test_infer_kind_bool_for_one_bit_signal(example_dbc_path: Path) -> None:
    import cantools

    db = cantools.database.load_file(str(example_dbc_path))
    # BodyStatus.DoorOpen is 1 bit per the fixture DBC.
    msg = db.get_message_by_name("BodyStatus")
    door = next(s for s in msg.signals if s.name == "DoorOpen")
    assert infer_kind(door, msg) == "bool"


def test_infer_kind_enum_segmented_for_small_value_table(
    example_dbc_path: Path,
) -> None:
    import cantools

    db = cantools.database.load_file(str(example_dbc_path))
    # GearState.Gear has 4 enum values per the fixture DBC.
    msg = db.get_message_by_name("GearState")
    gear = next(s for s in msg.signals if s.name == "Gear")
    assert infer_kind(gear, msg) == "enum-seg"


# ---------------------------------------------------------------------------
# Signal range
# ---------------------------------------------------------------------------

_RANGE_DBC = """\
VERSION ""

NS_ :

BS_:

BU_:

BO_ 1554 Charging: 8 Vector__XXX
 SG_ PowerNoRange : 0|8@1+ (0.1,0) [0|0] "kW" Vector__XXX
 SG_ PowerTooWide : 8|8@1+ (0.1,0) [0|255] "kW" Vector__XXX
 SG_ TorqueNoRange : 16|8@1- (1,0) [0|0] "Nm" Vector__XXX
 SG_ TempOffset : 24|8@1+ (1,-40) [0|0] "degC" Vector__XXX
 SG_ CurrentLimit : 32|16@1+ (0.1,0) [0|500] "A" Vector__XXX
"""


def _range_signal(name: str):  # type: ignore[no-untyped-def]
    import cantools

    db = cantools.database.load_string(_RANGE_DBC)
    msg = db.get_message_by_name("Charging")
    return db, msg, next(s for s in msg.signals if s.name == name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # [0|0] means "no range"; the 8 bits at 0.1 hold 0 … 25.5.
        ("PowerNoRange", (0.0, 25.5)),
        # A declared range the bits can't hold is narrowed to what they can.
        ("PowerTooWide", (0.0, 25.5)),
        ("TorqueNoRange", (-128.0, 127.0)),
        ("TempOffset", (-40.0, 215.0)),
        # A declared range inside what the bits can hold is kept.
        ("CurrentLimit", (0.0, 500.0)),
    ],
)
def test_signal_range_is_limited_to_what_the_bits_can_encode(
    name: str, expected: tuple[float, float]
) -> None:
    _db, _msg, sig = _range_signal(name)
    assert signal_range(sig) == pytest.approx(expected)


def test_infer_kind_uses_bit_range_when_dbc_omits_range() -> None:
    _db, msg, torque = _range_signal("TorqueNoRange")
    assert infer_kind(torque, msg) == "signed"
    _db, msg, power = _range_signal("PowerNoRange")
    assert infer_kind(power, msg) == "num-sm"


def test_slider_maximum_encodes_for_signal_without_dbc_range(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """Regression: an 8-bit signal scaled by 0.1 with range [0|0] offered 0 … 255."""
    db, msg, sig = _range_signal("PowerNoRange")
    spec = build_signal_spec(sig, msg, kind=infer_kind(sig, msg), values={})
    captured: list[float] = []
    w = control_for_signal(spec, on_change=captured.append)
    qtbot.addWidget(w)
    assert isinstance(w, TxSlider)
    w.set_value(1000.0)
    assert captured[-1] == pytest.approx(25.5)
    data = encode_tx_message(db, msg, {"PowerNoRange": captured[-1]})
    assert data[0] == 0xFF


# ---------------------------------------------------------------------------
# Multiplexed messages
# ---------------------------------------------------------------------------


def _edge_message(path: Path, name: str):  # type: ignore[no-untyped-def]
    import cantools

    db = cantools.database.load_file(str(path))
    return db, db.get_message_by_name(name)


def _signal(msg, name: str):  # type: ignore[no-untyped-def]
    return next(s for s in msg.signals if s.name == name)


def test_multiplexer_choices_are_labelled(edge_dbc_path: Path) -> None:
    """Value-table names win; otherwise the label lists the signals an ID carries."""
    _db, ctrl = _edge_message(edge_dbc_path, "ImdCtrl")
    assert multiplexer_choices(ctrl, _signal(ctrl, "Request_mux")) == {
        98: "Excitation_off",
        193: "Restart",
        240: "Set_max_voltage",
    }
    _db, status = _edge_message(edge_dbc_path, "ImdStatus")
    assert multiplexer_choices(status, _signal(status, "OpCode")) == {
        128: "Temperature reading",
        225: "Rp, Rp_uncertainty +2",
    }


def test_selector_gets_a_dropdown_of_valid_ids(edge_dbc_path: Path) -> None:
    _db, ctrl = _edge_message(edge_dbc_path, "ImdCtrl")
    selector = _signal(ctrl, "Request_mux")
    assert infer_kind(selector, ctrl) == "enum-dd"
    spec = build_signal_spec(selector, ctrl, kind="enum-dd", values={})
    assert spec.choices is not None
    assert sorted(spec.choices) == [98, 193, 240]
    # The dropdown shows the ID the encoder will send, not the unencodable 0.
    assert spec.value == 98
    spec = build_signal_spec(selector, ctrl, kind="enum-dd", values={"Set_max_voltage": 800})
    assert spec.value == 240


def test_encode_defaults_selector_to_lowest_valid_id(edge_dbc_path: Path) -> None:
    """Regression: the selector defaulted to 0, which isn't a valid ID, so nothing encoded."""
    db, ctrl = _edge_message(edge_dbc_path, "ImdCtrl")
    assert encode_tx_message(db, ctrl, {})[0] == 98


def test_encode_selector_follows_the_multiplexed_signal_being_set(edge_dbc_path: Path) -> None:
    db, ctrl = _edge_message(edge_dbc_path, "ImdCtrl")
    data = encode_tx_message(db, ctrl, {"Set_max_voltage": 800})
    assert data[0] == 240
    assert int.from_bytes(data[1:3], "little") == 800


def test_encode_explicit_selector_wins(edge_dbc_path: Path) -> None:
    db, ctrl = _edge_message(edge_dbc_path, "ImdCtrl")
    assert encode_tx_message(db, ctrl, {"Request_mux": 193, "Set_max_voltage": 800})[0] == 193


def test_inactive_signals_are_the_unselected_branches(edge_dbc_path: Path) -> None:
    _db, ctrl = _edge_message(edge_dbc_path, "ImdCtrl")
    assert implied_selectors(ctrl, {}) == {"Request_mux": 98}
    assert inactive_signals(ctrl, {}) == {"Set_max_voltage", "Restart"}
    assert inactive_signals(ctrl, {"Request_mux": 193}) == {"Set_max_voltage", "Excitation_off"}


def test_wide_raw_signal_gets_the_hex_control_and_encodes_exactly(edge_dbc_path: Path) -> None:
    """Regression: a 64-bit signal went through a float and lost its low bits."""
    db, blob = _edge_message(edge_dbc_path, "LogBlob")
    sig = _signal(blob, "LogData")
    assert infer_kind(sig, blob) == "hex"
    # Exact ints: as a float, the maximum would round up to 2**64.
    assert signal_range(sig) == (0, 2**64 - 1)
    data = encode_tx_message(db, blob, {"LogData": 0x0123456789ABCDEF})
    assert data == bytes.fromhex("efcdab8967452301")


def test_whole_number_scaled_wide_signal_stays_exact(edge_dbc_path: Path) -> None:
    db, msg = _edge_message(edge_dbc_path, "WideScaled")
    sig = _signal(msg, "Odometer")
    assert infer_kind(sig, msg) == "hex"
    assert signal_range(sig) == (-1000, (2**64 - 1) * 10 - 1000)
    data = encode_tx_message(db, msg, {"Odometer": 0x0123456789ABCDEF * 10 - 1000})
    assert data == bytes.fromhex("efcdab8967452301")


def test_fractional_scaled_wide_signal_keeps_a_numeric_control(edge_dbc_path: Path) -> None:
    """A fractional factor divides in floats in cantools; a raw grid couldn't stay exact."""
    _db, msg = _edge_message(edge_dbc_path, "WideFractional")
    assert infer_kind(_signal(msg, "Energy"), msg) == "num-lg"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _open_virtual_bus(reader: BusReader, qtbot, *, name: str = "CAN_1", listen_only: bool = False):  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name=name,
        interface="virtual",
        channel=f"tx-{uuid.uuid4().hex[:8]}",
        bitrate=500000,
        listen_only=listen_only,
    )
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)


def test_scheduler_one_shot_routes_through_bus_reader(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual_bus(reader, qtbot)
    sched = TxScheduler(reader, db)
    sched.apply(
        TxSetup(
            master_enabled=True,
            transmits=[
                TxMessage(
                    bus_name="CAN_1",
                    message_id=0x100,
                    message_name="EngineCommand",
                    mode="one-shot",
                    active=False,
                    signal_values={"EngineSpeed": 800.0},
                )
            ],
        )
    )

    # Patch the reader's one-shot send to capture the call.
    sent: list[can.Message] = []
    reader.send_message_async = lambda msg, bus_name: sent.append(msg)  # type: ignore[method-assign]
    sched.send_one_shot(0)
    assert len(sent) == 1
    assert sent[0].arbitration_id == 0x100

    sched.shutdown()
    reader.close()


def test_scheduler_one_shot_refused_when_master_off(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual_bus(reader, qtbot)
    sched = TxScheduler(reader, db)
    sched.apply(
        TxSetup(
            master_enabled=False,
            transmits=[
                TxMessage(
                    bus_name="CAN_1",
                    message_id=0x100,
                    message_name="EngineCommand",
                    mode="one-shot",
                )
            ],
        )
    )
    errors: list[tuple[int, str]] = []
    sched.error.connect(lambda mid, msg: errors.append((mid, msg)))
    sched.send_one_shot(0)
    assert errors and "Master is OFF" in errors[-1][1]
    sched.shutdown()
    reader.close()


def test_scheduler_periodic_starts_and_modifies(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Active periodic row → start_periodic; signal change → modify_data."""
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual_bus(reader, qtbot)

    task = MagicMock(period_ms=50, stopped=False)
    reader.start_periodic = MagicMock(return_value=task)  # type: ignore[method-assign]
    reader.stop_periodic = MagicMock()  # type: ignore[method-assign]

    sched = TxScheduler(reader, db)
    tx = TxMessage(
        bus_name="CAN_1",
        message_id=0x100,
        message_name="EngineCommand",
        mode="periodic",
        period_ms=50,
        active=True,
        signal_values={"EngineSpeed": 800.0},
    )
    sched.apply(TxSetup(master_enabled=True, transmits=[tx]))
    assert reader.start_periodic.call_count == 1

    # Signal change → modify_data on the existing task, no new start.
    tx.signal_values["EngineSpeed"] = 1200.0
    sched.apply(sched.setup)
    assert task.modify_data.called
    assert reader.start_periodic.call_count == 1

    sched.shutdown()
    assert reader.stop_periodic.called
    reader.close()


def test_scheduler_master_off_stops_periodic(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual_bus(reader, qtbot)

    reader.start_periodic = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(period_ms=50, stopped=False)
    )
    reader.stop_periodic = MagicMock()  # type: ignore[method-assign]

    sched = TxScheduler(reader, db)
    sched.apply(
        TxSetup(
            master_enabled=True,
            transmits=[
                TxMessage(
                    bus_name="CAN_1",
                    message_id=0x100,
                    message_name="EngineCommand",
                    mode="periodic",
                    period_ms=50,
                    active=True,
                )
            ],
        )
    )
    assert reader.start_periodic.call_count == 1

    sched.set_master(False)
    assert reader.stop_periodic.called

    sched.shutdown()
    reader.close()


def test_scheduler_listen_only_refuses_master_on(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["RO"])
    _open_virtual_bus(reader, qtbot, name="RO", listen_only=True)
    sched = TxScheduler(reader, db)
    errors: list[tuple[int, str]] = []
    sched.error.connect(lambda mid, msg: errors.append((mid, msg)))
    sched.apply(
        TxSetup(
            master_enabled=False,
            transmits=[
                TxMessage(
                    bus_name="RO",
                    message_id=0x100,
                    message_name="EngineCommand",
                    mode="periodic",
                    period_ms=50,
                    active=True,
                )
            ],
        )
    )
    sched.set_master(True)
    assert sched.setup.master_enabled is False
    assert errors and "listen-only" in errors[-1][1]
    sched.shutdown()
    reader.close()


def test_bus_reader_start_periodic_raises_on_listen_only(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """The reader-level guard fires before any cyclic task is created."""
    reader = BusReader()
    _open_virtual_bus(reader, qtbot, name="RO", listen_only=True)
    msg = can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False)
    with pytest.raises(BusReadOnlyError):
        reader.start_periodic(msg, "RO", 0.05)
    reader.close()


# ---------------------------------------------------------------------------
# End-to-end: frames actually reach the wire
#
# These attach a *sibling* ``can.Bus`` on the same virtual channel because
# python-can's virtual backend does not echo a bus's own sends back into
# its own recv() — the sibling proves the frame really left the controller.
# ---------------------------------------------------------------------------


def _open_reader_with_channel(reader: BusReader, qtbot, channel: str) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)


def test_one_shot_emits_real_frame(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    channel = f"e2e-{uuid.uuid4().hex[:8]}"
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    _open_reader_with_channel(reader, qtbot, channel)
    sched = TxScheduler(reader, db)
    sched.apply(
        TxSetup(
            master_enabled=True,
            transmits=[
                TxMessage(
                    bus_name="CAN_1",
                    message_id=0x100,
                    message_name="EngineCommand",
                    mode="one-shot",
                    signal_values={"EngineSpeed": 800.0},
                )
            ],
        )
    )
    receiver = can.Bus(interface="virtual", channel=channel)
    try:
        sched.send_one_shot(0)
        msg = receiver.recv(timeout=1.0)
        assert msg is not None
        assert msg.arbitration_id == 0x100
        # 800 rpm at scale 0.125 → raw 6400 = 0x1900 LE in the first 2 bytes.
        assert msg.data[0] == 0x00
        assert msg.data[1] == 0x19
    finally:
        receiver.shutdown()
        sched.shutdown()
        reader.close()


def test_periodic_emits_repeated_frames_and_stops(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    channel = f"e2e-{uuid.uuid4().hex[:8]}"
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    _open_reader_with_channel(reader, qtbot, channel)
    sched = TxScheduler(reader, db)
    receiver = can.Bus(interface="virtual", channel=channel)
    try:
        sched.apply(
            TxSetup(
                master_enabled=True,
                transmits=[
                    TxMessage(
                        bus_name="CAN_1",
                        message_id=0x100,
                        message_name="EngineCommand",
                        mode="periodic",
                        period_ms=20,
                        active=True,
                        signal_values={"EngineSpeed": 800.0},
                    )
                ],
            )
        )
        # Collect over a fixed wall-clock window. The task is still
        # running, so a "drain until recv() returns None" loop would never
        # terminate (frames arrive every 20ms, faster than any recv timeout).
        seen = 0
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            msg = receiver.recv(timeout=0.05)
            if msg is not None and msg.arbitration_id == 0x100:
                seen += 1
        assert seen >= 3  # ~200ms / 20ms, minus scheduling slack

        # Master OFF tears the task down — no further frames.
        sched.set_master(False)
        time.sleep(0.06)
        # Drain residual in-flight frames; now the task is stopped so the
        # queue empties and recv() returns None.
        while receiver.recv(timeout=0.05) is not None:
            pass
        # Confirm the stream stays dry.
        assert receiver.recv(timeout=0.1) is None
    finally:
        receiver.shutdown()
        sched.shutdown()
        reader.close()


def test_signal_change_modifies_live_payload(qapp, qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Editing a signal value while a periodic TX runs changes the bytes on the wire."""
    channel = f"e2e-{uuid.uuid4().hex[:8]}"
    reader = BusReader()
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    _open_reader_with_channel(reader, qtbot, channel)
    sched = TxScheduler(reader, db)
    tx = TxMessage(
        bus_name="CAN_1",
        message_id=0x100,
        message_name="EngineCommand",
        mode="periodic",
        period_ms=20,
        active=True,
        signal_values={"EngineSpeed": 800.0},
    )
    receiver = can.Bus(interface="virtual", channel=channel)
    try:
        sched.apply(TxSetup(master_enabled=True, transmits=[tx]))
        qtbot.wait(60)
        # Change the value and re-apply; modify_data should push new bytes.
        tx.signal_values["EngineSpeed"] = 1000.0  # raw 8000 = 0x1F40 LE
        sched.apply(sched.setup)
        # Collect over a fixed window (task still running) and keep the
        # most recent frame — it must carry the modified value.
        last = None
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            msg = receiver.recv(timeout=0.05)
            if msg is not None and msg.arbitration_id == 0x100:
                last = msg
        assert last is not None
        assert last.data[0] == 0x40
        assert last.data[1] == 0x1F
    finally:
        receiver.shutdown()
        sched.shutdown()
        reader.close()


# ---------------------------------------------------------------------------
# Disarm: anything that stops TX behind the user's back turns master OFF,
# and nothing restarts on its own.
# ---------------------------------------------------------------------------


@pytest.fixture
def tx_reader():  # type: ignore[no-untyped-def]
    reader = BusReader()
    yield reader
    reader.close()


def _db(dbc_path: Path, buses: list[str] | None = None) -> Database:
    db = Database()
    db.add_dbc(dbc_path, buses or ["CAN_1"])
    return db


def _periodic(bus: str = "CAN_1", *, period_ms: int = 20, active: bool = True) -> TxMessage:
    return TxMessage(
        bus_name=bus,
        message_id=0x100,
        message_name="EngineCommand",
        mode="periodic",
        period_ms=period_ms,
        active=active,
        signal_values={"EngineSpeed": 800.0},
    )


def _one_shot() -> TxMessage:
    return TxMessage(
        bus_name="CAN_1",
        message_id=0x100,
        message_name="EngineCommand",
        signal_values={"EngineSpeed": 800.0},
    )


def _open_fake(reader: BusReader, qtbot, interface: str = "pcan") -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(name="CAN_1", interface=interface, channel="X", bitrate=500000)  # type: ignore[arg-type]
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)


def test_disconnect_disarms_and_reconnect_does_not_resume(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    channel = f"e2e-{uuid.uuid4().hex[:8]}"
    _open_reader_with_channel(tx_reader, qtbot, channel)
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    tx = _periodic()
    receiver = can.Bus(interface="virtual", channel=channel)
    try:
        sched.apply(TxSetup(master_enabled=True, transmits=[tx]))
        assert receiver.recv(timeout=1.0) is not None

        with qtbot.waitSignal(sched.disarmed, timeout=2000) as blocker:
            tx_reader.close("CAN_1")
        assert blocker.args == ["Bus CAN_1 closed"]
        assert sched.setup.master_enabled is False
        while receiver.recv(timeout=0.05) is not None:
            pass

        _open_reader_with_channel(tx_reader, qtbot, channel)
        tx.signal_values["EngineSpeed"] = 1000.0
        tx.period_ms = 10
        sched.apply(sched.setup)
        assert receiver.recv(timeout=0.3) is None
        assert sched._tasks == {}
    finally:
        receiver.shutdown()
        sched.shutdown()


def test_closing_a_bus_without_active_rows_keeps_master_on(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    _open_virtual_bus(tx_reader, qtbot)
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic(active=False)]))
    disarms: list[str] = []
    sched.disarmed.connect(disarms.append)

    with qtbot.waitSignal(tx_reader.bus_stopped, timeout=2000):
        tx_reader.close("CAN_1")
    assert sched.setup.master_enabled is True
    assert disarms == []


def test_bus_failure_disarms_with_reason(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    fake = use_fake_bus(monkeypatch)
    # Kvaser: a receive error without a hardware code is fatal there.
    _open_fake(tx_reader, qtbot, "kvaser")
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic()]))
    qtbot.waitUntil(lambda: len(fake.sent) >= 1, timeout=2000)

    with qtbot.waitSignal(sched.disarmed, timeout=2000) as blocker:
        fake.recv_exc = can.CanOperationError("adapter gone")
    assert blocker.args[0].startswith("Bus CAN_1 lost (")
    assert "adapter gone" in blocker.args[0]
    assert sched.setup.master_enabled is False
    assert sched._tasks == {}


def test_cyclic_failure_disarms_and_flushes(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    fake = use_fake_bus(monkeypatch)
    _open_fake(tx_reader, qtbot, "pcan")
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic(period_ms=10)]))
    qtbot.waitUntil(lambda: len(fake.sent) >= 1, timeout=2000)

    with qtbot.waitSignal(sched.disarmed, timeout=2000) as blocker:
        fake.send_exc = can.CanOperationError("transmit queue full")
    assert blocker.args == ["Periodic 0x100 on CAN_1 failed (transmit queue full)"]
    assert sched.setup.master_enabled is False
    qtbot.waitUntil(lambda: fake.resets >= 1, timeout=2000)

    fake.send_exc = None
    sent = len(fake.sent)
    qtbot.wait(100)
    assert len(fake.sent) == sent


def test_period_change_restarts_task(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    """Regression: python-can can't retime a task, so a period edit had no effect."""
    _open_virtual_bus(tx_reader, qtbot)
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    tx = _periodic(period_ms=100)
    sched.apply(TxSetup(master_enabled=True, transmits=[tx]))
    old = sched._tasks[("CAN_1", 0x100)]

    tx.period_ms = 50
    sched.apply(sched.setup)
    new = sched._tasks[("CAN_1", 0x100)]
    assert new is not old
    assert old.stopped
    assert new.period_ms == 50
    assert not new.stopped
    sched.shutdown()


def test_master_off_flushes_targeted_buses(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    """Queued one-shots are discarded too, not only periodic rows' frames."""
    fake = use_fake_bus(monkeypatch)
    _open_fake(tx_reader, qtbot, "kvaser")
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(transmits=[_one_shot()]))
    sched.set_master(True)
    assert sched.setup.master_enabled is True

    sched.set_master(False)
    qtbot.waitUntil(lambda: fake.flushes == 1, timeout=2000)


def test_shutdown_turns_master_off_and_flushes(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    """The closing TX window's scheduler goes inert: a later bus stop is no disarm."""
    fake = use_fake_bus(monkeypatch)
    _open_fake(tx_reader, qtbot, "kvaser")
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic()]))
    task = sched._tasks[("CAN_1", 0x100)]
    disarms: list[str] = []
    sched.disarmed.connect(disarms.append)

    sched.shutdown()
    assert task.stopped
    assert sched.setup.master_enabled is False
    qtbot.waitUntil(lambda: fake.flushes == 1, timeout=2000)
    with qtbot.waitSignal(tx_reader.bus_stopped, timeout=2000):
        tx_reader.close("CAN_1")
    assert disarms == []


def test_arming_refused_when_target_bus_closed(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(transmits=[_periodic()]))
    errors: list[str] = []
    sched.error.connect(lambda _mid, msg: errors.append(msg))

    sched.set_master(True)
    assert sched.setup.master_enabled is False
    assert errors == ["bus 'CAN_1' is not connected — connect it first"]


def test_start_failure_disarms(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    _open_virtual_bus(tx_reader, qtbot)
    sched = TxScheduler(tx_reader, _db(example_dbc_path, ["CAN_1", "CAN_2"]))
    sched.apply(TxSetup(transmits=[_periodic(), _periodic("CAN_2", active=False)]))
    sched.set_master(True)
    assert sched.setup.master_enabled is True

    with qtbot.waitSignal(sched.disarmed, timeout=1000) as blocker:
        sched.toggle_active(1)
    assert blocker.args[0].startswith("Periodic 0x100 on CAN_2 didn't start")
    assert sched.setup.master_enabled is False
    assert sched._tasks == {}


def test_one_shot_sends_off_gui_thread(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    fake = use_fake_bus(monkeypatch)
    _open_fake(tx_reader, qtbot)
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_one_shot()]))

    sched.send_one_shot(0)
    qtbot.waitUntil(lambda: len(fake.sent) == 1, timeout=2000)
    msg, thread, _held = fake.sent[0]
    assert msg.arbitration_id == 0x100
    assert thread is not threading.main_thread()


def test_one_shot_failure_reports_error(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    fake = use_fake_bus(monkeypatch)
    fake.send_exc = can.CanOperationError("no ACK")
    _open_fake(tx_reader, qtbot)
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_one_shot()]))

    with qtbot.waitSignal(sched.error, timeout=2000) as blocker:
        sched.send_one_shot(0)
    assert blocker.args == [0x100, "send failed: no ACK"]
    assert sched.setup.master_enabled is True


def test_same_id_on_two_buses_gets_two_tasks(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    _open_virtual_bus(tx_reader, qtbot, name="CAN_1")
    _open_virtual_bus(tx_reader, qtbot, name="CAN_2")
    sched = TxScheduler(tx_reader, _db(example_dbc_path, ["CAN_1", "CAN_2"]))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic("CAN_1"), _periodic("CAN_2")]))

    assert set(sched._tasks) == {("CAN_1", 0x100), ("CAN_2", 0x100)}
    assert not any(task.stopped for task in sched._tasks.values())
    sched.shutdown()


# ---------------------------------------------------------------------------
# Disarm on bus health: only a worsening into bus errors or bus-off.
# ---------------------------------------------------------------------------


def test_health_worsening_disarms(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    _open_virtual_bus(tx_reader, qtbot)
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic()]))

    with qtbot.waitSignal(sched.disarmed, timeout=1000) as blocker:
        tx_reader.bus_health_changed.emit("CAN_1", "errors", "error passive")
    assert blocker.args == ["Bus CAN_1: bus errors (error passive)"]
    assert sched.setup.master_enabled is False
    assert sched._tasks == {}


def test_rearming_while_degraded_holds_until_it_gets_worse(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    _open_virtual_bus(tx_reader, qtbot)
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic()]))
    disarms: list[str] = []
    sched.disarmed.connect(disarms.append)
    tx_reader.bus_health_changed.emit("CAN_1", "errors", "error passive")
    assert len(disarms) == 1

    sched.set_master(True)
    # Same state, other detail: not a worsening.
    tx_reader.bus_health_changed.emit("CAN_1", "errors", "error-frame flood")
    assert sched.setup.master_enabled is True
    tx_reader.bus_health_changed.emit("CAN_1", "bus_off", "")
    assert disarms[1:] == ["Bus CAN_1: bus-off"]

    # Recovered, then worse again.
    sched.set_master(True)
    tx_reader.bus_health_changed.emit("CAN_1", "ok", "")
    assert sched.setup.master_enabled is True
    tx_reader.bus_health_changed.emit("CAN_1", "errors", "no ACK")
    assert disarms[2:] == ["Bus CAN_1: bus errors (no ACK)"]


def test_health_of_an_untargeted_bus_keeps_master_on(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    _open_virtual_bus(tx_reader, qtbot, name="CAN_1")
    _open_virtual_bus(tx_reader, qtbot, name="CAN_2")
    sched = TxScheduler(tx_reader, _db(example_dbc_path, ["CAN_1", "CAN_2"]))
    sched.apply(
        TxSetup(
            master_enabled=True, transmits=[_periodic("CAN_1"), _periodic("CAN_2", active=False)]
        )
    )
    disarms: list[str] = []
    sched.disarmed.connect(disarms.append)

    tx_reader.bus_health_changed.emit("CAN_2", "bus_off", "")
    # LOST is left to the bus failure that follows it.
    tx_reader.bus_health_changed.emit("CAN_1", "lost", "")
    assert disarms == []
    assert sched.setup.master_enabled is True
    sched.shutdown()


def test_scheduler_created_on_a_degraded_bus_sees_no_worsening(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    from ithildin.core.types import BusHealth

    _open_virtual_bus(tx_reader, qtbot)
    monkeypatch.setitem(tx_reader._health, "CAN_1", (BusHealth.ERRORS, "error passive"))
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic()]))

    tx_reader.bus_health_changed.emit("CAN_1", "errors", "no ACK")
    assert sched.setup.master_enabled is True
    sched.shutdown()


def test_arming_refused_while_bus_is_opening(  # type: ignore[no-untyped-def]
    qapp, qtbot, example_dbc_path: Path, tx_reader
) -> None:
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(transmits=[_periodic()]))
    errors: list[str] = []
    sched.error.connect(lambda _mid, msg: errors.append(msg))
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"tx-{uuid.uuid4().hex[:8]}", bitrate=500000
    )

    with qtbot.waitSignal(tx_reader.bus_started, timeout=2000):
        tx_reader.open(cfg)
        sched.set_master(True)
    assert sched.setup.master_enabled is False
    assert errors == ["bus 'CAN_1' is still connecting"]


def test_bus_errors_disarm_end_to_end(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, example_dbc_path: Path, tx_reader
) -> None:
    from can.interfaces.pcan import basic

    from ithildin.core import bus as bus_module

    monkeypatch.setattr(bus_module, "_HEALTH_TICK_S", 0.1)
    fake = use_fake_bus(monkeypatch)
    _open_fake(tx_reader, qtbot, "pcan")
    sched = TxScheduler(tx_reader, _db(example_dbc_path))
    sched.apply(TxSetup(master_enabled=True, transmits=[_periodic()]))
    qtbot.waitUntil(lambda: len(fake.sent) >= 1, timeout=2000)

    with qtbot.waitSignal(sched.disarmed, timeout=2000) as blocker:
        fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
    assert blocker.args == ["Bus CAN_1: bus errors (error passive)"]
    assert sched._tasks == {}
    qtbot.waitUntil(lambda: fake.resets >= 1, timeout=2000)
