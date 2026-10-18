# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tinted banner widget used for inline error/warn/info messages.

Three kinds: ``warn`` (amber), ``info`` (blue), ``error`` (red). Each
renders the same shape — left-side dot, message label, optional action
button, close ``×`` — and tints itself by setting an object name that
matches the QSS rules in ``ui.theme`` and toggling
``Qt.WA_StyledBackground`` so the stylesheet ``background:`` is honoured.

Banners are hidden by default; ``show_message(...)`` makes them visible
and resets the action button. The ``×`` close button hides the banner
and emits ``dismissed``; the action button (when configured) emits
``action_clicked`` and is the caller's responsibility to react to.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

from ithildin.ui.icons import themed_icon
from ithildin.ui.theme import ThemeManager

BannerKind = Literal["warn", "info", "error"]

_OBJECT_NAMES: dict[BannerKind, str] = {
    "warn": "WarnBanner",
    "info": "InfoBanner",
    "error": "ErrorBanner",
}

_DOT_TOKENS: dict[BannerKind, str] = {
    "warn": "warn",
    "info": "info",
    "error": "err",
}


class Banner(QWidget):
    """Inline message strip. Hidden by default."""

    dismissed = Signal()
    action_clicked = Signal()

    def __init__(self, kind: BannerKind = "warn", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind: BannerKind = kind
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName(_OBJECT_NAMES[kind])

        self._dot = QLabel("●", self)
        self._dot.setFixedWidth(14)
        self._label = QLabel(self)
        self._label.setObjectName("BannerLabel")
        self._label.setWordWrap(True)

        self._action_btn = QPushButton(self)
        self._action_btn.hide()
        self._action_btn.clicked.connect(self.action_clicked.emit)

        self._close_btn = QToolButton(self)
        themed_icon("x", self._close_btn.setIcon)
        self._close_btn.setAutoRaise(True)
        self._close_btn.setToolTip("Dismiss")
        self._close_btn.clicked.connect(self._on_dismiss)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 6, 6)
        layout.setSpacing(8)
        layout.addWidget(self._dot)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._action_btn)
        layout.addWidget(self._close_btn)

        self._apply_theme(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme)
        self.hide()

    @property
    def kind(self) -> BannerKind:
        return self._kind

    def set_kind(self, kind: BannerKind) -> None:
        if kind == self._kind:
            return
        self._kind = kind
        self.setObjectName(_OBJECT_NAMES[kind])
        # Changing objectName after first paint doesn't re-match the
        # application stylesheet's #WarnBanner / #InfoBanner / #ErrorBanner
        # rules against the widget. setStyleSheet(self.styleSheet()) is a
        # no-op because the widget's own QSS is empty — unpolish + polish
        # is the documented Qt incantation for forcing re-evaluation.
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()
        self._apply_theme(ThemeManager.instance().current_tokens())

    def show_message(
        self,
        text: str,
        *,
        kind: BannerKind | None = None,
        action_label: str | None = None,
    ) -> None:
        """Display ``text``; optionally show an action button.

        Pass ``kind`` to flip the banner's tint at the same time.
        ``action_label=None`` hides the action button; passing a non-empty
        string shows it. The caller listens to ``action_clicked`` for the
        button press.
        """
        if kind is not None and kind != self._kind:
            self.set_kind(kind)
        self._label.setText(text)
        if action_label:
            self._action_btn.setText(action_label)
            self._action_btn.show()
        else:
            self._action_btn.hide()
        self.show()

    def _on_dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()

    def _apply_theme(self, tokens: dict[str, str]) -> None:
        # Dot picks up its kind's status colour; the rest is governed by
        # the application stylesheet's #WarnBanner / #InfoBanner /
        # #ErrorBanner / #BannerLabel rules.
        token = _DOT_TOKENS[self._kind]
        self._dot.setStyleSheet(f"color: {tokens[token]}; background: transparent;")
        # The close button is now a themed SVG icon (auto-tinted, re-tinted
        # on theme switch); AutoRaise supplies the hover feedback.
        self._close_btn.setStyleSheet(
            "QToolButton { background: transparent; border: 0; padding: 0 4px; }"
        )
