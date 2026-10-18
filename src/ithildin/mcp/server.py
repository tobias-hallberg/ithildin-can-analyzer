# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Build a configured ``FastMCP`` instance for ithildin.

Separated from ``bridge.py`` so the construction is unit-testable
without spinning up the asyncio worker thread or an HTTP listener.

Tool registration is split out from the bridge so:
- Read tools (here, Stage 1.D.β) and write tools (Stage 1.D.γ) can be
  composed independently.
- Tests can call ``mcp.call_tool(name, args)`` synchronously.

v0.1 exposes only the streamable-HTTP transport (run by the bridge under
uvicorn with a bearer-auth gate). ``build_server`` doesn't bind anything
itself.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ithildin.mcp import tools
from ithildin.mcp.tools import McpReadContext, McpWriteContext


def build_server(
    ctx: McpReadContext,
    *,
    write_ctx: McpWriteContext | None = None,
    host: str = "127.0.0.1",
    port: int = 7320,
    name: str = "ithildin",
) -> FastMCP:
    """Construct a FastMCP with tools registered.

    ``ctx`` carries read state. Pass ``write_ctx`` to also register the
    write tools (``send_signal``, ``send_frame``); leave it ``None`` for a
    read-only server.
    """
    mcp = FastMCP(
        name,
        host=host,
        port=port,
        log_level="WARNING",
        instructions=(
            "Ithildin CAN bus analyser. Start with list_buses and "
            "list_messages to discover what's available, then list_signals / "
            "describe_signal for a message's fields. get_recent_messages "
            "returns decoded traffic; get_signal_value returns one signal's "
            "latest value. Write tools (send_signal, send_frame) are gated by "
            "the allowlist — a send with no matching rule is denied, and "
            "get_audit_log explains why."
        ),
    )
    register_read_tools(mcp, ctx)
    if write_ctx is not None:
        register_write_tools(mcp, write_ctx)
    return mcp


def register_read_tools(mcp: FastMCP, ctx: McpReadContext) -> None:
    """Register the Stage 1.D.β read-only tools on ``mcp``."""

    @mcp.tool()
    def list_buses() -> list[dict[str, Any]]:
        """List currently-known CAN buses with their live status."""
        return tools.list_buses(ctx)

    @mcp.tool()
    def list_dbcs() -> list[dict[str, Any]]:
        """List loaded DBC files with their bus binding and message count."""
        return tools.list_dbcs(ctx)

    @mcp.tool()
    def get_recent_messages(
        count: int = 100,
        message_id: int | None = None,
        bus: str | None = None,
        since_timestamp: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return the last ``count`` captured frames, decoded when a DBC is loaded.

        Filter by raw CAN ID (``message_id``), bus name (``bus``), or epoch
        seconds (``since_timestamp``, exclusive) to poll for new traffic.
        """
        return tools.get_recent_messages(
            ctx,
            count=count,
            message_id=message_id,
            bus=bus,
            since_timestamp=since_timestamp,
        )

    @mcp.tool()
    def get_bus_stats() -> dict[str, Any]:
        """Coarse capture stats: open state, bus name, total observed frames."""
        return tools.get_bus_stats(ctx)

    @mcp.tool()
    def list_allowlist() -> list[dict[str, Any]]:
        """Return the current MCP allowlist rules."""
        return tools.list_allowlist(ctx)

    @mcp.tool()
    def list_messages(bus: str | None = None) -> list[dict[str, Any]]:
        """List every message in the loaded DBCs — start here to discover names."""
        return tools.list_messages(ctx, bus=bus)

    @mcp.tool()
    def list_signals(message: str) -> list[dict[str, Any]]:
        """Full signal metadata for one DBC message, by message name."""
        return tools.list_signals(ctx, message)

    @mcp.tool()
    def describe_signal(signal_path: str) -> dict[str, Any] | None:
        """DBC metadata for ``Message.Signal``; ``None`` if unknown."""
        return tools.describe_signal(ctx, signal_path)

    @mcp.tool()
    def get_signal_value(signal_path: str) -> dict[str, Any] | None:
        """Most recently observed value of ``Message.Signal``."""
        return tools.get_signal_value(ctx, signal_path)

    @mcp.tool()
    def get_audit_log(count: int = 50, agent: str | None = None) -> list[dict[str, Any]]:
        """Tail of the send-decision audit log — why a send was allowed or denied."""
        return tools.get_audit_log(ctx, count=count, agent=agent)


def register_write_tools(mcp: FastMCP, ctx: McpWriteContext) -> None:
    """Register the write tools. Every send routes through the allowlist."""

    @mcp.tool()
    def send_signal(
        message: str,
        signal: str,
        value: float,
        agent: str = "",
        bus: str | None = None,
    ) -> dict[str, Any]:
        """Encode + send one signal value (DBC-validated, allowlist-gated).

        ``bus`` is required only when more than one bus is open.
        """
        return tools.send_signal(ctx, message, signal, value, agent=agent, bus=bus)

    @mcp.tool()
    def send_frame(
        message_id: int,
        data_hex: str,
        is_extended: bool = False,
        agent: str = "",
        bus: str | None = None,
    ) -> dict[str, Any]:
        """Send a raw frame by CAN ID + hex payload. Needs a wildcard rule."""
        return tools.send_frame(
            ctx, message_id, data_hex, is_extended=is_extended, agent=agent, bus=bus
        )
