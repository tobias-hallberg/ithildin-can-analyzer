# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``mcp.bridge.McpServerBridge``.

The bridge is the Qt↔asyncio integration point: a worker thread runs
its own asyncio loop hosting FastMCP's streamable-http transport,
talking back to the GUI thread via Qt signals. These tests verify the
lifecycle (start → started → stop), idempotence (double-start is a
no-op, stop without start is safe), and the running-state predicate.

``test_real_client_lists_and_calls_tools`` drives an actual MCP client
over the wire; the rest exercise the thread/signal plumbing in
isolation. Tool-level behaviour is covered in ``test_mcp_server.py``
and ``test_mcp_tools.py``.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
from pathlib import Path

import can
import pytest

from ithildin.app_state.database import Database
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.bridge import McpServerBridge
from ithildin.mcp.permissions import AllowlistStore, AllowRule
from ithildin.mcp.tools import McpReadContext, McpWriteContext
from tests.fake_bus import LiveReader


def _free_port() -> int:
    """Bind to port 0, read the port the OS picked, release the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_read_ctx(tmp_path: Path, *live_buses: str) -> McpReadContext:
    return McpReadContext(
        bus_reader=LiveReader(*live_buses),
        database=Database(),
        allowlist=AllowlistStore(tmp_path / "allowlist.toml"),
    )


@pytest.fixture
def bridge(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Build a bridge and guarantee it's stopped at teardown."""
    b = McpServerBridge(_make_read_ctx(tmp_path))
    yield b
    # Best-effort cleanup; if start() never ran this is a no-op.
    if b.is_running:
        b.stop(wait_s=3.0)


def test_is_running_false_before_start(bridge: McpServerBridge) -> None:
    assert bridge.is_running is False


