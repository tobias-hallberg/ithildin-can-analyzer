# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.settings_window`` and the export preferences it edits."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from ithildin.core.signal_export import COMMA, SEMICOLON
from ithildin.ui import export_prefs
from ithildin.ui.settings_window import SettingsDialog
from tests.conftest import make_app_context


def test_csv_format_loads_previews_and_saves(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    export_prefs.save_csv_format("semicolon")
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    combo = dlg._csv_format_combo

    assert combo.currentData() == "semicolon"
    assert dlg._csv_sample.text() == "time_s;Engine.Speed [rpm];Engine.Gear\n0,125000;812,5;3"

    combo.setCurrentIndex(combo.findData("comma"))
    assert dlg._csv_sample.text() == "time_s,Engine.Speed [rpm],Engine.Gear\n0.125000,812.5,3"

    dlg._on_accept()
    assert export_prefs.load_csv_format() == "comma"


def test_csv_format_saves_with_a_project_bound(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(make_app_context(tmp_path))
    assert dlg._csv_format_combo.currentData() == "system"

    dlg._csv_format_combo.setCurrentIndex(dlg._csv_format_combo.findData("semicolon"))
    dlg._on_accept()
    assert export_prefs.load_csv_format() == "semicolon"


def test_unknown_stored_format_falls_back_to_system(qapp) -> None:  # type: ignore[no-untyped-def]
    QSettings(export_prefs.SETTINGS_ORG, export_prefs.SETTINGS_APP).setValue(
        "export/csv_format", "tab"
    )
    assert export_prefs.load_csv_format() == "system"


def test_system_format_follows_the_decimal_mark(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _Locale:
        mark = ","

        @classmethod
        def system(cls) -> type[_Locale]:
            return cls

        @classmethod
        def decimalPoint(cls) -> str:
            return cls.mark

    monkeypatch.setattr(export_prefs, "QLocale", _Locale)
    assert export_prefs.csv_dialect("system") == SEMICOLON
    assert export_prefs.csv_dialect("comma") == COMMA
    _Locale.mark = "."
    assert export_prefs.csv_dialect("system") == COMMA
    assert export_prefs.csv_dialect("semicolon") == SEMICOLON
