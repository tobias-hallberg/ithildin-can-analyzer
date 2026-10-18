# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Window tray — bottom rail listing every open MDI sub-window (handoff §2.2).

Not a tab bar: a row of buttons, one per ``QMdiSubWindow``. Active
button gets accent styling; minimised buttons go italic + chevron-prefix
+ dimmed text. Click on minimised → restore + focus. Click on visible →
focus.

We render the rail ourselves rather than using ``QMdiArea::TabbedView``
because handoff §2.2 explicitly calls for the rail style.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QHBoxLayout,
    QMdiArea,
    QMdiSubWindow,
    QPushButton,
    QWidget,
)
from shiboken6 import isValid

from ithildin.ui.icons import themed_callback
from ithildin.ui.theme import ThemeManager

if TYPE_CHECKING:
    from ithildin.ui.window_manager import WindowManager


def _active_qss(tokens: dict[str, str]) -> str:
    return (
        f"QPushButton {{ background: {tokens['bg_selected_strong']}; "
        f"border-top: 2px solid {tokens['accent']}; "
        f"color: {tokens['fg_0']}; padding: 2px 10px; }}"
    )


def _inactive_qss(tokens: dict[str, str]) -> str:
    return (
        f"QPushButton {{ background: transparent; border: 1px solid {tokens['border_2']}; "
        f"color: {tokens['fg_1']}; padding: 2px 10px; }}"
        f"QPushButton:hover {{ background: {tokens['bg_hover']}; }}"
    )


def _minimised_qss(tokens: dict[str, str]) -> str:
    return (
        f"QPushButton {{ background: transparent; border: 1px dashed {tokens['border_2']}; "
        f"color: {tokens['fg_3']}; font-style: italic; padding: 2px 10px; }}"
        f"QPushButton:hover {{ background: {tokens['bg_hover']}; color: {tokens['fg_1']}; }}"
    )


class WindowTrayWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._wm: WindowManager | None = None
        self._area: QMdiArea | None = None
        self._buttons: dict[int, QPushButton] = {}  # window_id -> button
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(4)
        layout.addStretch(1)
        self._layout = layout
        self.setFixedHeight(28)
        themed_callback(self._restyle_all)

    def bind(self, wm: WindowManager) -> None:
        self._wm = wm
        self._area = wm.area
        wm.window_opened.connect(self._on_opened)
        wm.window_closed.connect(self._on_closed)
        wm.window_renamed.connect(self._on_renamed)
        self._area.subWindowActivated.connect(self._on_activated)

    # ---- Slots ----

    def _on_opened(self, window_id: int, _kind: str) -> None:
        if self._wm is None:
            return
        for entry in self._wm.list_windows():
            if entry.window_id != window_id:
                continue
            btn = QPushButton(entry.view.windowTitle() or entry.kind, self)
            btn.setCheckable(False)
            btn.clicked.connect(lambda _checked=False, wid=window_id: self._focus(wid))
            # Insert before the stretch.
            self._layout.insertWidget(self._layout.count() - 1, btn)
            self._buttons[window_id] = btn
            entry.subwindow.windowStateChanged.connect(
                lambda _old, _new, sw=entry.subwindow, wid=window_id: self._restyle(wid, sw)
            )
            self._restyle(window_id, entry.subwindow)
            return

    def _on_closed(self, window_id: int) -> None:
        btn = self._buttons.pop(window_id, None)
        if btn is None:
            return
        btn.setParent(None)
        btn.deleteLater()

    def _on_activated(self, _sub: QMdiSubWindow | None) -> None:
        if self._wm is None:
            return
        for entry in self._wm.list_windows():
            self._restyle(entry.window_id, entry.subwindow)

    def _on_renamed(self, window_id: int) -> None:
        if self._wm is None:
            return
        for entry in self._wm.list_windows():
            if entry.window_id == window_id:
                self._restyle(window_id, entry.subwindow)
                return

    # ---- Internal ----

    def _focus(self, window_id: int) -> None:
        if self._wm is None or self._area is None:
            return
        for entry in self._wm.list_windows():
            if entry.window_id == window_id:
                if entry.subwindow.isMinimized():
                    entry.subwindow.showNormal()
                else:
                    entry.subwindow.show()
                self._area.setActiveSubWindow(entry.subwindow)
                return

    def _restyle(self, window_id: int, sub: QMdiSubWindow) -> None:
        btn = self._buttons.get(window_id)
        if btn is None:
            return
        # A queued windowStateChanged can arrive after the subwindow's C++
        # object is torn down (close during teardown / theme switch). Touching
        # it then raises; drop the stale restyle instead.
        if not isValid(sub):
            return
        tokens = ThemeManager.instance().current_tokens()
        title = sub.widget().windowTitle() if sub.widget() else ""
        if sub.isMinimized():
            btn.setText(f"▸ {title}")
            btn.setStyleSheet(_minimised_qss(tokens))
            return
        btn.setText(title)
        active = self._area is not None and self._area.activeSubWindow() is sub
        btn.setStyleSheet(_active_qss(tokens) if active else _inactive_qss(tokens))

    def _restyle_all(self) -> None:
        if self._wm is None:
            return
        for entry in self._wm.list_windows():
            self._restyle(entry.window_id, entry.subwindow)
