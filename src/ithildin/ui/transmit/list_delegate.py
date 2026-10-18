# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Custom list-row delegate for the TX window's transmits list.

Renders the two-line row described in the handoff §5:

* Tri-state run-state button on the left (transmitting / armed / stopped).
* Message name + hex ID in mono.
* Period badge on the right (transmitting variant blinks at ~1 Hz).
* Signal-summary second line (ellipsised).

The delegate also intercepts clicks inside the run-state button so it
toggles ``tx.active`` without selecting the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from ithildin.core.transmit import TxMessage
from ithildin.ui.fonts import tabular_font, ui_font
from ithildin.ui.theme import ThemeManager

if TYPE_CHECKING:
    pass


TX_ROW_ROLE = Qt.ItemDataRole.UserRole + 1
"""Per-item role: stash the :class:`TxMessage` reference on the model item."""


_ROW_HEIGHT = 46
_BTN_SIZE = 22
_BTN_LEFT = 8
_BADGE_RIGHT_MARGIN = 10
_BADGE_PAD_X = 6
_BADGE_HEIGHT = 14


class TxListDelegate(QStyledItemDelegate):
    """Two-line painted row with a tri-state run-state button + period badge.

    The owner widget should:
      * call :meth:`set_master_enabled` whenever the TX-master state flips
        so the delegate paints the correct armed / transmitting variant;
      * connect :attr:`active_toggled` to flip the row's ``active`` flag
        without triggering selection.
    """

    active_toggled = Signal(int)  # row index in the model

    def __init__(self, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self._master_enabled = False
        self._blink_on = True

    def set_master_enabled(self, enabled: bool) -> None:
        if enabled == self._master_enabled:
            return
        self._master_enabled = enabled
        # The view repaints on its own thanks to the model's data-changed
        # nudges from the window; nothing to do here beyond storing state.

    def blink_tick(self) -> None:
        """Toggle the blink phase. View should call viewport().update()."""
        self._blink_on = not self._blink_on

    # ------------------------------------------------------------------
    # Size / paint
    # ------------------------------------------------------------------

    def sizeHint(
        self,
        _option: QStyleOptionViewItem,
        _index: QModelIndex | QPersistentModelIndex,
    ) -> Any:
        from PySide6.QtCore import QSize

        return QSize(220, _ROW_HEIGHT)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        tokens = ThemeManager.instance().current_tokens()
        tx = index.data(TX_ROW_ROLE)
        if not isinstance(tx, TxMessage):
            super().paint(painter, option, index)
            return

        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        transmitting = self._master_enabled and tx.active
        armed = (not self._master_enabled) and tx.active

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Background + selection left border.
        bg = QColor(tokens["bg_selected"]) if selected else QColor(0, 0, 0, 0)
        painter.fillRect(rect, bg)
        if selected:
            accent_strip = QRect(rect.left(), rect.top(), 2, rect.height())
            painter.fillRect(accent_strip, QColor(tokens["accent"]))

        # Bottom hairline divider so rows read as a stack.
        painter.setPen(QPen(QColor(tokens["border_0"]), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        # Run-state button on the left.
        btn_rect = self._button_rect(rect)
        if transmitting:
            border = QColor(tokens["ok"])
            fill = QColor(tokens["ok"])
            fill.setAlphaF(0.18)
            glyph_color = QColor(tokens["ok"])
            glyph = "■"
        elif armed:
            border = QColor(tokens["warn"])
            fill = QColor(tokens["warn"])
            fill.setAlphaF(0.18)
            glyph_color = QColor(tokens["warn"])
            glyph = "■"
        else:
            border = QColor(tokens["border_1"])
            fill = QColor(0, 0, 0, 0)
            glyph_color = QColor(tokens["fg_3"])
            glyph = "▶"
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(btn_rect), 3, 3)
        painter.setPen(QPen(glyph_color))
        painter.setFont(_glyph_font())
        painter.drawText(btn_rect, int(Qt.AlignmentFlag.AlignCenter), glyph)

        # First text line: name + hex ID.
        text_x = btn_rect.right() + 10
        text_w = rect.right() - text_x - 8
        name_y = rect.top() + 6
        name_font = tabular_font()
        name_font.setPointSize(11)
        name_fm = QFontMetrics(name_font)
        painter.setFont(name_font)
        painter.setPen(QPen(QColor(tokens["fg_0"])))
        painter.drawText(
            text_x,
            name_y + name_fm.ascent(),
            f"{tx.message_name}",
        )
        name_w = name_fm.horizontalAdvance(tx.message_name)
        # Hex ID immediately after the name, dimmer.
        painter.setPen(QPen(QColor(tokens["fg_3"])))
        painter.drawText(
            text_x + name_w + 8,
            name_y + name_fm.ascent(),
            f"0x{tx.message_id:X}",
        )

        # Period badge on the far right of the first line.
        badge_text, badge_state = self._badge(tx, transmitting)
        if badge_text:
            badge_font = tabular_font()
            badge_font.setPointSize(8)
            badge_fm = QFontMetrics(badge_font)
            has_dot = transmitting and tx.mode == "periodic"
            extra = 12 if has_dot else 0
            badge_w = badge_fm.horizontalAdvance(badge_text) + _BADGE_PAD_X * 2 + extra
            badge_rect = QRect(
                rect.right() - _BADGE_RIGHT_MARGIN - badge_w,
                name_y + (name_fm.height() - _BADGE_HEIGHT) // 2,
                badge_w,
                _BADGE_HEIGHT,
            )
            self._paint_badge(painter, badge_rect, tokens, badge_state, badge_text, has_dot)

        # Second line: signal summary, ellipsised. Empty-overrides placeholder
        # paints dimmer than a real summary so it reads as metadata.
        summary = self._summary_for(tx)
        if summary:
            sum_font = ui_font()
            sum_font.setPointSize(9)
            sum_fm = QFontMetrics(sum_font)
            elide = sum_fm.elidedText(summary, Qt.TextElideMode.ElideRight, text_w)
            painter.setFont(sum_font)
            placeholder = not tx.signal_values
            painter.setPen(QPen(QColor(tokens["fg_3" if placeholder else "fg_2"])))
            painter.drawText(text_x, rect.bottom() - 8, elide)

        painter.restore()

    # ------------------------------------------------------------------
    # Click capture (run-state button)
    # ------------------------------------------------------------------

    def editorEvent(
        self,
        event: QEvent,
        _model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            btn_rect = self._button_rect(option.rect)
            if btn_rect.contains(event.position().toPoint()):
                self.active_toggled.emit(index.row())
                return True
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _button_rect(row_rect: QRect) -> QRect:
        cy = row_rect.top() + (row_rect.height() - _BTN_SIZE) // 2
        return QRect(row_rect.left() + _BTN_LEFT, cy, _BTN_SIZE, _BTN_SIZE)

    @staticmethod
    def _badge(tx: TxMessage, transmitting: bool) -> tuple[str, str]:
        if tx.mode == "one-shot":
            return "once", "neutral"
        if transmitting:
            return f"{tx.period_ms}ms", "ok"
        return f"{tx.period_ms}ms", "neutral"

    def _paint_badge(
        self,
        painter: QPainter,
        rect: QRect,
        tokens: dict[str, str],
        state: str,
        text: str,
        with_dot: bool,
    ) -> None:
        if state == "ok":
            border = QColor(tokens["ok"])
            fill = QColor(tokens["ok"])
            fill.setAlphaF(0.16)
            text_color = QColor(tokens["ok"])
        else:
            border = QColor(tokens["border_0"])
            fill = QColor(0, 0, 0, 0)
            text_color = QColor(tokens["fg_3"])
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(rect), 2, 2)
        text_x = rect.left() + _BADGE_PAD_X
        if with_dot:
            dot_d = 5
            dot_color = QColor(tokens["ok"])
            if not self._blink_on:
                dot_color.setAlphaF(0.25)
            painter.setBrush(dot_color)
            painter.setPen(Qt.PenStyle.NoPen)
            dot_center = QPoint(text_x + dot_d // 2, rect.center().y())
            painter.drawEllipse(dot_center, dot_d // 2, dot_d // 2)
            text_x += dot_d + 4
        badge_font = tabular_font()
        badge_font.setPointSize(8)
        painter.setFont(badge_font)
        painter.setPen(QPen(text_color))
        painter.drawText(
            QRect(text_x, rect.top(), rect.right() - text_x - 2, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )

    @staticmethod
    def _summary_for(tx: TxMessage) -> str:
        """Build the second-line summary; empty overrides → ``(defaults)``."""
        if not tx.signal_values:
            return "(defaults)"
        parts: list[str] = []
        for name, v in list(tx.signal_values.items())[:4]:
            if isinstance(v, bool):
                parts.append(f"{name}:{'ON' if v else 'OFF'}")
            elif isinstance(v, float):
                parts.append(f"{name}:{v:g}")
            else:
                parts.append(f"{name}:{v}")
        if len(tx.signal_values) > 4:
            parts.append("…")
        return " · ".join(parts)


def _glyph_font() -> QFont:
    f = QFont()
    f.setPointSize(8)
    f.setBold(True)
    return f


__all__ = ["TX_ROW_ROLE", "TxListDelegate"]
