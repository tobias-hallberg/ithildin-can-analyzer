# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Reusable collapsible section: header band + inner widget.

A horizontal header band (object name ``PaneToggleHeader``) hosts a
chevron toggle (object name ``PaneToggleButton``) plus optional small
icon-only action buttons appended via :meth:`add_header_action`. Click
the toggle to collapse: the inner widget hides and the section's height
is pinned to the header band so a surrounding splitter or layout
reclaims the freed space.

Originated as ``_CollapsibleSection`` inside :mod:`ithildin.ui.left_dock`;
promoted here so other panes (e.g. the Trace window's frame-detail pane)
can wear the same chevron-toggle chrome.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ithildin.ui.icons import themed_icon

HEADER_BAND_HEIGHT = 22


class CollapsibleSection(QWidget):
    """Header band + inner widget, expand/collapse on header click.

    The header is a horizontal band (object name ``PaneToggleHeader``)
    that paints the section's title strip. Inside it sits the toggle
    button (object name ``PaneToggleButton``) plus any optional action
    buttons appended via :meth:`add_header_action`.
    """

    toggled = Signal(bool)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._title = title

        self._header = QWidget(self)
        self._header.setObjectName("PaneToggleHeader")
        # WA_StyledBackground so the QSS background paints — without it
        # the QWidget container wouldn't render its own fill, and the
        # header band would only be visible under the toggle button.
        self._header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Pin the band to a fixed height so sections with action buttons
        # don't end up taller than action-less ones. QSS padding alone
        # isn't enough — QToolButton style metrics override it on some
        # platforms.
        self._header.setFixedHeight(HEADER_BAND_HEIGHT)
        header_row = QHBoxLayout(self._header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        self._toggle = QToolButton(self._header)
        self._toggle.setObjectName("PaneToggleButton")
        self._toggle.setText(f"▾  {title.upper()}")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)
        self._toggle.setAutoRaise(True)
        self._toggle.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._toggle.setFixedHeight(HEADER_BAND_HEIGHT)
        self._toggle.toggled.connect(self._on_toggled)
        # Toggle takes only its natural width and the stretch fills the
        # band — this keeps the title pinned to the same left offset in
        # every section, whether or not the section has action buttons.
        header_row.addWidget(self._toggle)
        header_row.addStretch(1)
        self._header_row = header_row
        outer.addWidget(self._header)

        self._inner = QWidget(self)
        self._inner_layout = QVBoxLayout(self._inner)
        # Zero left/right indent: rows live flush with the header text so
        # nested columns line up with the toggle arrow.
        self._inner_layout.setContentsMargins(0, 0, 0, 4)
        self._inner_layout.setSpacing(2)
        outer.addWidget(self._inner)

    def add_header_action(
        self,
        *,
        icon_name: str | None = None,
        text: str | None = None,
        tooltip: str,
        callback: Callable[[], None],
    ) -> QToolButton:
        """Append a small action button to the right of the header.

        Pass either ``icon_name`` (icon-only mode, 14×14 px) or ``text``
        (text-only mode) — not both. Returns the button so callers can
        flip its enabled state from background tasks (e.g. while a DBC
        is parsing).
        """
        if (icon_name is None) == (text is None):
            raise ValueError("provide exactly one of icon_name or text")
        btn = QToolButton(self._header)
        btn.setObjectName("PaneHeaderAction")
        if text is not None:
            btn.setText(text)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        else:
            assert icon_name is not None
            themed_icon(icon_name, btn.setIcon)
            btn.setIconSize(QSize(14, 14))
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        btn.setFixedHeight(HEADER_BAND_HEIGHT)
        btn.setToolTip(tooltip)
        btn.setAutoRaise(True)
        btn.clicked.connect(callback)
        self._header_row.addWidget(btn)
        return btn

    def set_inner(self, widget: QWidget) -> None:
        # Replace any existing inner widget.
        while self._inner_layout.count():
            item = self._inner_layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._inner_layout.addWidget(widget)

    def is_expanded(self) -> bool:
        return self._toggle.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self._toggle.setChecked(expanded)

    def _on_toggled(self, expanded: bool) -> None:
        self._inner.setVisible(expanded)
        arrow = "▾" if expanded else "▸"
        self._toggle.setText(f"{arrow}  {self._title.upper()}")
        if expanded:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX — restore range
        else:
            # Pin to the header band so the surrounding splitter (if any)
            # shrinks this pane and gives the freed space to its siblings.
            self.setMinimumHeight(HEADER_BAND_HEIGHT)
            self.setMaximumHeight(HEADER_BAND_HEIGHT)
        self.toggled.emit(expanded)
