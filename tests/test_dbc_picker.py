# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.dbc_picker.DbcPicker``.

Parsing happens on a worker thread, so the assertions wait for the entry to
settle (``qtbot.waitUntil``). The valid file comes from the shared
``example_dbc_path`` fixture (3 messages); the invalid case writes garbage.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from ithildin.ui.dbc_picker import DbcPicker


def _drop_event(paths: list[Path]) -> tuple[QDropEvent, QMimeData]:
    # The caller must hold the returned QMimeData: QDropEvent does not take
    # ownership, and event.mimeData() dangles once Python frees it.
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    return event, mime


def test_valid_dbc_parses_and_appears_in_results(qapp, qtbot, example_dbc_path) -> None:  # type: ignore[no-untyped-def]
    picker = DbcPicker()
    qtbot.addWidget(picker)
    picker._add_path(example_dbc_path)

    qtbot.waitUntil(lambda: bool(picker.results()), timeout=4000)

    assert picker.is_ready() is True
    results = picker.results()
    assert len(results) == 1
    path, decoder = results[0]
    assert path == example_dbc_path
    assert len(decoder.database.messages) == 3  # _DBC_SOURCE defines 3
    assert "3 msgs" in picker._entries[example_dbc_path].status.text()


def test_invalid_dbc_shows_error_and_blocks_ready(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "broken.dbc"
    bad.write_text("this is not a dbc", encoding="utf-8")
    picker = DbcPicker()
    qtbot.addWidget(picker)
    picker._add_path(bad)

    qtbot.waitUntil(lambda: picker._entries[bad].state == "error", timeout=4000)

    assert picker.results() == []
    assert picker.is_ready() is False
    assert "✗" in picker._entries[bad].status.text()


def test_remove_entry_clears_it(qapp, qtbot, example_dbc_path) -> None:  # type: ignore[no-untyped-def]
    picker = DbcPicker()
    qtbot.addWidget(picker)
    picker._add_path(example_dbc_path)
    qtbot.waitUntil(lambda: bool(picker.results()), timeout=4000)

    picker.remove_path(example_dbc_path)

    assert example_dbc_path not in picker._entries
    assert picker.results() == []
    assert picker.is_ready() is True  # empty picker is ready


def test_duplicate_path_is_ignored(qapp, qtbot, example_dbc_path) -> None:  # type: ignore[no-untyped-def]
    picker = DbcPicker()
    qtbot.addWidget(picker)
    picker._add_path(example_dbc_path)
    picker._add_path(example_dbc_path)
    qtbot.waitUntil(lambda: bool(picker.results()), timeout=4000)
    assert len(picker._entries) == 1


def test_drop_dbc_files_adds_them(qapp, qtbot, tmp_path, example_dbc_path) -> None:  # type: ignore[no-untyped-def]
    second = tmp_path / "second.dbc"
    second.write_text(example_dbc_path.read_text(encoding="utf-8"), encoding="utf-8")
    picker = DbcPicker()
    qtbot.addWidget(picker)

    event, _mime = _drop_event([example_dbc_path, second])
    picker.dropEvent(event)

    assert event.isAccepted()
    qtbot.waitUntil(lambda: len(picker.results()) == 2, timeout=4000)
    assert {p for p, _decoder in picker.results()} == {example_dbc_path, second}


def test_drop_ignores_non_dbc_files(qapp, qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    txt = tmp_path / "notes.txt"
    txt.write_text("hello", encoding="utf-8")
    picker = DbcPicker()
    qtbot.addWidget(picker)

    event, _mime = _drop_event([txt])
    picker.dropEvent(event)

    assert not event.isAccepted()
    assert picker._entries == {}
