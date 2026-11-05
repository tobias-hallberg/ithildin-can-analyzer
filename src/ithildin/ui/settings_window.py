# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Settings dialog.

Two tabs: **Project · Plot defaults**, saved in the ``.itp``, and
**Export · CSV files**, saved per user in ``QSettings``
(``ui/export_prefs.py``). Capture and MCP tabs land alongside as the
underlying features grow.
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

from ithildin.core.signal_export import format_time, format_value
from ithildin.ui.export_prefs import (
    CSV_FORMATS,
    CsvFormat,
    csv_dialect,
    load_csv_format,
    save_csv_format,
)
from ithildin.ui.fonts import tabular_font
from ithildin.ui.signal_plot import (
    _WINDOW_PRESETS,
    _format_window_seconds,
    _parse_window_text,
)

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext


def _csv_format_label(fmt: CsvFormat) -> str:
    if fmt == "comma":
        return "Comma separated, decimal point (1.5)"
    if fmt == "semicolon":
        return "Semicolon separated, decimal comma (1,5)"
    now = "semicolon, 1,5" if csv_dialect("system").delimiter == ";" else "comma, 1.5"
    return f"Match this computer's region (now {now})"


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
        self._tabs.addTab(self._build_export_tab(), "Export")
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

    def _build_export_tab(self) -> QWidget:
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(8, 12, 8, 8)
        page_layout.setSpacing(10)

        title = QLabel("CSV files", page)
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        page_layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        self._csv_format_combo = QComboBox(page)
        for fmt in CSV_FORMATS:
            self._csv_format_combo.addItem(_csv_format_label(fmt), fmt)
        self._csv_format_combo.setCurrentIndex(self._csv_format_combo.findData(load_csv_format()))
        self._csv_format_combo.setToolTip(
            "Spreadsheets in regions that write 1,5 expect semicolon-separated files."
        )
        self._csv_format_combo.currentIndexChanged.connect(self._refresh_csv_sample)
        form.addRow("Format:", self._csv_format_combo)

        self._csv_sample = QLabel(page)
        self._csv_sample.setFont(tabular_font())
        self._csv_sample.setStyleSheet("color: palette(mid);")
        self._csv_sample.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Example:", self._csv_sample)
        page_layout.addLayout(form)
        self._refresh_csv_sample()

        hint = QLabel(
            "Used by the Signal Plot's Export. Saved on this computer, not in the project.",
            page,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        page_layout.addWidget(hint)

        page_layout.addStretch(1)
        return page

    def _selected_csv_format(self) -> CsvFormat:
        fmt: CsvFormat = self._csv_format_combo.currentData()
        return fmt

    def _refresh_csv_sample(self) -> None:
        d = csv_dialect(self._selected_csv_format())
        header = d.delimiter.join(["time_s", "Engine.Speed [rpm]", "Engine.Gear"])
        row = d.delimiter.join([format_time(0.125, d), format_value(812.5, d), "3"])
        self._csv_sample.setText(f"{header}\n{row}")

    def _on_accept(self) -> None:
        if self._ctx is None:
            save_csv_format(self._selected_csv_format())
            self.accept()
            return
        ws = self._ctx.project.workspace
        new_value = _parse_window_text(self._plot_window_combo.currentText())
        if new_value is None:
            # Reject garbage silently; user can fix and re-press OK.
            return
        save_csv_format(self._selected_csv_format())
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