def test_start_emits_started_with_http_url(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    port = _free_port()
    with qtbot.waitSignal(bridge.started, timeout=5000) as blocker:
        bridge.start(host="127.0.0.1", port=port)
    url = blocker.args[0]
    assert isinstance(url, str)
    assert url.startswith(f"http://127.0.0.1:{port}")
    assert bridge.is_running is True


def test_stop_after_start_emits_stopped_and_clears_running(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    port = _free_port()
    with qtbot.waitSignal(bridge.started, timeout=5000):
        bridge.start(host="127.0.0.1", port=port)
    with qtbot.waitSignal(bridge.stopped, timeout=5000):
        joined = bridge.stop(wait_s=3.0)
    assert joined is True
    assert bridge.is_running is False


def test_double_start_is_a_noop(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    port = _free_port()
    with qtbot.waitSignal(bridge.started, timeout=5000):
        bridge.start(host="127.0.0.1", port=port)

    # Second start must not crash, must not spin up another thread, and
    # must not retry the bind (which would fail with EADDRINUSE).
    bridge.start(host="127.0.0.1", port=port)
    assert bridge.is_running is True


def test_starts_without_stdio(qtbot, bridge: McpServerBridge, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The windowed frozen build has no stdout/stderr. uvicorn's default log
    config calls ``sys.stdout.isatty()``, so the server failed to start."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    errors: list[str] = []
    bridge.error.connect(errors.append)

    with qtbot.waitSignal(bridge.started, timeout=5000):
        bridge.start(host="127.0.0.1", port=_free_port())

    assert errors == []
    # No dictConfig ran: uvicorn records propagate to the app's root handlers.
    assert logging.getLogger("uvicorn").handlers == []


def test_stop_when_never_started_is_safe(bridge: McpServerBridge) -> None:
    # Stop without start: returns True (nothing to wait on); does not
    # raise; leaves is_running False.
    assert bridge.stop(wait_s=0.5) is True
    assert bridge.is_running is False


def _http_status(port: int, token: str | None, timeout: float = 30.0) -> int:
    """POST to the MCP endpoint and return the HTTP status.

    Network errors propagate, so a failing assertion names the real error
    rather than reading as a status of 0.
    """
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp", data=b"{}", headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _serving(port: int, token: str | None) -> bool:
    """Readiness probe: has the transport started answering HTTP?"""
    try:
        _http_status(port, token, timeout=1.0)
    except OSError:
        # Not serving yet. Linux refuses the connection outright (URLError);
        # Windows accepts it into the backlog and then aborts mid-response
        # (WinError 10053), which surfaces as a bare OSError. URLError is an
        # OSError subclass, so this covers both.
        return False
    return True


def test_live_http_requires_bearer_token(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: the running transport rejects a tokenless request with 401
    and lets a correctly-tokened one through to the app (any non-401)."""
    port = _free_port()
    with qtbot.waitSignal(bridge.started, timeout=5000):
        bridge.start(host="127.0.0.1", port=port)
    qtbot.waitUntil(lambda: _serving(port, bridge.token), timeout=20000)

    assert _http_status(port, None) == 401
    assert _http_status(port, "wrong") == 401
    # Correct token clears the gate; the MCP app then handles it (not 401).
    assert _http_status(port, bridge.token) != 401


def test_can_restart_after_stop(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    """One bridge instance survives a start → stop → start cycle.

    The dialog's port-conflict retry path depends on this — it stops
    the bridge and starts again on the next port up.
    """
    port_a = _free_port()
    with qtbot.waitSignal(bridge.started, timeout=5000):
        bridge.start(host="127.0.0.1", port=port_a)
    with qtbot.waitSignal(bridge.stopped, timeout=5000):
        bridge.stop(wait_s=3.0)

    port_b = _free_port()
    with qtbot.waitSignal(bridge.started, timeout=5000) as blocker:
        bridge.start(host="127.0.0.1", port=port_b)
    assert blocker.args[0].startswith(f"http://127.0.0.1:{port_b}")


def test_bind_conflict_emits_error(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    """A port already in use must surface as ``error``, not a silent death.

    uvicorn raises ``SystemExit`` from inside the loop when it can't bind.
    That's a BaseException, so it used to slip past the worker thread's
    ``except Exception`` and the dialog showed "running" then "stopped"
    with no explanation.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker_sock:
        blocker_sock.bind(("127.0.0.1", 0))
        blocker_sock.listen(1)
        port = int(blocker_sock.getsockname()[1])

        with qtbot.waitSignal(bridge.error, timeout=10000) as blocker:
            bridge.start(host="127.0.0.1", port=port)
        assert str(port) in blocker.args[0]

    qtbot.waitUntil(lambda: bridge.is_running is False, timeout=5000)


def test_bind_conflict_does_not_emit_started(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    """``started`` means listening — a failed bind must never announce it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker_sock:
        blocker_sock.bind(("127.0.0.1", 0))
        blocker_sock.listen(1)
        port = int(blocker_sock.getsockname()[1])

        seen: list[str] = []
        bridge.started.connect(seen.append)
        with qtbot.waitSignal(bridge.stopped, timeout=10000):
            bridge.start(host="127.0.0.1", port=port)
        assert seen == []


def test_real_client_lists_and_calls_tools(qtbot, bridge: McpServerBridge) -> None:  # type: ignore[no-untyped-def]
    """Drive the wire with a real MCP client: initialize → tools/list → tools/call.

    Everything else here tests the plumbing in isolation; this proves the
    whole stack (uvicorn + bearer gate + streamable-http + FastMCP tool
    dispatch) actually speaks the protocol.
    """
    import asyncio

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    port = _free_port()
    with qtbot.waitSignal(bridge.started, timeout=5000) as started:
        bridge.start(host="127.0.0.1", port=port)
    url = started.args[0]
    headers = {"Authorization": f"Bearer {bridge.token}"}

    async def drive() -> tuple[set[str], str]:
        # The bearer token rides on the httpx client the transport uses.
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    called = await session.call_tool("get_bus_stats", {})
                    return {t.name for t in listed.tools}, repr(called)

    names, called = asyncio.run(drive())
    assert "get_bus_stats" in names
    assert "list_messages" in names
    # No bus open in this fixture, so the tool reports closed.
    assert "false" in called.lower() or "False" in called


def test_stop_during_stalled_send_returns(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A send stuck in the driver must not keep the server from stopping; it
    still writes its audit line once the driver returns."""
    import asyncio
    import contextlib
    import json

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    entered = threading.Event()
    release = threading.Event()

    def stalled(_msg: can.Message, _bus: str | None = None) -> None:
        entered.set()
        release.wait(10)

    read = _make_read_ctx(tmp_path, "CAN_1")
    read.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    audit_path = tmp_path / "audit.jsonl"
    write = McpWriteContext(read=read, audit=AuditWriter(audit_path), send=stalled)
    bridge = McpServerBridge(read, write_ctx=write)
    with qtbot.waitSignal(bridge.started, timeout=5000) as started:
        bridge.start(host="127.0.0.1", port=_free_port())
    url = started.args[0]
    headers = {"Authorization": f"Bearer {bridge.token}"}

    server_gone = threading.Event()

    async def drive() -> None:
        async with (
            httpx.AsyncClient(headers=headers) as http_client,
            streamable_http_client(url, http_client=http_client, terminate_on_close=False) as (
                r,
                w,
                _,
            ),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            call = asyncio.create_task(
                session.call_tool(
                    "send_frame", {"message_id": 0x100, "data_hex": "01", "bus": "CAN_1"}
                )
            )
            await asyncio.to_thread(server_gone.wait, 10)
            call.cancel()
            # The server went away under this call on purpose; how the
            # client reports that doesn't matter here.
            with contextlib.suppress(BaseException):
                await call

    def run_client() -> None:
        with contextlib.suppress(BaseException):
            asyncio.run(asyncio.wait_for(drive(), 15))

    client = threading.Thread(target=run_client, name="mcp-test-client", daemon=True)
    client.start()
    try:
        assert entered.wait(10)
        with qtbot.waitSignal(bridge.stopped, timeout=5000):
            assert bridge.stop(wait_s=6.0) is True
    finally:
        server_gone.set()
        release.set()
        client.join(15)
        if bridge.is_running:
            bridge.stop(wait_s=5.0)
    assert not client.is_alive()

    def lines() -> list[dict[str, object]]:
        if not audit_path.exists():
            return []
        return [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines()]

    qtbot.waitUntil(lambda: len(lines()) == 1, timeout=5000)
    (line,) = lines()
    assert (line["decision"], line["outcome"]) == ("ALLOW", "SENT")
