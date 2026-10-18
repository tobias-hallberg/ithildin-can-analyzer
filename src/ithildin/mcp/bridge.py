# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Qt↔asyncio bridge for the FastMCP server.

A worker thread runs its own asyncio event loop hosting FastMCP. The
GUI thread keeps Qt's loop. They communicate by:
- the bridge calling ``QMetaObject``-friendly signals back into the GUI
  (started / stopped / error),
- ``call_soon_threadsafe`` dispatching shutdown into the asyncio loop.

We deliberately do NOT use ``qasync``: a misbehaving tool handler must
not be able to stall paint events in the GUI thread (see plan + plan-
agent critique). Hand-rolled bridge keeps the loops independent.

The bridge runs the **streamable-http** transport — the long-running-app
path, where a client attaches to an already-open GUI session. Stdio (CLI
agent subprocess attach) and sse are not exposed; the FastMCP instance
can run either, only the bridge's ``_main`` would change.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from ithildin.mcp.auth import BearerAuthMiddleware
from ithildin.mcp.server import build_server

if TYPE_CHECKING:
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    from ithildin.mcp.tools import McpReadContext, McpWriteContext


# How long uvicorn gets to unwind connections + ASGI lifespan on stop
# before we stop waiting. Must stay under ``stop()``'s thread-join budget.
_GRACEFUL_STOP_S = 2.0


class McpServerBridge(QObject):
    """Owns one FastMCP + one asyncio loop running in a worker thread."""

    started = Signal(str)  # public URL once the server is listening
    stopped = Signal()
    error = Signal(str)

    def __init__(
        self,
        read_ctx: McpReadContext,
        write_ctx: McpWriteContext | None = None,
        token: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._read_ctx = read_ctx
        self._write_ctx = write_ctx
        # The transport is always token-gated. A caller (app.py) passes the
        # persisted per-user token; when absent (tests) we mint an ephemeral
        # one so the server is never accidentally open.
        self._token = token or secrets.token_urlsafe(32)
        self._mcp: FastMCP | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._server: uvicorn.Server | None = None
        self._bind: tuple[str, int] | None = None

    def _bind_label(self) -> str:
        if self._bind is None:
            return "the configured address"
        return f"{self._bind[0]}:{self._bind[1]}"

    @property
    def token(self) -> str:
        """The bearer token a client must present as ``Authorization``."""
        return self._token

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def server(self) -> FastMCP | None:
        return self._mcp

    def start(self, host: str = "127.0.0.1", port: int = 7320) -> None:
        """Build the FastMCP and start the worker thread.

        Returns immediately; ``started`` is emitted from the asyncio
        loop once the HTTP server is listening.
        """
        if self.is_running:
            return
        self._mcp = build_server(self._read_ctx, write_ctx=self._write_ctx, host=host, port=port)
        self._bind = (host, port)
        self._thread = threading.Thread(
            target=self._run,
            name="ithildin-mcp",
            daemon=True,
        )
        self._thread.start()

    def stop(self, wait_s: float = 3.0) -> bool:
        """Signal shutdown and join the thread. Returns whether it exited."""
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(wait_s)
        joined = self._thread is None or not self._thread.is_alive()
        if joined:
            self._thread = None
            self._loop = None
            self._stop_event = None
            self._mcp = None
            self._server = None
        return joined

    # ---- Worker-thread side ----

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except SystemExit as exc:
            # uvicorn calls sys.exit(1) when it can't bind. That's a
            # BaseException, so a bare ``except Exception`` would let the
            # thread die silently and the dialog would show "running" for
            # one tick before flipping to stopped with no explanation.
            self._emit_error(
                f"MCP server could not bind {self._bind_label()} "
                f"(address already in use?) — exit {exc.code}"
            )
        except Exception as exc:
            self._emit_error(f"MCP loop error: {exc}")
        finally:
            try:
                # A bind failure unwinds ``_main`` mid-await, orphaning the
                # tasks it created. Reap them before closing the loop or
                # asyncio logs "Task was destroyed but it is pending".
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._emit_stopped()

    async def _main(self) -> None:
        import uvicorn

        assert self._mcp is not None
        self._stop_event = asyncio.Event()

        url = (
            f"http://{self._mcp.settings.host}:{self._mcp.settings.port}"
            f"{self._mcp.settings.streamable_http_path}"
        )

        # Wrap FastMCP's Starlette app in the bearer-auth gate, then host it
        # under uvicorn ourselves (rather than run_streamable_http_async) so
        # the middleware sees every request before a tool handler runs.
        app = BearerAuthMiddleware(self._mcp.streamable_http_app(), self._token)
        config = uvicorn.Config(
            app,
            host=self._mcp.settings.host,
            port=self._mcp.settings.port,
            log_level="warning",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        self._server = server

        server_task = asyncio.create_task(server.serve(), name="ithildin-mcp-http")
        stop_task = asyncio.create_task(self._stop_event.wait(), name="ithildin-mcp-stop-wait")

        # Announce ready only once uvicorn flips ``started`` — it sets that
        # after the socket is bound and listening. Emitting on a blind sleep
        # reported success for a bind that had already failed.
        if await self._await_bound(server, server_task):
            try:
                self.started.emit(url)
            except RuntimeError:
                pass

        done, _pending = await asyncio.wait(
            [server_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Retrieve any stored exception so it's surfaced rather than logged
        # by asyncio as "exception was never retrieved".
        for t in done:
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                continue
            if exc is not None:
                self._emit_error(f"MCP server stopped: {exc}")

        # Ask uvicorn to unwind its own accept loop, live connections and
        # ASGI lifespan, then *await* it. Cancelling ``serve()`` instead
        # kills the lifespan task mid-receive, which starlette logs as a
        # CancelledError traceback on every shutdown.
        server.should_exit = True
        if not server_task.done():
            try:
                await asyncio.wait_for(server_task, timeout=_GRACEFUL_STOP_S)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:
                self._emit_error(f"task crash during shutdown: {exc}")
        if not stop_task.done():
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass

    async def _await_bound(
        self,
        server: uvicorn.Server,
        server_task: asyncio.Task[None],
        timeout_s: float = 10.0,
    ) -> bool:
        """Wait until uvicorn is listening. ``False`` if it never got there."""
        step = 0.02
        waited = 0.0
        while waited < timeout_s:
            if server_task.done():
                return False
            if server.started:
                return True
            await asyncio.sleep(step)
            waited += step
        self._emit_error(
            f"MCP server did not start listening on {self._bind_label()} within {timeout_s:.0f}s"
        )
        return False

    def _emit_error(self, message: str) -> None:
        try:
            self.error.emit(message)
        except RuntimeError:
            pass

    def _emit_stopped(self) -> None:
        try:
            self.stopped.emit()
        except RuntimeError:
            pass
