# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Toolbar "Open project" split-button.

Main face: opens the file dialog (emits ``open_requested``).
Dropdown arrow: lists recent projects, newest first; clicking an entry
emits ``recent_chosen(path)``. The owning ``MainWindow`` rebuilds the
menu via ``set_recent`` whenever the MRU changes.

Differs from ``ViewLauncherButton`` only in popup mode — that one is
``InstantPopup`` (whole button opens the menu); this one is
``MenuButtonPopup`` so the default action fires on the main face.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QToolButton, QWidget

from ithildin.ui import icons


class OpenProjectButton(QToolButton):
    open_requested = Signal()
    recent_chosen = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # QToolButtons inserted via QToolBar.insertWidget don't inherit
        # the toolbar's toolButtonStyle (only auto-created buttons for
        # QActions do). Match the surrounding chrome explicitly.
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setText("Open")

        # Default action: main-face click fires this; emits the signal.
        self._default = QAction("Open", self)
        self._default.triggered.connect(self.open_requested)
        icons.themed_icon("open", self._default.setIcon)
        self.setDefaultAction(self._default)
        # ``setDefaultAction`` copies the action's icon onto the button;
        # subscribe the button itself too so theme switches refresh both.
        icons.themed_icon("open", self.setIcon)

        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        self.set_recent([])

    def set_recent(self, paths: list[Path]) -> None:
        try:
            self._menu.clear()
        except RuntimeError:
            return
        if not paths:
            empty = QAction("(No recent projects)", self._menu)
            empty.setEnabled(False)
            font = empty.font()
            font.setItalic(True)
            empty.setFont(font)
            self._menu.addAction(empty)
            return
        for p in paths[:_MAX_VISIBLE]:
            action = QAction(p.name, self._menu)
            action.setToolTip(str(p))
            action.triggered.connect(lambda _checked=False, path=p: self.recent_chosen.emit(path))
            self._menu.addAction(action)


_MAX_VISIBLE = 10
