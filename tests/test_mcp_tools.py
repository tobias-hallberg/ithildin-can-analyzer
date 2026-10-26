# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``mcp.tools`` read-tool implementations."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import can
from can.interfaces.pcan import basic

from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.database import Database
from ithildin.core.types import BusHealth, FrameRecord
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.permissions import AllowlistStore, AllowRule
from ithildin.mcp.tools import (
    McpReadContext,
    describe_signal,
    get_audit_log,
    get_bus_stats,
    get_recent_messages,
    get_signal_value,
    list_allowlist,
    list_buses,
    list_dbcs,
    list_messages,
    list_signals,
)
from ithildin.workspace.schema import BusConfig
from tests.fake_bus import FakeBus, use_fake_bus


def _make_ctx(tmp_path: Path) -> McpReadContext:
    return McpReadContext(
        bus_reader=BusReader(),
        database=Database(),
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
    )


def test_list_buses_returns_empty_when_disconnected(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    assert list_buses(ctx) == []


def test_list_buses_reports_open_bus(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"t-{uuid.uuid4().hex[:6]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    try:
        out = list_buses(ctx)
        assert len(out) == 1
        assert out[0]["name"] == "CAN_1"
        assert out[0]["status"] == "live"
        assert out[0]["status_detail"] is None
        assert out[0]["interface"] == "virtual"
        assert out[0]["bitrate"] == 500000
        assert out[0]["listen_only"] is False
        assert out[0]["dbcs"] == []
    finally:
        ctx.bus_reader.close()


def test_list_buses_reports_degraded_health(qapp, qtbot, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    ctx = _make_ctx(tmp_path)
    reader = ctx.bus_reader
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(BusConfig(name="CAN_1", interface="pcan", channel="X", bitrate=500000))
    try:
        with qtbot.waitSignal(reader.bus_health_changed, timeout=2000):
            fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
        (bus,) = list_buses(ctx)
        assert (bus["status"], bus["status_detail"]) == ("errors", "error passive")

        with qtbot.waitSignal(reader.bus_health_changed, timeout=2000):
            fake.status_code = basic.PCAN_ERROR_BUSOFF
        (bus,) = list_buses(ctx)
        assert (bus["status"], bus["status_detail"]) == ("bus_off", None)

        # A lost adapter fails the bus at once: it leaves the list, LOST stays.
        with qtbot.waitSignal(reader.bus_failed, timeout=2000):
            fake.status_code = basic.PCAN_ERROR_ILLHW
        assert list_buses(ctx) == []
        assert reader.health("CAN_1") == (BusHealth.LOST, "")
    finally:
        assert reader.shutdown() == []


def test_list_buses_reports_opening(qapp, qtbot, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeBus()
    fake.open_gate = threading.Event()
    use_fake_bus(monkeypatch, fake)
    ctx = _make_ctx(tmp_path)
    ctx.bus_reader.open(BusConfig(name="CAN_1", interface="virtual", channel="t", bitrate=500000))
    try:
        (bus,) = list_buses(ctx)
        assert (bus["status"], bus["status_detail"]) == ("opening", None)
        with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
            fake.open_gate.set()
        assert list_buses(ctx)[0]["status"] == "live"
    finally:
        fake.open_gate.set()
        ctx.bus_reader.close()


def test_list_dbcs_reports_loaded(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    out = list_dbcs(ctx)
    assert len(out) == 1
    entry = out[0]
    assert entry["path"] == str(example_dbc_path)
    assert entry["buses"] == ["CAN_1"]
    # Fixture: EngineCommand + BodyStatus (2 signals each) + GearState (1).
    assert entry["message_count"] == 3
    assert entry["signal_count"] == 5


def test_get_recent_messages_decodes(qapp, qtbot, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"t-{uuid.uuid4().hex[:6]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    try:
        sender = can.Bus(interface="virtual", channel=str(cfg.channel))
        try:
            payload = (6400).to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
            sender.send(can.Message(arbitration_id=0x100, data=payload, is_extended_id=False))
        finally:
            sender.shutdown()
        ring = ctx.bus_reader.ring
        assert ring is not None
        qtbot.waitUntil(lambda: ring.total_seq >= 1, timeout=2000)
        out = get_recent_messages(ctx, count=10)
        assert len(out) == 1
        msg = out[0]
        assert msg["id"] == 0x100
        assert msg["message"] == "EngineCommand"
        assert msg["decoded"]["EngineSpeed"] == 800
    finally:
        ctx.bus_reader.close()


def test_get_recent_messages_id_filter(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Filter pulls only the requested CAN ID."""
    ctx = _make_ctx(tmp_path)
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"t-{uuid.uuid4().hex[:6]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    try:
        sender = can.Bus(interface="virtual", channel=str(cfg.channel))
        try:
            for can_id in (0x100, 0x200, 0x100, 0x300):
                sender.send(can.Message(arbitration_id=can_id, data=b"\x01"))
        finally:
            sender.shutdown()
        ring = ctx.bus_reader.ring
        assert ring is not None
        qtbot.waitUntil(lambda: ring.total_seq >= 4, timeout=2000)

        only_100 = get_recent_messages(ctx, count=100, message_id=0x100)
        assert len(only_100) == 2
        assert all(m["id"] == 0x100 for m in only_100)
    finally:
        ctx.bus_reader.close()


def test_get_bus_stats(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    assert get_bus_stats(ctx) == {"open": False}

    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"t-{uuid.uuid4().hex[:6]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    try:
        stats = get_bus_stats(ctx)
        assert stats["open"] is True
        assert stats["name"] == "CAN_1"
        assert "total_frames" in stats
        assert "ring_capacity" in stats
    finally:
        ctx.bus_reader.close()


def test_list_allowlist_serialises_rules(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.allowlist.add_rule(
        AllowRule(
            id=0,
            message_id=0x301,
            signal="ReqCurrent",
            rate_hz=1.0,
            agent_filter="claude-code",
            bus_name="CAN_1",
        )
    )
    out = list_allowlist(ctx)
    assert len(out) == 1
    rule = out[0]
    assert rule["message_id"] == 0x301
    assert rule["message_id_hex"] == "0x301"
    assert rule["signal"] == "ReqCurrent"
    assert rule["rate_hz"] == 1.0
    assert rule["agent_filter"] == "claude-code"


def test_describe_signal_finds_loaded(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    found = describe_signal(ctx, "EngineCommand.EngineSpeed")
    assert found is not None
    assert found["message"] == "EngineCommand"
    assert found["message_id"] == 0x100
    assert found["unit"] == "rpm"
    assert found["minimum"] == 0.0
    assert found["maximum"] == 8000.0
    assert found["bus"] == "CAN_1"


def test_describe_signal_unknown_returns_none(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    assert describe_signal(ctx, "DoesNotExist.Whatever") is None
    assert describe_signal(ctx, "no-dot") is None


def test_list_messages_enumerates_dbc(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Discovery entry point: an agent that knows no names can still start."""
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    out = list_messages(ctx)
    by_name = {m["name"]: m for m in out}
    assert set(by_name) == {"EngineCommand", "BodyStatus", "GearState"}
    assert by_name["EngineCommand"]["id"] == 0x100
    assert by_name["EngineCommand"]["id_hex"] == "0x100"
    assert by_name["EngineCommand"]["signal_count"] == 2
    assert by_name["EngineCommand"]["bus"] == "CAN_1"


def test_list_messages_bus_filter(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    assert list_messages(ctx, bus="CAN_1") != []
    assert list_messages(ctx, bus="CAN_9") == []


def test_list_signals_returns_metadata(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    out = list_signals(ctx, "EngineCommand")
    by_name = {s["signal"]: s for s in out}
    assert set(by_name) == {"EngineSpeed", "IdleSetpoint"}
    assert by_name["EngineSpeed"]["unit"] == "rpm"
    assert by_name["EngineSpeed"]["maximum"] == 8000.0
    assert by_name["EngineSpeed"]["length_bits"] == 16
    assert by_name["EngineSpeed"]["choices"] is None
    assert list_signals(ctx, "NoSuchMessage") == []


def test_list_signals_surfaces_value_table(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Enum signals carry their VAL_ table so an agent can name the states."""
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    gear = list_signals(ctx, "GearState")[0]
    assert gear["choices"] == {0: "PARK", 1: "REVERSE", 2: "NEUTRAL", 3: "DRIVE"}


def test_get_signal_value_unknown_signal_is_none(  # type: ignore[no-untyped-def]
    qapp, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    assert get_signal_value(ctx, "EngineCommand.NoSuchSignal") is None


def test_get_signal_value_known_but_no_capture(  # type: ignore[no-untyped-def]
    qapp, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Known signal with nothing captured reports a null value, not an error."""
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    out = get_signal_value(ctx, "EngineCommand.EngineSpeed")
    assert out is not None
    assert out["value"] is None
    assert out["unit"] == "rpm"
    assert "timestamp" not in out


def test_get_signal_value_returns_latest(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Two frames for the same ID: the newest value wins."""
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"t-{uuid.uuid4().hex[:6]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    try:
        sender = can.Bus(interface="virtual", channel=str(cfg.channel))
        try:
            for raw in (6400, 8000):  # 800 rpm then 1000 rpm at 0.125 scale
                payload = raw.to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
                sender.send(can.Message(arbitration_id=0x100, data=payload, is_extended_id=False))
        finally:
            sender.shutdown()
        ring = ctx.bus_reader.ring
        assert ring is not None
        qtbot.waitUntil(lambda: ring.total_seq >= 2, timeout=2000)

        out = get_signal_value(ctx, "EngineCommand.EngineSpeed")
        assert out is not None
        assert out["value"] == 1000
        assert out["timestamp"] > 0
    finally:
        ctx.bus_reader.close()


def test_get_recent_messages_since_timestamp(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """``since_timestamp`` is exclusive, so polling with the last seen ts is empty."""
    ctx = _make_ctx(tmp_path)
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"t-{uuid.uuid4().hex[:6]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    try:
        sender = can.Bus(interface="virtual", channel=str(cfg.channel))
        try:
            sender.send(can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False))
        finally:
            sender.shutdown()
        ring = ctx.bus_reader.ring
        assert ring is not None
        qtbot.waitUntil(lambda: ring.total_seq >= 1, timeout=2000)

        first = get_recent_messages(ctx, count=10)
        assert len(first) == 1
        ts = first[0]["timestamp"]
        assert get_recent_messages(ctx, count=10, since_timestamp=ts) == []
        assert len(get_recent_messages(ctx, count=10, since_timestamp=ts - 1.0)) == 1
    finally:
        ctx.bus_reader.close()


def test_get_recent_messages_bus_filter_excludes_other_buses(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"t-{uuid.uuid4().hex[:6]}",
        bitrate=500000,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    try:
        sender = can.Bus(interface="virtual", channel=str(cfg.channel))
        try:
            sender.send(can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False))
        finally:
            sender.shutdown()
        ring = ctx.bus_reader.ring
        assert ring is not None
        qtbot.waitUntil(lambda: ring.total_seq >= 1, timeout=2000)

        assert len(get_recent_messages(ctx, count=10, bus="CAN_1")) == 1
        assert get_recent_messages(ctx, count=10, bus="CAN_2") == []
    finally:
        ctx.bus_reader.close()


def test_get_audit_log_without_path_is_empty(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    assert get_audit_log(_make_ctx(tmp_path)) == []


def test_get_audit_log_tails_and_filters(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Tail returns the newest lines, oldest first, and honours the agent filter."""
    audit_path = tmp_path / "audit.jsonl"
    writer = AuditWriter(audit_path)
    for i in range(10):
        writer.append({"call": "send_signal", "agent": f"a{i % 2}", "decision": "DENY", "seq": i})
    writer.close()

    ctx = McpReadContext(
        bus_reader=BusReader(),
        database=Database(),
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
        audit_path=audit_path,
    )
    out = get_audit_log(ctx, count=3)
    assert [e["seq"] for e in out] == [7, 8, 9]
    assert all("ts" in e for e in out)

    only_a1 = get_audit_log(ctx, count=3, agent="a1")
    assert [e["seq"] for e in only_a1] == [5, 7, 9]


def test_signal_metadata_reports_bit_range_and_multiplexing(
    qapp, tmp_path: Path, edge_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(edge_dbc_path, ["CAN_1"])

    temp = describe_signal(ctx, "ImdStatus.Temperature")
    assert temp is not None
    # Declared [0|0], so the range is what 16 bits at 0.1 / -40 can carry.
    assert temp["minimum"] == -40.0
    assert temp["maximum"] == 6513.5
    assert temp["multiplexer_signal"] == "OpCode"
    assert temp["multiplexer_ids"] == [128]

    by_name = {s["signal"]: s for s in list_signals(ctx, "ImdCtrl")}
    selector = by_name["Request_mux"]
    assert selector["is_multiplexer"] is True
    assert selector["choices"] == {
        98: "Excitation_off",
        193: "Restart",
        240: "Set_max_voltage",
    }
    assert by_name["Set_max_voltage"]["multiplexer_ids"] == [240]


def _engine_frame(raw: int, ts: float, *, is_error_frame: bool = False) -> FrameRecord:
    payload = raw.to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
    return FrameRecord(
        bus_name="CAN_1",
        timestamp=ts,
        arbitration_id=0x100,
        data=payload,
        is_error_frame=is_error_frame,
    )


def _open_virtual(ctx: McpReadContext, qtbot) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"t-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)


def test_get_recent_messages_does_not_decode_error_frames(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual(ctx, qtbot)
    try:
        ring = ctx.bus_reader.rings["CAN_1"]
        ring.append(_engine_frame(6400, 1.0))
        ring.append(_engine_frame(6400, 2.0, is_error_frame=True))

        data, err = get_recent_messages(ctx, count=10)
        assert data["message"] == "EngineCommand"
        assert data["decoded"]["EngineSpeed"] == 800
        assert err["is_error"] is True
        assert err["message"] is None
        assert err["decoded"] is None
    finally:
        ctx.bus_reader.close()


def test_get_signal_value_skips_error_frames(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """A newer error frame with the message's ID doesn't hide the last real value."""
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual(ctx, qtbot)
    try:
        ring = ctx.bus_reader.rings["CAN_1"]
        ring.append(_engine_frame(6400, 1.0))
        ring.append(_engine_frame(8000, 2.0, is_error_frame=True))

        out = get_signal_value(ctx, "EngineCommand.EngineSpeed")
        assert out is not None
        assert out["value"] == 800
        assert out["timestamp"] == 1.0
    finally:
        ctx.bus_reader.close()
