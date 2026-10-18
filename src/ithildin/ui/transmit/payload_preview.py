# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Live encoded-payload strip rendered at the bottom of the TX editor.

Renders ``PAYLOAD  0x{id}  ·  {dlc} B  ·  {hex bytes}`` in mono. The
window calls :meth:`update_for` whenever a signal value changes; the
strip re-encodes via :func:`encode_tx_message` and re-renders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from ithildin.core.transmit import TxMessage, encode_tx_message
from ithildin.ui.fonts import tabular_font
from ithildin.ui.theme import ThemeManager

if TYPE_CHECKING:
    from ithildin.app_state.database import Database


class TxPayloadPreview(QFrame):
    """Bottom strip of the editor showing the live-encoded CAN bytes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._database: Database | None = None
        self.setObjectName("TxPayloadStrip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(28)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 12, 4)
        row.setSpacing(10)
        self._label = QLabel("PAYLOAD", self)
        self._label.setObjectName("TxPayloadCaption")
        self._label.setFont(tabular_font())
        row.addWidget(self._label)
        self._content = QLabel("—", self)
        self._content.setObjectName("TxPayloadBytes")
        self._content.setFont(tabular_font())
        row.addWidget(self._content, 1)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    def set_database(self, database: Database) -> None:
        self._database = database

    def clear(self) -> None:
        self._content.setText("—")

    def update_for(self, tx: TxMessage | None) -> None:
        if tx is None or self._database is None:
            self.clear()
            return
        decoder = self._database.decoder_for_bus(tx.bus_name)
        if decoder is None:
            self._content.setText("(no DBC bound)")
            return
        try:
            msg_obj = decoder.database.get_message_by_frame_id(tx.message_id)
        except KeyError:
            self._content.setText("(message removed from DBC)")
            return
        try:
            data = encode_tx_message(decoder.database, msg_obj, tx.signal_values)
        except Exception as exc:  # pragma: no cover — defensive
            self._content.setText(f"(encode error: {exc})")
            return
        hex_bytes = " ".join(f"{b:02X}" for b in data)
        self._content.setText(f"0x{tx.message_id:X}  ·  {len(data)} B  ·  {hex_bytes}")

    def _restyle(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        self.setStyleSheet(
            f"""
            QFrame#TxPayloadStrip {{
                background: {tokens["bg_1"]};
                border-top: 1px solid {tokens["border_0"]};
            }}
            QLabel#TxPayloadCaption {{
                color: {tokens["fg_3"]};
                letter-spacing: 1px;
            }}
            QLabel#TxPayloadBytes {{
                color: {tokens["fg_1"]};
            }}
            """
        )


__all__ = ["TxPayloadPreview"]
