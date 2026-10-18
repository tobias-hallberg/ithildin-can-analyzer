# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Font helpers — UI default and tabular variants.

The application bootstrap (``ithildin.app._apply_default_font``) sets
``QApplication``'s default font to a UI sans-serif fallback chain
(Inter → Segoe UI → system-ui → sans-serif).

Cells where digit width matters (Trace ID column, audit feed, Frame
Inspector bytes) ask for ``tabular_font()`` instead, which prefers
JetBrains Mono and falls back through the platform monospace stack.
"""

from __future__ import annotations

from PySide6.QtGui import QFont

UI_FAMILIES = ("Inter", "Inter Tight", "Segoe UI", "system-ui", "sans-serif")
TABULAR_FAMILIES = (
    "JetBrains Mono",
    "Cascadia Mono",
    "Consolas",
    "Menlo",
    "DejaVu Sans Mono",
    "monospace",
)


def ui_font(point_size: int = 10) -> QFont:
    f = QFont()
    f.setFamilies(list(UI_FAMILIES))
    f.setPointSize(point_size)
    return f


def tabular_font(point_size: int = 10) -> QFont:
    f = QFont()
    f.setFamilies(list(TABULAR_FAMILIES))
    f.setPointSize(point_size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f
