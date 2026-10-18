# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``mcp.auth`` — the bearer-token gate on the HTTP transport."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ithildin.mcp.auth import BearerAuthMiddleware, load_or_create_token


def test_token_created_once_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    first = load_or_create_token(path)
    assert first  # non-empty
    assert path.read_text(encoding="utf-8").strip() == first
    # A second call returns the same token — it is not regenerated.
    assert load_or_create_token(path) == first


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_token_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    load_or_create_token(path)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600


class _Recorder:
    """Collects the ASGI messages an app sends, for assertions."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int | None:
        for m in self.sent:
            if m["type"] == "http.response.start":
                return int(m["status"])
        return None


async def _noop_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _http_scope(auth: str | None) -> dict:
    headers = [(b"authorization", auth.encode("latin-1"))] if auth is not None else []
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}


def test_middleware_rejects_missing_token() -> None:
    import asyncio

    called = False

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True

    rec = _Recorder()
    mw = BearerAuthMiddleware(inner, token="s3cret")
    asyncio.run(mw(_http_scope(None), _noop_receive, rec.send))
    assert rec.status == 401
    assert called is False


def test_middleware_rejects_wrong_token() -> None:
    import asyncio

    rec = _Recorder()
    mw = BearerAuthMiddleware(lambda *_a: None, token="s3cret")
    asyncio.run(mw(_http_scope("Bearer nope"), _noop_receive, rec.send))
    assert rec.status == 401


def test_middleware_reads_the_body_before_refusing() -> None:
    """Regression: answering before the body arrived let the TCP stack reset
    the connection, and on Windows the reset ate the 401 (flaky bridge test)."""
    import asyncio

    events: list[str] = []
    chunks = [b"{", b'"a"', b"}"]

    async def receive() -> dict:
        events.append("receive")
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    async def send(message: dict) -> None:
        events.append(message["type"])

    mw = BearerAuthMiddleware(lambda *_a: None, token="s3cret")
    asyncio.run(mw(_http_scope(None), receive, send))
    assert events == [
        "receive",
        "receive",
        "receive",
        "http.response.start",
        "http.response.body",
    ]


def test_middleware_stops_draining_an_endless_body() -> None:
    import asyncio

    reads = 0

    async def endless() -> dict:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 16384, "more_body": True}

    rec = _Recorder()
    mw = BearerAuthMiddleware(lambda *_a: None, token="s3cret")
    asyncio.run(mw(_http_scope(None), endless, rec.send))
    assert rec.status == 401
    assert reads == 4  # 64 KiB in 16 KiB chunks


def test_middleware_passes_correct_token() -> None:
    import asyncio

    seen = False

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        nonlocal seen
        seen = True

    rec = _Recorder()
    mw = BearerAuthMiddleware(inner, token="s3cret")
    asyncio.run(mw(_http_scope("Bearer s3cret"), _noop_receive, rec.send))
    assert seen is True
    assert rec.status is None  # inner handled it; middleware sent nothing


def test_middleware_passes_non_http_scopes_through() -> None:
    import asyncio

    seen = False

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        nonlocal seen
        seen = True

    mw = BearerAuthMiddleware(inner, token="s3cret")
    # A lifespan scope must not be blocked, or the transport never starts.
    asyncio.run(mw({"type": "lifespan"}, _noop_receive, _Recorder().send))
    assert seen is True
