# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``mcp.tools.send_signal`` / ``send_frame``.

Allow/deny matrix + audit-log content. Uses a stub send callable so we
don't need a live bus — the tools' contract is "compute decision +
encode + call send". The actual python-can side is exercised by the
existing BusReader tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import can

from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.database import Database
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.permissions import AllowlistStore, AllowRule
from ithildin.mcp.tools import (
    McpReadContext,
    McpWriteContext,
    send_frame,
    send_signal,
)


def _make_ctx(
    tmp_path: Path, example_dbc_path: Path | None = None
) -> tuple[McpWriteContext, list[can.Message]]:
    sent: list[can.Message] = []

    def stub_send(msg: can.Message, bus_name: str | None = None) -> None:
        sent.append(msg)

    db = Database()
    if example_dbc_path is not None:
        db.add_dbc(example_dbc_path, ["CAN_1"])

    read = McpReadContext(
        bus_reader=BusReader(),
        database=db,
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
    )
    write = McpWriteContext(
        read=read,
        audit=AuditWriter(tmp_path / "audit.jsonl"),
        send=stub_send,
    )
    return write, sent


def _audit_lines(tmp_path: Path) -> list[dict]:
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


# ---------- send_signal ----------


def test_send_signal_no_rule_denies(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0)
    assert result == {"ok": False, "reason": "NO_RULE"}
    assert sent == []
    audit = _audit_lines(tmp_path)
    assert len(audit) == 1
    assert audit[0]["decision"] == "DENY"
    assert audit[0]["reason"] == "NO_RULE"


def test_send_signal_with_matching_rule_succeeds(
    qapp, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0)
    assert result["ok"] is True
    assert result["reason"] == "OK"
    assert len(sent) == 1
    assert sent[0].arbitration_id == 0x100
    # 800 rpm encodes to raw 6400 = 0x1900 in the first 16 LE bits.
    assert sent[0].data[0] == 0x00 and sent[0].data[1] == 0x19
    audit = _audit_lines(tmp_path)
    assert audit[0]["decision"] == "ALLOW"
    assert audit[0]["reason"] == "OK"


def test_send_signal_out_of_range_denies(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(
        AllowRule(
            id=0,
            message_id=0x100,
            signal="EngineSpeed",
            enforce_signal_ranges=True,
        )
    )
    # EngineSpeed maxes at 8000 rpm in the fixture.
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 9000.0)
    assert result["ok"] is False
    assert result["reason"] == "OUT_OF_RANGE"
    assert sent == []


def test_send_signal_unknown_message_denies(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    result = send_signal(ctx, "DoesNotExist", "Whatever", 1.0)
    assert result["ok"] is False
    assert result["reason"] == "UNKNOWN_MESSAGE"
    assert sent == []
    audit = _audit_lines(tmp_path)
    assert audit[0]["reason"] == "UNKNOWN_MESSAGE"


def test_send_signal_dry_run_audits_but_does_not_send(
    qapp, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    # Wrap the existing context with dry_run=True.
    ctx = McpWriteContext(read=ctx.read, audit=ctx.audit, send=ctx.send, dry_run=True)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0)
    assert result["ok"] is True
    assert result["reason"] == "DRY_RUN"
    assert sent == []  # No actual send happened.
    audit = _audit_lines(tmp_path)
    assert audit[0]["dry_run"] is True


def test_send_signal_bus_error_is_audited(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Bus-side failure (e.g. write to a closed bus) yields BUS_ERROR."""
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    read = McpReadContext(
        bus_reader=BusReader(),
        database=db,
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
    )
    read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))

    def boom(_msg: can.Message, _bus: str | None = None) -> None:
        raise RuntimeError("simulated bus stall")

    ctx = McpWriteContext(
        read=read,
        audit=AuditWriter(tmp_path / "audit.jsonl"),
        send=boom,
    )
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0)
    assert result["ok"] is False
    assert result["reason"] == "BUS_ERROR"
    audit = _audit_lines(tmp_path)
    assert audit[0]["reason"] == "BUS_ERROR"
    assert "simulated bus stall" in audit[0]["detail"]


# ---------- send_frame ----------


def test_send_frame_no_rule_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    result = send_frame(ctx, message_id=0x100, data_hex="01020304")
    assert result["ok"] is False
    assert result["reason"] == "NO_RULE"
    assert sent == []


def test_send_frame_specific_signal_rule_does_not_authorise(
    qapp, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Safety: a per-signal rule must not let raw send_frame through."""
    ctx, _sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    result = send_frame(ctx, message_id=0x100, data_hex="01020304")
    assert result["ok"] is False
    assert result["reason"] == "NO_RULE"


def test_send_frame_wildcard_rule_allows(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    result = send_frame(ctx, message_id=0x100, data_hex="01 02 03 04")
    assert result["ok"] is True
    assert result["reason"] == "OK"
    assert len(sent) == 1
    assert sent[0].data == b"\x01\x02\x03\x04"


def test_send_frame_bad_hex_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    result = send_frame(ctx, message_id=0x100, data_hex="not hex")
    assert result["ok"] is False
    assert result["reason"] == "BAD_DATA"
    assert sent == []


def test_send_frame_id_out_of_range_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    # A wildcard rule for the bad ID isn't reachable — validation runs first.
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x800, signal="*"))
    result = send_frame(ctx, message_id=0x800, data_hex="00")  # > 11-bit max
    assert result["ok"] is False
    assert result["reason"] == "BAD_ID"
    assert sent == []


def test_send_frame_negative_id_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    result = send_frame(ctx, message_id=-1, data_hex="00")
    assert result["ok"] is False
    assert result["reason"] == "BAD_ID"
    assert sent == []


def test_send_frame_extended_id_allows_larger_range(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x800, signal="*"))
    # 0x800 is out of range for 11-bit but fine as a 29-bit extended id.
    result = send_frame(ctx, message_id=0x800, data_hex="00", is_extended=True)
    assert result["ok"] is True
    assert len(sent) == 1
    assert sent[0].is_extended_id is True


