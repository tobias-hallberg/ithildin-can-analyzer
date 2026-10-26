# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""The quit path of ``app.main``: ``_finish`` after the event loop ends."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from functools import partial
from pathlib import Path

from ithildin import app as app_mod
from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.diagnostics import ReportRelay
from ithildin.workspace.schema import BusConfig
from tests.fake_bus import FakeBus, use_fake_bus


def _virtual() -> BusConfig:
    return BusConfig(
        name="CAN_1", interface="virtual", channel=f"q-{uuid.uuid4().hex[:8]}", bitrate=500000
    )


def _relay(tmp_path: Path) -> ReportRelay:
    relay = ReportRelay(tmp_path / "ithildin-test.log")
    logging.getLogger().addHandler(relay.handler)
    return relay


def test_quit_returns_once_every_bus_worker_stopped(
    qapp, qtbot, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)
    relay = _relay(tmp_path)
    reader = BusReader()
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(_virtual())

    assert app_mod._finish(reader, relay, 3) == 3
    assert exits == []
    assert reader.bus_names() == []
    assert relay.handler not in logging.getLogger().handlers


def test_quit_with_a_stuck_bus_worker_exits_without_teardown(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch, caplog
) -> None:
    """Destroying the stuck worker's running QThread would abort the process."""
    calls: list[str] = []
    monkeypatch.setattr(os, "_exit", lambda rc: calls.append(f"exit {rc}"))
    monkeypatch.setattr(logging, "shutdown", lambda: calls.append("logging shutdown"))
    fake = FakeBus()
    fake.block = threading.Event()
    use_fake_bus(monkeypatch, fake)
    relay = _relay(tmp_path)
    reader = BusReader()
    monkeypatch.setattr(reader, "shutdown", partial(reader.shutdown, wait_ms=50))
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(_virtual())
    # Hung in recv: a stop requested before that lets the worker exit cleanly.
    qtbot.waitUntil(lambda: fake.recv_calls > 0, timeout=2000)
    try:
        app_mod._finish(reader, relay, 3)

        assert calls == ["logging shutdown", "exit 3"]
        assert "Bus worker(s) ['CAN_1'] still running at exit; forcing exit" in caplog.text
        assert relay.handler not in logging.getLogger().handlers
    finally:
        with qtbot.waitSignal(reader.bus_released, timeout=2000):
            fake.block.set()
