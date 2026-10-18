# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``app_state.bus_reader.BusReader``."""

from __future__ import annotations

import uuid

import can
import pytest

from ithildin.app_state.bus_reader import BusReader, BusReadOnlyError
from ithildin.workspace.schema import BusConfig


def test_open_capture_close(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    channel = f"reader-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=channel,
        bitrate=500000,
    )
    reader = BusReader()

    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)
    assert reader.is_open
    assert reader.bus_name == "CAN_1"

    sender = can.Bus(interface="virtual", channel=channel)
    received_batches: list[list] = []
    reader.frames_received.connect(lambda batch: received_batches.append(batch))
    try:
        for i in range(3):
            sender.send(
                can.Message(
                    arbitration_id=0x100 + i,
                    data=bytes([i]),
                    is_extended_id=False,
                )
            )
    finally:
        sender.shutdown()

    qtbot.waitUntil(lambda: sum(len(b) for b in received_batches) >= 3, timeout=2000)
    reader.close()
    assert not reader.is_open
    assert reader.bus_name is None

    flat = [f for batch in received_batches for f in batch]
    assert {f.arbitration_id for f in flat} == {0x100, 0x101, 0x102}


def test_open_is_additive_across_buses(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """Opening a second bus must not close the first — multi-bus."""
    cfg_a = BusConfig(
        name="A", interface="virtual", channel=f"a-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    cfg_b = BusConfig(
        name="B", interface="virtual", channel=f"b-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    reader = BusReader()

    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_a)
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_b)

    # bus_name is the single-bus convenience and returns None when 2+ are open.
    assert reader.bus_name is None
    assert sorted(reader.bus_names()) == ["A", "B"]
    assert sorted(reader.rings.keys()) == ["A", "B"]

    # Closing one leaves the other running; bus_name returns to the survivor.
    reader.close("A")
    assert reader.bus_names() == ["B"]
    assert reader.bus_name == "B"

    reader.close()  # close all


def test_send_message_requires_target_when_ambiguous(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """``send_message`` raises ValueError when 2+ buses are open and no name given."""
    cfg_a = BusConfig(
        name="A", interface="virtual", channel=f"a-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    cfg_b = BusConfig(
        name="B", interface="virtual", channel=f"b-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    reader = BusReader()
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_a)
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_b)

    msg = can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False)
    with pytest.raises(ValueError, match="bus_name"):
        reader.send_message(msg)

    # Naming a target works.
    reader.send_message(msg, "A")
    reader.close()


def test_send_message_raises_on_listen_only_bus(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """A bus opened with ``listen_only=True`` rejects every transmit."""
    channel = f"ro-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(
        name="RO",
        interface="virtual",
        channel=channel,
        bitrate=500000,
        listen_only=True,
    )
    reader = BusReader()
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)

    assert reader.is_listen_only("RO") is True
    assert reader.is_listen_only("does-not-exist") is False

    msg = can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False)
    with pytest.raises(BusReadOnlyError, match="listen-only"):
        reader.send_message(msg)
    reader.close()


def test_own_send_is_captured_with_is_rx_false(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """receive_own_messages loops the app's own send back into the Trace, tagged TX."""
    channel = f"own-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)
    reader = BusReader()
    captured: list = []
    reader.frames_received.connect(lambda batch: captured.extend(batch))
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)

    reader.send_message(can.Message(arbitration_id=0x321, data=b"\x01\x02", is_extended_id=False))
    qtbot.waitUntil(lambda: any(f.arbitration_id == 0x321 for f in captured), timeout=2000)
    own = next(f for f in captured if f.arbitration_id == 0x321)
    assert own.is_rx is False  # echoed own-send is tagged TX
    reader.close()
