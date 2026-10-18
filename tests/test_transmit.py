# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for the TX engine (``core.transmit``)."""

from __future__ import annotations

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
    encode_tx_message,
    infer_kind,
)
from ithildin.workspace.schema import BusConfig

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
    assert infer_kind(door) == "bool"


def test_infer_kind_enum_segmented_for_small_value_table(
    example_dbc_path: Path,
) -> None:
    import cantools

    db = cantools.database.load_file(str(example_dbc_path))
    # GearState.Gear has 4 enum values per the fixture DBC.
    msg = db.get_message_by_name("GearState")
    gear = next(s for s in msg.signals if s.name == "Gear")
    assert infer_kind(gear) == "enum-seg"


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

    # Patch the reader's send_message to capture the call.
    sent: list[can.Message] = []
    reader.send_message = lambda msg, bus_name=None: sent.append(msg)  # type: ignore[method-assign]
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

    task = MagicMock()
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

    reader.start_periodic = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
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
