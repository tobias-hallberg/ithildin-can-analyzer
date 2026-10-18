# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Animated pill switch — the app's standard on/off toggle.

A checkable ``QAbstractButton`` painted as a sliding pill. The thumb
animates between sides via a ``QPropertyAnimation`` on the ``thumb_pos``
Qt property. Built once here because it is reused across surfaces (the
Create CAN bus dialog, and the Transmit window later) — see handoff §8.

``danger=True`` swaps the on-state tint from ``accent`` to ``warn`` for
safety-critical toggles (listen-only).
"""

from __future__ import annotations

from PySide6.QtCore import Property, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QAbstractButton, QWidget

from ithildin.ui.theme import ThemeManager

_TRACK_W = 34
_TRACK_H = 18
_MARGIN = 2
_THUMB_D = _TRACK_H - 2 * _MARGIN
_TRAVEL = _TRACK_W - 2 * _MARGIN - _THUMB_D


class IthildinPillToggle(QAbstractButton):
    """A small animated switch. ``toggled(bool)`` is inherited from Qt."""

    def __init__(self, *, danger: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._danger = danger
        self._thumb_pos = 0.0  # 0.0 = off (left), 1.0 = on (right)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._anim = QPropertyAnimation(self, b"thumb_pos", self)
        self._anim.setDuration(120)
        self.toggled.connect(self._animate_to)
        # Bound method, not a lambda: Qt tracks the receiver and drops the
        # connection when this widget is destroyed. A lambda has no QObject
        # receiver, so it would keep firing into a deleted C++ object.
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    def sizeHint(self) -> QSize:
        return QSize(_TRACK_W, _TRACK_H)

    def _on_theme_changed(self, _tokens: dict[str, str]) -> None:
        self.update()

    def _animate_to(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._thumb_pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def get_thumb_pos(self) -> float:
        return self._thumb_pos

    def set_thumb_pos(self, value: float) -> None:
        self._thumb_pos = value
        self.update()

    thumb_pos = Property(float, get_thumb_pos, set_thumb_pos)

    def paintEvent(self, _event: QPaintEvent) -> None:
        tokens = ThemeManager.instance().current_tokens()
        on = self._thumb_pos > 0.5
        on_color = tokens["warn"] if self._danger else tokens["accent"]

        track = QColor(on_color if on else tokens["bg_3"])
        border = QColor(on_color if on else tokens["border_1"])
        thumb = QColor(tokens["bg_0"] if on else tokens["fg_2"])

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Centre the fixed-size track within whatever rect Qt gave us.
        ox = (self.width() - _TRACK_W) / 2
        oy = (self.height() - _TRACK_H) / 2

        painter.setPen(border)
        painter.setBrush(track)
        painter.drawRoundedRect(
            QRectF(ox + 0.5, oy + 0.5, _TRACK_W - 1, _TRACK_H - 1),
            _TRACK_H / 2,
            _TRACK_H / 2,
        )

        tx = ox + _MARGIN + self._thumb_pos * _TRAVEL
        ty = oy + _MARGIN
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(thumb)
        painter.drawEllipse(QRectF(tx, ty, _THUMB_D, _THUMB_D))
