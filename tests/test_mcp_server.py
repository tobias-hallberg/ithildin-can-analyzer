# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``mcp.server.build_server`` — registers expected tools.

Async tool calls are wrapped in ``asyncio.run`` so we don't need
pytest-asyncio. The bridge tests live in ``test_mcp_bridge.py``; this
file verifies tool registration / metadata without spinning up a
network listener.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path

from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.database import Database
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.permissions import AllowlistStore
from ithildin.mcp.server import build_server
from ithildin.mcp.tools import McpReadContext, McpWriteContext


def _run[T](coro: Awaitable[T]) -> T:
    """Run an awaitable to completion in a fresh loop. Avoids pytest-asyncio."""
    return asyncio.run(coro)  # type: ignore[arg-type]


def _make_ctx(tmp_path: Path) -> McpReadContext:
    return McpReadContext(
        bus_reader=BusReader(),
        database=Database(),
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
    )


def test_registers_read_tools(tmp_path: Path) -> None:
    mcp = build_server(_make_ctx(tmp_path))
    tool_names = {t.name for t in _run(mcp.list_tools())}
    assert tool_names == {
        "list_buses",
        "list_dbcs",
        "get_recent_messages",
        "get_bus_stats",
        "list_allowlist",
        "list_messages",
        "list_signals",
        "describe_signal",
        "get_signal_value",
        "get_audit_log",
    }


def test_call_list_buses_returns_empty_when_disconnected(tmp_path: Path) -> None:
    mcp = build_server(_make_ctx(tmp_path))
    result = _run(mcp.call_tool("list_buses", {}))
    # FastMCP returns either a Sequence[ContentBlock] or a dict for
    # structured outputs depending on version; both encode the empty list.
    assert "[]" in repr(result) or "list_buses" in repr(result)


def test_describe_signal_dispatches(tmp_path: Path, example_dbc_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    mcp = build_server(ctx)
    result = _run(mcp.call_tool("describe_signal", {"signal_path": "EngineCommand.EngineSpeed"}))
    text = repr(result)
    assert "EngineCommand" in text
    assert "rpm" in text


def _make_write_ctx(tmp_path: Path) -> McpWriteContext:
    return McpWriteContext(
        read=_make_ctx(tmp_path),
        audit=AuditWriter(tmp_path / "audit.jsonl"),
        send=lambda _msg, _bus=None: None,
    )


def test_write_ctx_registers_write_tools(tmp_path: Path) -> None:
    """The write half must actually reach FastMCP, not just exist in tools.py."""
    mcp = build_server(_make_ctx(tmp_path), write_ctx=_make_write_ctx(tmp_path))
    tool_names = {t.name for t in _run(mcp.list_tools())}
    assert {"send_signal", "send_frame"} <= tool_names


def test_read_only_server_omits_write_tools(tmp_path: Path) -> None:
    """Default-deny at the surface: no write_ctx means the tools aren't exposed."""
    mcp = build_server(_make_ctx(tmp_path))
    tool_names = {t.name for t in _run(mcp.list_tools())}
    assert "send_signal" not in tool_names
    assert "send_frame" not in tool_names


def test_send_signal_dispatches_and_is_denied_without_rule(
    tmp_path: Path, example_dbc_path: Path
) -> None:
    """Through FastMCP, a send with no allowlist rule is denied and audited."""
    read = _make_ctx(tmp_path)
    read.database.add_dbc(example_dbc_path, ["CAN_1"])
    write = McpWriteContext(
        read=read,
        audit=AuditWriter(tmp_path / "audit.jsonl"),
        send=lambda _msg, _bus=None: None,
    )
    mcp = build_server(read, write_ctx=write)
    result = _run(
        mcp.call_tool(
            "send_signal",
            {"message": "EngineCommand", "signal": "EngineSpeed", "value": 800.0},
        )
    )
    assert "NO_RULE" in repr(result)
    assert "NO_RULE" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_send_signal_keeps_json_integers_exact(tmp_path: Path, edge_dbc_path: Path) -> None:
    """FastMCP must not coerce an integer value to float: 64-bit signals need every bit."""
    import can

    from ithildin.mcp.permissions import AllowRule

    read = _make_ctx(tmp_path)
    read.database.add_dbc(edge_dbc_path, ["CAN_1"])
    read.allowlist.add_rule(AllowRule(id=0, message_id=123, signal="LogData"))
    sent: list[can.Message] = []
    write = McpWriteContext(
        read=read,
        audit=AuditWriter(tmp_path / "audit.jsonl"),
        send=lambda msg, _bus=None: sent.append(msg),
    )
    mcp = build_server(read, write_ctx=write)
    _run(
        mcp.call_tool(
            "send_signal",
            {"message": "LogBlob", "signal": "LogData", "value": 0x0123456789ABCDEF},
        )
    )
    assert bytes(sent[0].data) == bytes.fromhex("efcdab8967452301")
