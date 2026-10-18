# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""About dialog — moonlit-orb easter egg + version + license.

The orb is a small painted widget: a soft radial gradient drawn with
``QPainter`` over a transparent canvas. It nods to the project's namesake
— *ithildin*, the moon-light alloy on the doors of Durin in *The Lord of
the Rings*: invisible by day, glowing under starlight or moonlight.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QRadialGradient
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ithildin import __version__
from ithildin.ui.theme import ThemeManager


class _MoonlitOrb(QWidget):
    """A soft glowing disc rendered via QPainter + QRadialGradient."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(120, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Bound method so the connection auto-drops on destruction (a lambda
        # would keep the app-lifetime ThemeManager firing into a dead widget).
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, _tokens: dict[str, str]) -> None:
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        tokens = ThemeManager.instance().current_tokens()
        accent = QColor(tokens["accent"])
        accent_dim = QColor(tokens["accent_dim"])

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        center = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) / 2

        gradient = QRadialGradient(center, radius)
        # Bright cyan core fading to translucent edge — the "ithildin glow".
        bright = QColor(accent.red(), accent.green(), accent.blue(), 230)
        mid = QColor(accent_dim.red(), accent_dim.green(), accent_dim.blue(), 150)
        edge = QColor(accent_dim.red(), accent_dim.green(), accent_dim.blue(), 0)
        gradient.setColorAt(0.0, bright)
        gradient.setColorAt(0.55, mid)
        gradient.setColorAt(1.0, edge)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QRectF(0, 0, self.width(), self.height()))


class AboutDialog(QDialog):
    DEFAULT_SIZE = (480, 420)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Ithildin")
        w, h = self.DEFAULT_SIZE
        self.resize(w, h)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Moonlit orb — the easter egg.
        orb = _MoonlitOrb(self)
        orb_wrap = QWidget(self)
        orb_layout = QVBoxLayout(orb_wrap)
        orb_layout.setContentsMargins(0, 0, 0, 0)
        orb_layout.addWidget(orb, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(orb_wrap)

        title = QLabel("<h2 style='margin:0;'>Ithildin</h2>", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        version = QLabel(f"Version {__version__}", self)
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version.setObjectName("AboutMeta")
        layout.addWidget(version)

        quote = QLabel(
            "<i>Ithildin: only star-light or moon-light reveals it,"
            " and only when read by one who knows.</i>",
            self,
        )
        quote.setAlignment(Qt.AlignmentFlag.AlignCenter)
        quote.setWordWrap(True)
        quote.setObjectName("AboutQuote")
        layout.addWidget(quote)

        body = QLabel(
            "Open-source CAN bus analyser with built-in MCP integration.<br>"
            'Released under <a href="https://www.gnu.org/licenses/gpl-3.0.html">'
            "GPL-3.0-or-later</a>.",
            self,
        )
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setObjectName("AboutMeta")
        layout.addWidget(body)

        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._apply_theme(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme)

    def _apply_theme(self, tokens: dict[str, str]) -> None:
        self.setStyleSheet(
            f"QLabel#AboutMeta {{ color: {tokens['fg_2']}; font-size: 9pt; }}"
            f"QLabel#AboutQuote {{ color: {tokens['fg_1']}; font-size: 10pt; }}"
        )
