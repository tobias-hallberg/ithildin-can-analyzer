# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Drop-target wrapper accepting all three ithildin MIME types.

The merged Trace view's Tracked pane is a single drop area that takes
``BusPayload``, ``DbcPayload``, ``SignalPayload``, or ``MessagePayload``.
The owning widget dispatches by payload type.

Format priority on a multi-format ``QMimeData``: bus > dbc > signal.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ithildin.ui._drag_payload import (
    BUS_MIME,
    DBC_MIME,
    SIGNAL_MIME,
    parse_from_mime,
)

_ACCEPTED = (BUS_MIME, DBC_MIME, SIGNAL_MIME)


class TraceDropArea(QWidget):
    """A ``QWidget`` that accepts bus, DBC, signal, and message drops."""

    payload_dropped = Signal(object)  # BusPayload | DbcPayload | SignalPayload | MessagePayload

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def set_inner(self, widget: QWidget) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._layout.addWidget(widget)

    def _accepts(self, event: QDragEnterEvent | QDragMoveEvent) -> bool:
        mime = event.mimeData()
        return any(mime.hasFormat(fmt) for fmt in _ACCEPTED)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._accepts(event):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._accepts(event):
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
