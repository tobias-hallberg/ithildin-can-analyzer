# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Reusable drop-target wrapper for ``application/x-ithildin-signal``.

pyqtgraph's ``PlotWidget`` and ``QTableView`` both struggle with drop
acceptance because their inner ``GraphicsView`` / item delegate eats the
event. The fix is the same in both cases: wrap the widget in a plain
``QWidget`` whose ``dragEnterEvent`` / ``dropEvent`` we own, and decode
the MIME ourselves.

The wrapper emits ``payload_dropped(object)`` with a parsed
``SignalPayload`` or ``MessagePayload``; the parent window dispatches by
``kind``.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ithildin.ui._drag_payload import SIGNAL_MIME, parse_from_mime


class SignalDropArea(QWidget):
    """A ``QWidget`` that accepts ``application/x-ithildin-signal`` drops."""

    payload_dropped = Signal(object)  # SignalPayload | MessagePayload

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def set_inner(self, widget: QWidget) -> None:
        # Only one inner widget at a time.
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._layout.addWidget(widget)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(SIGNAL_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasFormat(SIGNAL_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        payloads = parse_from_mime(event.mimeData())
        if not payloads:
            event.ignore()
            return
        for payload in payloads:
            self.payload_dropped.emit(payload)
        event.acceptProposedAction()
