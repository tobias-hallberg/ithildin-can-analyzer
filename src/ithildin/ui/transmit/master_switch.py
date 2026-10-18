# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""TX-master pill switch — visual anchor of the Transmit window.

A single large pill: ``TX MASTER`` caption + animated thumb + ``ON AIR``
or ``OFF AIR`` state label. Click anywhere on the widget to toggle.
Painted entirely from theme tokens so light/dark stays coherent.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ithildin.ui.fonts import tabular_font
from ithildin.ui.theme import ThemeManager


class TxMasterPill(QWidget):
    """Large pill switch that flips the bus TX master gate.

    Emits :attr:`toggled` after a state change. The owner can also call
    :meth:`set_enabled_state` to flip without firing the signal — used
    for listen-only refusals that need to revert the visual without
    re-entering ``set_master`` on the scheduler.
    """

    toggled = Signal(bool)

    HEIGHT = 32
    PILL_W = 60
    PILL_H = 26
    CAPTION = "TX MASTER"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on = False
        self._thumb_anim_pos = 0.0  # 0.0 = off, 1.0 = on
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"thumbPos", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        ThemeManager.instance().theme_changed.connect(lambda _t: self.update())

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._on

    def set_enabled_state(self, on: bool, *, animate: bool = True) -> None:
        """Set ON/OFF without emitting :attr:`toggled`."""
        if on == self._on:
            return
        self._on = on
        target = 1.0 if on else 0.0
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._thumb_anim_pos)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._thumb_anim_pos = target
            self.update()

    # ------------------------------------------------------------------
    # Animation property
    # ------------------------------------------------------------------

    def _get_thumb_pos(self) -> float:
        return self._thumb_anim_pos

    def _set_thumb_pos(self, v: float) -> None:
        self._thumb_anim_pos = float(v)
        self.update()

    thumbPos = Property(float, _get_thumb_pos, _set_thumb_pos)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def sizeHint(self) -> Any:
        from PySide6.QtCore import QSize

        # Caption + pill + state label.
        cap_fm = QFontMetrics(_caption_font())
        st_fm = QFontMetrics(_state_font())
        w = (
            10
            + cap_fm.horizontalAdvance(self.CAPTION)
            + 14
            + self.PILL_W
            + 14
            + st_fm.horizontalAdvance("OFF AIR")
            + 12
        )
        return QSize(w, self.HEIGHT)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._on = not self._on
            target = 1.0 if self._on else 0.0
            self._anim.stop()
            self._anim.setStartValue(self._thumb_anim_pos)
            self._anim.setEndValue(target)
            self._anim.start()
            self.toggled.emit(self._on)
        super().mousePressEvent(ev)

    def paintEvent(self, _event: QPaintEvent) -> None:
        tokens = ThemeManager.instance().current_tokens()
        on = self._on
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Full-widget pill background (the click target). Tinted slightly
        # green when ON to reinforce the master state.
        bg_color = QColor(tokens["accent_bg"]) if on else QColor(tokens["bg_2"])
        if not self.isEnabled():
            bg_color = QColor(tokens["bg_1"])
        border_color = QColor(tokens["ok"]) if on else QColor(tokens["border_1"])
        p.setBrush(QBrush(bg_color))
        p.setPen(QPen(border_color, 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 5, 5)

        # Caption.
        x = 12.0
        p.setFont(_caption_font())
        cap_color = QColor(tokens["ok"]) if on else QColor(tokens["fg_3"])
        if not self.isEnabled():
            cap_color = QColor(tokens["fg_3"])
        p.setPen(QPen(cap_color))
        cap_fm = QFontMetrics(_caption_font())
        cap_w = cap_fm.horizontalAdvance(self.CAPTION)
        p.drawText(
            QRectF(x, 0, cap_w, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.CAPTION,
        )
        x += cap_w + 14

        # Pill.
        pill_y = (self.height() - self.PILL_H) / 2
        pill_rect = QRectF(x, pill_y, self.PILL_W, self.PILL_H)
        pill_color = QColor(tokens["ok"]) if on else QColor(tokens["bg_3"])
        pill_border = QColor(tokens["ok"]) if on else QColor(tokens["border_1"])
        if not self.isEnabled():
            pill_color = QColor(tokens["bg_3"])
            pill_border = QColor(tokens["border_1"])
        p.setBrush(QBrush(pill_color))
        p.setPen(QPen(pill_border, 1))
        p.drawRoundedRect(pill_rect, self.PILL_H / 2, self.PILL_H / 2)

        # Thumb (animated).
        thumb_d = self.PILL_H - 8
        thumb_travel = self.PILL_W - thumb_d - 6
        thumb_x = pill_rect.left() + 3 + thumb_travel * self._thumb_anim_pos
        thumb_rect = QRectF(thumb_x, pill_rect.center().y() - thumb_d / 2, thumb_d, thumb_d)
        thumb_color = QColor(tokens["bg_0"]) if on else QColor(tokens["fg_2"])
        if not self.isEnabled():
            thumb_color = QColor(tokens["fg_3"])
        p.setBrush(QBrush(thumb_color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(thumb_rect)
        x = pill_rect.right() + 14

        # State label.
        p.setFont(_state_font())
        state_text = "ON AIR" if on else "OFF AIR"
        state_color = QColor(tokens["ok"]) if on else QColor(tokens["fg_2"])
        if not self.isEnabled():
            state_color = QColor(tokens["fg_3"])
        p.setPen(QPen(state_color))
        p.drawText(
            QRectF(x, 0, self.width() - x - 8, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            state_text,
        )


def _caption_font() -> QFont:
    f = tabular_font()
    f.setPointSize(9)
    f.setBold(True)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
    return f


def _state_font() -> QFont:
    f = tabular_font()
    f.setPointSize(10)
    f.setBold(True)
    return f


__all__ = ["TxMasterPill"]
