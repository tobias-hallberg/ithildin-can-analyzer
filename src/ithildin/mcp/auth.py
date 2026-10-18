# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Bearer-token auth for the MCP HTTP transport.

The streamable-HTTP transport is otherwise unauthenticated: any process
that can reach the port could drive the bus (subject only to the
allowlist). A bearer token gates the transport so only a client holding
the per-user token can call tools.

The token lives at ``~/.ithildin/<project>/mcp_token`` with ``0o600``
permissions — deliberately **not** in the ``.itp`` project file, which is
meant to stay shareable in git without leaking per-user security grants.

This is a gate, not transport encryption: the HTTP is still plaintext on
loopback. Do not expose the port. See ``SECURITY.md``.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_FILENAME = "mcp_token"

# How much of a refused request's body to read before answering. Plenty for
# any MCP message; a client without the token streaming more than this gets
# no more of our time, and its 401 may then be cut short by a reset.
_DRAIN_LIMIT = 64 * 1024


def load_or_create_token(path: Path) -> str:
    """Return the persisted token, generating one on first use (``0o600``)."""
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0o600 is honored on POSIX; on Windows the mode is largely ignored,
    # but the file still lands in the per-user profile directory.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write(token)
    return token


class BearerAuthMiddleware:
    """ASGI middleware requiring ``Authorization: Bearer <token>`` on HTTP.

    Non-HTTP scopes (``lifespan``, ``websocket``) pass through untouched so
    the transport's own startup/shutdown still runs. The token comparison
    is constant-time.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        presented = headers.get(b"authorization", b"").decode("latin-1")
        if not (presented and hmac.compare_digest(presented, self._expected)):
            await _drain_body(receive)
            await _send_401(send)
            return
        await self._app(scope, receive, send)


async def _drain_body(receive: Receive) -> None:
    """Read a refused request's body before answering it.

    Closing the connection while the client's body is still in flight makes
    the TCP stack answer that body with a reset, and on Windows a reset
    discards the 401 the client hasn't read yet. Python's ``http.client``,
    for one, sends the body as a separate packet after the headers.
    """
    remaining = _DRAIN_LIMIT
    while remaining > 0:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body", False):
            return
        remaining -= len(message.get("body", b""))


async def _send_401(send: Send) -> None:
    body = json.dumps({"error": "unauthorized"}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