def test_send_frame_oversized_payload_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    result = send_frame(ctx, message_id=0x100, data_hex="00" * 9)  # 9 > 8 bytes
    assert result["ok"] is False
    assert result["reason"] == "BAD_LENGTH"
    assert sent == []


def test_send_signal_non_finite_denies(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", float("nan"))
    assert result["ok"] is False
    assert result["reason"] == "BAD_VALUE"
    assert sent == []


# ---------- listen-only ----------


def test_send_signal_on_listen_only_bus_denies(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Pre-flight rejects sends on a listen-only bus before allowlist check."""
    import uuid

    from ithildin.workspace.schema import BusConfig

    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    # Add an otherwise-permissive rule so we know the listen-only gate is
    # what denies, not a missing allowlist entry.
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    bus_reader = ctx.read.bus_reader
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=f"ro-mcp-{uuid.uuid4().hex[:8]}",
        bitrate=500000,
        listen_only=True,
    )
    with qtbot.waitSignal(bus_reader.bus_started, timeout=2000):
        bus_reader.open(cfg)

    try:
        result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0)
        assert result["ok"] is False
        assert result["reason"] == "BUS_READ_ONLY"
        assert sent == []
        audit = _audit_lines(tmp_path)
        assert audit[-1]["reason"] == "BUS_READ_ONLY"
        assert audit[-1]["decision"] == "DENY"

        frame_result = send_frame(ctx, message_id=0x100, data_hex="01020304")
        assert frame_result["ok"] is False
        assert frame_result["reason"] == "BUS_READ_ONLY"
    finally:
        bus_reader.close()


def test_send_signal_routes_to_named_bus(tmp_path: Path, example_dbc_path: Path) -> None:
    """``bus=`` reaches the send callable, so multi-bus targeting works."""
    sent: list[tuple[can.Message, str | None]] = []

    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_2"])
    read = McpReadContext(
        bus_reader=BusReader(),
        database=db,
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
    )
    read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    ctx = McpWriteContext(
        read=read,
        audit=AuditWriter(tmp_path / "audit.jsonl"),
        send=lambda msg, bus=None: sent.append((msg, bus)),
    )

    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_2")
    assert result["ok"] is True
    assert len(sent) == 1
    assert sent[0][1] == "CAN_2"
    assert _audit_lines(tmp_path)[0]["args"]["bus"] == "CAN_2"


def test_send_frame_routes_to_named_bus(tmp_path: Path) -> None:
    sent: list[tuple[can.Message, str | None]] = []

    read = McpReadContext(
        bus_reader=BusReader(),
        database=Database(),
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
    )
    read.allowlist.add_rule(AllowRule(id=0, message_id=0x200, signal="*"))
    ctx = McpWriteContext(
        read=read,
        audit=AuditWriter(tmp_path / "audit.jsonl"),
        send=lambda msg, bus=None: sent.append((msg, bus)),
    )

    result = send_frame(ctx, 0x200, "0011223344556677", bus="CAN_B")
    assert result["ok"] is True
    assert sent[0][1] == "CAN_B"


def test_send_signal_named_listen_only_bus_is_refused(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """The listen-only guard follows ``bus=`` rather than only checking bus #1.

    With two buses open, ``BusReader.bus_name`` is ``None``, so the old
    code checked nothing at all.
    """
    import uuid

    from ithildin.workspace.schema import BusConfig

    reader = BusReader()
    for name, listen_only in (("CAN_TX", False), ("CAN_RO", True)):
        cfg = BusConfig(
            name=name,
            interface="virtual",
            channel=f"w-{uuid.uuid4().hex[:8]}",
            bitrate=500000,
            listen_only=listen_only,
        )
        with qtbot.waitSignal(reader.bus_started, timeout=2000):
            reader.open(cfg)
    try:
        db = Database()
        db.add_dbc(example_dbc_path, ["CAN_RO"])
        read = McpReadContext(
            bus_reader=reader,
            database=db,
            allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
        )
        read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
        ctx = McpWriteContext(
            read=read,
            audit=AuditWriter(tmp_path / "audit.jsonl"),
            send=lambda _msg, _bus=None: None,
        )

        result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_RO")
        assert result["ok"] is False
        assert result["reason"] == "BUS_READ_ONLY"
        assert _audit_lines(tmp_path)[0]["decision"] == "DENY"
    finally:
        reader.close()
