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
import uuid
from dataclasses import replace
from pathlib import Path

import can
import pytest

from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.database import Database
from ithildin.core.types import BusHealth
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.permissions import AllowlistStore, AllowRule
from ithildin.mcp.tools import (
    McpReadContext,
    McpWriteContext,
    send_frame,
    send_signal,
)
from ithildin.workspace.schema import BusConfig


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
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_1")
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
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_1")
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
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 9000.0, bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "OUT_OF_RANGE"
    assert sent == []


def test_send_signal_unknown_message_denies(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    result = send_signal(ctx, "DoesNotExist", "Whatever", 1.0, bus="CAN_1")
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
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_1")
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
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "BUS_ERROR"
    (line,) = _audit_lines(tmp_path)
    # The allowlist allowed it; the driver refused it.
    assert (line["decision"], line["outcome"], line["reason"]) == (
        "ALLOW",
        "SEND_FAILED",
        "BUS_ERROR",
    )
    assert line["rule_id"] == result["rule_id"]
    assert "simulated bus stall" in line["detail"]


def test_send_frame_bus_error_is_audited_allow_send_failed(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, _sent = _make_ctx(tmp_path)
    rule = ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))

    def boom(_msg: can.Message, _bus: str | None = None) -> None:
        raise can.CanOperationError("transmit buffer full")

    result = send_frame(replace(ctx, send=boom), 0x100, "01", bus="CAN_1")
    assert result == {
        "ok": False,
        "reason": "BUS_ERROR",
        "rule_id": rule.id,
        "detail": "transmit buffer full",
    }
    (line,) = _audit_lines(tmp_path)
    assert (line["decision"], line["outcome"]) == ("ALLOW", "SEND_FAILED")


# ---------- audit: decision and outcome ----------


def test_sent_is_audited_allow_sent(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    rule = ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    assert send_frame(ctx, 0x100, "01", bus="CAN_1") == {
        "ok": True,
        "reason": "OK",
        "rule_id": rule.id,
    }
    assert len(sent) == 1
    (line,) = _audit_lines(tmp_path)
    assert (line["decision"], line["outcome"], line["reason"]) == ("ALLOW", "SENT", "OK")
    assert line["rule_id"] == rule.id


def test_deny_is_audited_not_sent(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, _sent = _make_ctx(tmp_path)
    send_frame(ctx, 0x100, "01", bus="CAN_1")
    (line,) = _audit_lines(tmp_path)
    assert (line["decision"], line["outcome"], line["reason"]) == ("DENY", "NOT_SENT", "NO_RULE")
    assert "rule_id" not in line


def test_dry_run_is_allow_dry_run(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    rule = ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    result = send_frame(replace(ctx, dry_run=True), 0x100, "01", bus="CAN_1")
    assert result == {"ok": True, "reason": "DRY_RUN", "rule_id": rule.id, "dry_run": True}
    assert sent == []
    (line,) = _audit_lines(tmp_path)
    assert (line["decision"], line["outcome"], line["reason"]) == ("ALLOW", "DRY_RUN", "DRY_RUN")
    assert line["dry_run"] is True


def test_encode_failure_is_allow_not_sent(  # type: ignore[no-untyped-def]
    qapp, tmp_path: Path, example_dbc_path: Path, monkeypatch
) -> None:
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))

    def refuse(*_args: object) -> bytes:
        raise ValueError("cannot encode")

    monkeypatch.setattr("ithildin.mcp.tools.encode_tx_message", refuse)
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_1")
    assert result["reason"] == "ENCODE_FAILED"
    assert sent == []
    (line,) = _audit_lines(tmp_path)
    assert (line["decision"], line["outcome"]) == ("ALLOW", "NOT_SENT")
    assert line["detail"] == "cannot encode"


class _Panic(BaseException):
    """A vendor binding's panic: not an ``Exception``."""


def test_base_exception_from_send_is_audited(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, _sent = _make_ctx(tmp_path)
    rule = ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))

    def panic(_msg: can.Message, _bus: str | None = None) -> None:
        raise _Panic("driver panicked")

    with pytest.raises(_Panic):
        send_frame(replace(ctx, send=panic), 0x100, "01", bus="CAN_1")
    (line,) = _audit_lines(tmp_path)
    # Whether the frame left the adapter is unknown.
    assert (line["decision"], line["outcome"], line["reason"]) == (
        "ALLOW",
        "UNKNOWN",
        "INTERNAL_ERROR",
    )
    assert line["rule_id"] == rule.id
    assert line["detail"].startswith("_Panic")


