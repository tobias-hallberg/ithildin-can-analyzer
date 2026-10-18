# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.fonts`` — fallback chains."""

from __future__ import annotations

from PySide6.QtGui import QFont

from ithildin.ui.fonts import (
    TABULAR_FAMILIES,
    UI_FAMILIES,
    tabular_font,
    ui_font,
)


def test_ui_font_has_inter_first_then_platform_fallbacks(qapp) -> None:  # type: ignore[no-untyped-def]
    f = ui_font()
    families = f.families()
    assert families[0] == "Inter"
    # Segoe UI is the canonical Windows fallback.
    assert "Segoe UI" in families
    assert families[-1] == "sans-serif"


def test_tabular_font_prefers_jetbrains_then_consolas(qapp) -> None:  # type: ignore[no-untyped-def]
    f = tabular_font()
    families = f.families()
    assert families[0] == "JetBrains Mono"
    assert "Consolas" in families  # Windows-bundled mono fallback
    assert families[-1] == "monospace"
    assert f.styleHint() == QFont.StyleHint.Monospace


def test_ui_font_point_size_overridable(qapp) -> None:  # type: ignore[no-untyped-def]
    assert ui_font(point_size=12).pointSize() == 12
    assert tabular_font(point_size=8).pointSize() == 8


def test_family_constants_match_factory_output(qapp) -> None:  # type: ignore[no-untyped-def]
    assert ui_font().families() == list(UI_FAMILIES)
    assert tabular_font().families() == list(TABULAR_FAMILIES)
