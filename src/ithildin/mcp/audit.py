# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""JSONL audit log for MCP send attempts.

Every allow/deny decision the bridge makes is appended to this file as
one JSON object per line. The file rotates when it crosses
``max_bytes`` — the existing log moves to ``<path>.1`` (overwriting any
previous rotation) and a fresh log opens.

Pure I/O — no Qt, no asyncio. The bridge calls ``append`` from any
thread; appends are guarded by a lock so concurrent writers don't
interleave a line.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


class AuditWriter:
    """Append-only JSONL writer with size-triggered rotation."""

    def __init__(self, path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._fp: IO[bytes] | None = None
        self._size = 0

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: dict[str, Any]) -> None:
        """Write one JSON line. Adds a ``ts`` ISO-8601 timestamp if missing."""
        if "ts" not in event:
            event = {"ts": datetime.now(UTC).isoformat(), **event}
        line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self._ensure_open()
            assert self._fp is not None
            if self._size + len(line) > self._max_bytes:
                self._rotate()
                self._ensure_open()
                assert self._fp is not None
            self._fp.write(line)
            self._fp.flush()
            self._size += len(line)

    def close(self) -> None:
        with self._lock:
            if self._fp is not None:
                self._fp.close()
                self._fp = None

    # ---- Internal ----

    def _ensure_open(self) -> None:
        if self._fp is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 0o600 at creation: the audit trail records which agent sent
            # what to the bus — not something to leave world-readable on a
            # shared machine. Honored on POSIX; a no-op mode on Windows.
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            self._fp = os.fdopen(fd, "ab")
            self._size = self._path.stat().st_size if self._path.exists() else 0

    def _rotate(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None
        backup = self._path.with_suffix(self._path.suffix + ".1")
        if backup.exists():
            backup.unlink()
        if self._path.exists():
            os.replace(self._path, backup)
        self._size = 0
