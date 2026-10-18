# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Report dock — a thin running log of cross-window events.

Bus errors, DBC parse failures, MCP transport changes, and similar
notifications append a line here. The dock auto-shows itself when a new
entry arrives; the user can dismiss it via the dock's close button.

Each row carries a kind ("warn"/"error"/"info") which colours the leading
dot. We deliberately keep this dirt-simple — it's a status log, not a
debugger.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from ithildin.ui.fonts import tabular_font
from ithildin.ui.theme import ThemeManager

ReportKind = Literal["warn", "info", "error"]

_KIND_TOKENS: dict[ReportKind, str] = {
    "warn": "warn",
    "info": "info",
    "error": "err",
}


class ReportDockWidget(QListWidget):
    """Simple chronological list. Newest on top."""

    MAX_ENTRIES = 500

    def __init__(self) -> None:
        super().__init__()
        self.setUniformItemSizes(True)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setFont(tabular_font(point_size=9))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def append(self, message: str, *, kind: ReportKind = "info") -> None:
        """Add an entry. Returns nothing — purely fire-and-forget."""
        ts = datetime.now().strftime("%H:%M:%S")
        token = _KIND_TOKENS[kind]
        tint = ThemeManager.instance().current_tokens()[token]
        # Construct unparented; insertItem positions us at row 0 (newest first).
        item = QListWidgetItem(f"{ts}  ●  {message}")
        item.setForeground(QColor(tint))
        self.insertItem(0, item)
        # Trim oldest if we've hit the cap.
        while self.count() > self.MAX_ENTRIES:
            self.takeItem(self.count() - 1)
