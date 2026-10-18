# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Shared top bar for MDI view content.

Every view kind that wants a header strip — Trace, Signal Plot, Record —
builds one of these and fills its content region with view-specific
controls (filter box, segmented toggles, counts). The bar owns the
chrome (background, bottom-border, height, margins) so the strips read
identically across views instead of each rolling its own ``QHBoxLayout``.

When a subwindow is maximized the native ``QMdiSubWindow`` title bar is
collapsed to 0 px (see ``_TightMdiFrameStyle`` in ``window_manager.py``),
so this bar becomes the window's only chrome: ``enter_maximized`` reveals
a title label on the left and restore/close buttons on the right.
``WindowManager`` drives that, and wires ``restore_requested`` /
``close_requested`` to the owning subwindow.

Views with no controls of their own (Monitor, Data, Replay) pass
``hide_when_idle=True``: the bar stays hidden until the window is
maximized, then surfaces just to host the title + window controls.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal, SignalInstance
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLayout,
    QToolButton,
    QWidget,
)

from ithildin.ui.icons import themed_icon

_BAR_HEIGHT = 28


class ViewHeaderBar(QWidget):
    """Uniform header strip for an MDI view.

    Layout order is ``[title] [view content…] [restore] [close]``. Views
    append content (and pane-toggle buttons) via the ``add_*`` helpers,
    which always insert *before* the trailing window controls so those
    stay pinned to the right edge.
    """

    restore_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, hide_when_idle: bool = False) -> None:
        super().__init__(parent)
        self._hide_when_idle = hide_when_idle
        self.setObjectName("ViewHeader")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(_BAR_HEIGHT)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 0, 4, 0)
        self._layout.setSpacing(6)

        self._title = QLabel(self)
        self._title.setVisible(False)
        self._layout.addWidget(self._title)

        self._restore_btn = self._make_window_button(
            "window_restore", "Restore subwindow", self.restore_requested
        )
        self._close_btn = self._make_window_button(
            "window_close", "Close subwindow", self.close_requested
        )
        self._layout.addWidget(self._restore_btn)
        self._layout.addWidget(self._close_btn)

        if self._hide_when_idle:
            self.setVisible(False)

    def _make_window_button(
        self, icon_name: str, tooltip: str, signal: SignalInstance
    ) -> QToolButton:
        btn = QToolButton(self)
        btn.setAutoRaise(True)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setToolTip(tooltip)
        themed_icon(icon_name, btn.setIcon)
        btn.clicked.connect(signal)
        btn.setVisible(False)
        return btn

    def _content_insert_index(self) -> int:
        # Two trailing window-control buttons are always last; content
        # lands just before them.
        return self._layout.count() - 2

    # ---- View build-time API ----

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self._layout.insertWidget(self._content_insert_index(), widget, stretch)

    def add_layout(self, sublayout: QLayout) -> None:
        self._layout.insertLayout(self._content_insert_index(), sublayout)

    def add_stretch(self, stretch: int = 1) -> None:
        self._layout.insertStretch(self._content_insert_index(), stretch)

    def add_pane_toggle(self, tooltip: str, slot: Callable[[], object]) -> QToolButton:
        """Add a standard flat pane-toggle button and return it.

        The caller keeps the reference to assign a state-dependent icon
        (``pane_left_on`` / ``pane_left_off``) via its own
        ``themed_callback`` refresh method.
        """
        btn = QToolButton(self)
        btn.setAutoRaise(True)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setToolTip(tooltip)
        btn.clicked.connect(slot)
        self.add_widget(btn)
        return btn

    # ---- Maximize state (driven by WindowManager) ----

    def enter_maximized(self, title: str) -> None:
        self._title.setText(title)
        self._title.setVisible(True)
        self._restore_btn.setVisible(True)
        self._close_btn.setVisible(True)
        if self._hide_when_idle:
            self.setVisible(True)

    def exit_maximized(self) -> None:
        self._title.setVisible(False)
        self._restore_btn.setVisible(False)
        self._close_btn.setVisible(False)
        if self._hide_when_idle:
            self.setVisible(False)

    def set_maximized_title(self, title: str) -> None:
        if self._title.isVisible():
            self._title.setText(title)

    def window_controls_visible(self) -> bool:
        """Intended visibility of the restore/close controls.

        Uses ``isHidden`` (not ``isVisible``) so the answer is correct
        even before the owning window is shown — handy in tests.
        """
        return not self._restore_btn.isHidden()
