# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.dbc_loader.DbcLoader``."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread

from ithildin.core.decoder import Decoder
from ithildin.ui.dbc_loader import DbcLoader


def _run_on_thread(loader: DbcLoader) -> QThread:
    """Mirror the dock's lifecycle: move worker, kick on started."""
    thread = QThread()
    loader.moveToThread(thread)
    thread.started.connect(loader.run)
    loader.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.start()
    return thread


def test_dbc_loader_emits_loaded(qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    loader = DbcLoader(example_dbc_path)
    with qtbot.waitSignal(loader.loaded, timeout=5000) as blocker:
        thread = _run_on_thread(loader)
    assert thread.wait(2000)
    path, decoder = blocker.args
    assert path == example_dbc_path
    assert isinstance(decoder, Decoder)
    assert decoder.message_name(0x100) == "EngineCommand"


def test_dbc_loader_emits_failed(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "broken.dbc"
    bad.write_text("not a dbc", encoding="utf-8")
    loader = DbcLoader(bad)
    with qtbot.waitSignal(loader.failed, timeout=5000) as blocker:
        thread = _run_on_thread(loader)
    assert thread.wait(2000)
    path, _reason = blocker.args
    assert path == bad
