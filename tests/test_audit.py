# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``mcp.audit.AuditWriter``."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from ithildin.mcp.audit import AuditWriter


def test_append_writes_jsonl_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    writer = AuditWriter(path)
    writer.append({"call": "send_signal", "decision": "OK", "rule_id": 1})
    writer.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["call"] == "send_signal"
    assert parsed["decision"] == "OK"
    assert "ts" in parsed  # timestamp injected when missing


def test_explicit_timestamp_is_preserved(tmp_path: Path) -> None:
    writer = AuditWriter(tmp_path / "audit.jsonl")
    writer.append({"ts": "2026-01-01T00:00:00+00:00", "decision": "OK"})
    writer.close()
    parsed = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert parsed["ts"] == "2026-01-01T00:00:00+00:00"


def test_rotation_at_max_bytes(tmp_path: Path) -> None:
    """Rotation moves current → ``.1``, opening a fresh log each time.

    We only keep one backup, so a sustained-overflow run drops the
    earliest entries — the test verifies that the **most recent** events
    sit in the current file and a backup exists with prior content.
    """
    path = tmp_path / "audit.jsonl"
    # Cap small enough to rotate after each (or every other) entry.
    writer = AuditWriter(path, max_bytes=128)
    for i in range(8):
        writer.append({"call": "send_signal", "decision": "OK", "rule_id": i})
    writer.close()

    backup = tmp_path / "audit.jsonl.1"
    assert backup.exists()
    cur = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    # Latest event lives in the current log.
    assert cur["rule_id"] == 7
    # Backup exists with at least one prior entry.
    bak_lines = backup.read_text(encoding="utf-8").splitlines()
    assert bak_lines  # non-empty


def test_blocked_rotation_keeps_appending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows a reader holding the log open blocks the rename. The line
    still lands, the old backup survives, and the next append rotates."""
    path = tmp_path / "audit.jsonl"
    backup = tmp_path / "audit.jsonl.1"
    # A line is about 140 bytes, so a second line rotates the log.
    writer = AuditWriter(path, max_bytes=200)
    for i in range(2):
        writer.append({"i": i, "filler": "x" * 80})

    def in_use(_src: object, _dst: object) -> None:
        raise PermissionError(32, "The process cannot access the file")

    def ids(p: Path) -> list[int]:
        return [json.loads(line)["i"] for line in p.read_text(encoding="utf-8").splitlines()]

    with monkeypatch.context() as m:
        m.setattr(os, "replace", in_use)
        writer.append({"i": 2, "filler": "x" * 80})
    assert ids(backup) == [0]
    writer.append({"i": 3, "filler": "x" * 80})
    writer.close()

    assert ids(backup) == [1, 2]
    assert ids(path) == [3]


def test_concurrent_appends_do_not_interleave(tmp_path: Path) -> None:
    """Two threads hammering append() must each produce intact JSON lines."""
    path = tmp_path / "audit.jsonl"
    writer = AuditWriter(path)

    def hammer(tag: str) -> None:
        for i in range(50):
            writer.append({"thread": tag, "i": i, "filler": "x" * 64})

    threads = [threading.Thread(target=hammer, args=(f"t{j}",)) for j in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    writer.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    # Every line is parseable JSON — no torn writes.
    parsed = [json.loads(line) for line in lines]
    assert all("thread" in p and "i" in p for p in parsed)