def test_exception_before_decision_is_audited_deny(  # type: ignore[no-untyped-def]
    qapp, tmp_path: Path, monkeypatch
) -> None:
    ctx, sent = _make_ctx(tmp_path)

    def broken(_request: object) -> None:
        raise RuntimeError("allowlist broke")

    monkeypatch.setattr(ctx.read.allowlist, "check_frame", broken)
    with pytest.raises(RuntimeError, match="allowlist broke"):
        send_frame(ctx, 0x100, "01", bus="CAN_1")
    assert sent == []
    (line,) = _audit_lines(tmp_path)
    assert (line["decision"], line["outcome"], line["reason"]) == (
        "DENY",
        "NOT_SENT",
        "INTERNAL_ERROR",
    )
    assert line["detail"] == "RuntimeError: allowlist broke"


# ---------- bus health ----------


def test_unhealthy_bus_denies_before_allowlist(  # type: ignore[no-untyped-def]
    qapp, tmp_path: Path, example_dbc_path: Path, monkeypatch
) -> None:
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*", rate_hz=1.0))
    health = {"CAN_1": (BusHealth.ERRORS, "error passive")}
    monkeypatch.setattr(ctx.read.bus_reader, "health", health.get)

    result = send_frame(ctx, 0x100, "01", bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "BUS_UNHEALTHY"
    assert "bus errors (error passive)" in result["detail"]
    assert send_signal(ctx, "EngineCommand", "EngineSpeed", 800.0, bus="CAN_1")["reason"] == (
        "BUS_UNHEALTHY"
    )
    assert sent == []
    assert [(line["decision"], line["outcome"]) for line in _audit_lines(tmp_path)] == [
        ("DENY", "NOT_SENT")
    ] * 2

    # Allowed at once: the refusals spent none of the 1 Hz rule's tokens.
    health["CAN_1"] = (BusHealth.OK, "")
    assert send_frame(ctx, 0x100, "01", bus="CAN_1")["ok"] is True
    assert len(sent) == 1


def test_lost_bus_is_unhealthy_after_it_closed(qapp, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    monkeypatch.setattr(ctx.read.bus_reader, "health", {"CAN_1": (BusHealth.LOST, "")}.get)
    result = send_frame(ctx, 0x100, "01", bus="CAN_1")
    assert result["reason"] == "BUS_UNHEALTHY"
    assert result["detail"] == "bus 'CAN_1' is unhealthy: adapter lost"
    assert sent == []


_NO_BUS = {
    "ok": False,
    "reason": "NO_BUS",
    "detail": "no bus named, and not exactly one bus is open",
}


def test_unnamed_send_with_no_bus_open_is_refused(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    assert send_frame(ctx, 0x100, "01") == _NO_BUS
    assert sent == []
    (line,) = _audit_lines(tmp_path)
    assert (line["decision"], line["outcome"], line["reason"]) == ("DENY", "NOT_SENT", "NO_BUS")


def test_unnamed_send_with_two_buses_open_is_refused_before_allowlist(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """With two buses open and no ``bus``, the target is unresolved. Refused
    before the allowlist, it spends none of the any-bus rule's tokens, and
    ``send_message`` never gets ``None`` to resolve by itself."""
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*", rate_hz=1.0))
    reader = ctx.read.bus_reader
    for name in ("CAN_1", "CAN_2"):
        cfg = BusConfig(
            name=name,
            interface="virtual",
            channel=f"scoped-{uuid.uuid4().hex[:8]}",
            bitrate=500000,
        )
        with qtbot.waitSignal(reader.bus_started, timeout=2000):
            reader.open(cfg)
    try:
        assert send_frame(ctx, 0x100, "01") == _NO_BUS
        assert sent == []
        assert send_frame(ctx, 0x100, "01", bus="CAN_1")["ok"] is True
    finally:
        reader.close()


# ---------- send_frame ----------


def test_send_frame_no_rule_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    result = send_frame(ctx, message_id=0x100, data_hex="01020304", bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "NO_RULE"
    assert sent == []


def test_send_frame_specific_signal_rule_does_not_authorise(
    qapp, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Safety: a per-signal rule must not let raw send_frame through."""
    ctx, _sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    result = send_frame(ctx, message_id=0x100, data_hex="01020304", bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "NO_RULE"


def test_send_frame_wildcard_rule_allows(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    result = send_frame(ctx, message_id=0x100, data_hex="01 02 03 04", bus="CAN_1")
    assert result["ok"] is True
    assert result["reason"] == "OK"
    assert len(sent) == 1
    assert sent[0].data == b"\x01\x02\x03\x04"


def test_send_frame_bad_hex_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    result = send_frame(ctx, message_id=0x100, data_hex="not hex", bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "BAD_DATA"
    assert sent == []


def test_send_frame_id_out_of_range_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    # A wildcard rule for the bad ID isn't reachable — validation runs first.
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x800, signal="*"))
    result = send_frame(ctx, message_id=0x800, data_hex="00", bus="CAN_1")  # > 11-bit max
    assert result["ok"] is False
    assert result["reason"] == "BAD_ID"
    assert sent == []


def test_send_frame_negative_id_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    result = send_frame(ctx, message_id=-1, data_hex="00", bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "BAD_ID"
    assert sent == []


def test_send_frame_extended_id_allows_larger_range(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x800, signal="*"))
    # 0x800 is out of range for 11-bit but fine as a 29-bit extended id.
    result = send_frame(ctx, message_id=0x800, data_hex="00", is_extended=True, bus="CAN_1")
    assert result["ok"] is True
    assert len(sent) == 1
    assert sent[0].is_extended_id is True


def test_send_frame_oversized_payload_denies(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    result = send_frame(ctx, message_id=0x100, data_hex="00" * 9, bus="CAN_1")  # 9 > 8 bytes
    assert result["ok"] is False
    assert result["reason"] == "BAD_LENGTH"
    assert sent == []


def test_send_signal_non_finite_denies(qapp, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, example_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    result = send_signal(ctx, "EngineCommand", "EngineSpeed", float("nan"), bus="CAN_1")
    assert result["ok"] is False
    assert result["reason"] == "BAD_VALUE"
    assert sent == []


# ---------- listen-only ----------


def test_send_signal_on_listen_only_bus_denies(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Pre-flight rejects sends on a listen-only bus before allowlist check."""
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


# ---------- signals the example DBC lacks ----------


def test_send_signal_beyond_what_the_bits_hold_is_out_of_range(
    qapp, tmp_path: Path, edge_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A [0|0] range means "none declared"; the bits still bound the value."""
    ctx, sent = _make_ctx(tmp_path, edge_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=1601, signal="Temperature"))
    # 16 bits at 0.1 with offset -40 hold -40 … 6513.5.
    result = send_signal(ctx, "ImdStatus", "Temperature", 7000.0, bus="CAN_1")
    assert result["reason"] == "OUT_OF_RANGE"
    assert sent == []


def test_send_signal_sets_the_selector_for_a_multiplexed_signal(
    qapp, tmp_path: Path, edge_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, edge_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=1600, signal="Set_max_voltage"))
    result = send_signal(ctx, "ImdCtrl", "Set_max_voltage", 800, bus="CAN_1")
    assert result["ok"] is True
    assert sent[0].data[0] == 240
    assert int.from_bytes(sent[0].data[1:3], "little") == 800


def test_send_signal_keeps_a_64_bit_int_exact(qapp, tmp_path: Path, edge_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, edge_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=123, signal="LogData"))
    result = send_signal(ctx, "LogBlob", "LogData", 0x0123456789ABCDEF, bus="CAN_1")
    assert result["ok"] is True
    assert bytes(sent[0].data) == bytes.fromhex("efcdab8967452301")
    # One past the top is refused by the range check, not the encoder.
    result = send_signal(ctx, "LogBlob", "LogData", 2**64, bus="CAN_1")
    assert result["reason"] == "OUT_OF_RANGE"


def test_send_signal_keeps_a_whole_number_scaled_64_bit_value_exact(
    qapp, tmp_path: Path, edge_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx, sent = _make_ctx(tmp_path, edge_dbc_path)
    ctx.read.allowlist.add_rule(AllowRule(id=0, message_id=124, signal="Odometer"))
    # Factor 10, offset -1000: raw 0x0123456789ABCDEF.
    result = send_signal(ctx, "WideScaled", "Odometer", 0x0123456789ABCDEF * 10 - 1000, bus="CAN_1")
    assert result["ok"] is True
    assert bytes(sent[0].data) == bytes.fromhex("efcdab8967452301")
    # Just past the top: a float bound would round up and let it reach the encoder.
    result = send_signal(ctx, "WideScaled", "Odometer", 2**64 * 10 - 1000, bus="CAN_1")
    assert result["reason"] == "OUT_OF_RANGE"
