# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Settings dialog. Project-scoped preferences live here.

Currently exposes one section — **Project · Plot defaults**. Capture
and MCP tabs land alongside as the underlying features grow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ithildin.ui.signal_plot import (
    _WINDOW_PRESETS,
    _format_window_seconds,
    _parse_window_text,
)

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext


class SettingsDialog(QDialog):
    DEFAULT_SIZE = (540, 440)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        w, h = self.DEFAULT_SIZE
        self.resize(w, h)

        self._ctx: AppContext | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_project_tab(), "Project")
        layout.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def bind_app(self, ctx: AppContext) -> None:
        """Bind to the live project so OK can persist user-edited values."""
        self._ctx = ctx
        prefs = ctx.project.workspace.preferences
        self._plot_window_combo.blockSignals(True)
        try:
            self._plot_window_combo.setCurrentText(
                _format_window_seconds(prefs.default_plot_window_seconds)
            )
        finally:
            self._plot_window_combo.blockSignals(False)

    def _build_project_tab(self) -> QWidget:
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(8, 12, 8, 8)
        page_layout.setSpacing(10)

        title = QLabel("Plot defaults", page)
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        page_layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        self._plot_window_combo = QComboBox(page)
        self._plot_window_combo.setEditable(True)
        self._plot_window_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for label, _value in _WINDOW_PRESETS:
            self._plot_window_combo.addItem(label)
        self._plot_window_combo.setToolTip(
            "Initial time window for new Signal Plot windows. Each plot window can override this."
        )

        form.addRow("Time window:", self._plot_window_combo)
        page_layout.addLayout(form)

        hint = QLabel(
            "Applies to plot windows opened after this change. "
            "Existing windows keep their current setting.",
            page,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        page_layout.addWidget(hint)

        page_layout.addStretch(1)
        return page

    def _on_accept(self) -> None:
        if self._ctx is None:
            self.accept()
            return
        ws = self._ctx.project.workspace
        new_value = _parse_window_text(self._plot_window_combo.currentText())
        if new_value is None:
            # Reject garbage silently; user can fix and re-press OK.
            return
        if new_value != ws.preferences.default_plot_window_seconds:
            updated = ws.model_copy(
                update={
                    "preferences": ws.preferences.model_copy(
                        update={"default_plot_window_seconds": new_value}
                    )
                }
            )
            self._ctx.project.set_workspace(updated)
            if self._ctx.project.is_saved:
                self._ctx.project.save()
        self.accept()
