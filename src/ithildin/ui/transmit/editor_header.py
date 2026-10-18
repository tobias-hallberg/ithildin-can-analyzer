# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Editor-header widgets for the Transmit window.

Three small custom widgets that compose the header of the right editor
pane:

* :class:`ModeSegmented` — ``one-shot | periodic`` pill picker.
* :class:`PeriodField` — ``EVERY [50] ms`` numeric input with a zero-clamp.
* :class:`ActionButton` — themed send / start / stop button driven by
  the row's mode + active flag + master-on state.

Hoisted out of :mod:`ithildin.ui.transmit_window` so they're testable in
isolation and the window file stays under ~700 lines.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import themed_icon
from ithildin.ui.theme import ThemeManager

# Shared height for the three header controls so the mode picker, period
# field, and action button line up on one baseline.
_CONTROL_H = 28


class ModeSegmented(QFrame):
    """Tiny ``one-shot | periodic`` segmented control."""

    changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TxModeSeg")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(_CONTROL_H)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._buttons: dict[str, QPushButton] = {}
        for label in ("one-shot", "periodic"):
            btn = QPushButton(label, self)
            btn.setObjectName("TxModeBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, m=label: self._set(m))
            row.addWidget(btn)
            self._buttons[label] = btn
        self._current = "one-shot"
        self._buttons["one-shot"].setProperty("active", True)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    def set_mode(self, mode: str) -> None:
        if mode == self._current:
            return
        self._set(mode, emit=False)

    def _set(self, mode: str, *, emit: bool = True) -> None:
        self._current = mode
        for k, b in self._buttons.items():
            b.setProperty("active", k == mode)
            b.setChecked(k == mode)
            b.style().unpolish(b)
            b.style().polish(b)
        if emit:
            self.changed.emit(mode)

    def _restyle(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        self.setStyleSheet(
            f"""
            QFrame#TxModeSeg {{
                background: {tokens["bg_2"]};
                border: 1px solid {tokens["border_1"]};
                border-radius: 3px;
            }}
            QPushButton#TxModeBtn {{
                background: transparent;
                color: {tokens["fg_2"]};
                border: 0;
                border-right: 1px solid {tokens["border_0"]};
                padding: 4px 12px;
            }}
            QPushButton#TxModeBtn:last-child {{ border-right: 0; }}
            QPushButton#TxModeBtn[active="true"] {{
                background: {tokens["accent_bg"]};
                color: {tokens["accent"]};
            }}
            """
        )


class PeriodField(QFrame):
    """``EVERY  [   50] ms`` editable numeric field with zero-clamp."""

    changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TxPeriodField")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(_CONTROL_H)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(4)
        prefix = QLabel("EVERY", self)
        prefix.setObjectName("TxPeriodPrefix")
        prefix.setFont(tabular_font())
        row.addWidget(prefix)
        self._edit = QLineEdit("50", self)
        self._edit.setObjectName("TxPeriodEdit")
        self._edit.setFont(tabular_font())
        self._edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._edit.setMaxLength(5)
        self._edit.setFixedWidth(56)
        self._edit.setValidator(QIntValidator(1, 60000, self))
        self._edit.editingFinished.connect(self._emit)
        row.addWidget(self._edit)
        suffix = QLabel("ms", self)
        suffix.setObjectName("TxPeriodSuffix")
        suffix.setFont(tabular_font())
        row.addWidget(suffix)
        self._last_emitted = 50
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    def set_value(self, v: int) -> None:
        s = str(int(v))
        if s != self._edit.text():
            self._edit.setText(s)
        self._last_emitted = int(v)

    def _emit(self) -> None:
        try:
            v = int(self._edit.text())
        except ValueError:
            v = 0
        if v < 1:
            # QIntValidator rejects edits below 1 *while typing*, but
            # ``editingFinished`` still fires with the unclamped text on
            # focus-out — restore the last good value rather than emit a
            # zero-period reconcile that would make python-can spin.
            self._edit.setText(str(self._last_emitted))
            return
        self._last_emitted = v
        self.changed.emit(v)

    def _restyle(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        self.setStyleSheet(
            f"""
            QFrame#TxPeriodField {{
                background: {tokens["bg_2"]};
                border: 1px solid {tokens["border_1"]};
                border-radius: 3px;
            }}
            QFrame#TxPeriodField:disabled {{
                background: {tokens["bg_1"]};
                border-color: {tokens["border_0"]};
            }}
            QLabel#TxPeriodPrefix, QLabel#TxPeriodSuffix {{
                color: {tokens["fg_3"]};
                background: transparent;
                padding: 0 2px;
            }}
            QLineEdit#TxPeriodEdit {{
                color: {tokens["fg_0"]};
                background: transparent;
                border: 0;
                padding: 2px 2px;
            }}
            QLineEdit#TxPeriodEdit:disabled {{
                color: {tokens["fg_3"]};
            }}
            QLineEdit#TxPeriodEdit:focus {{
                border-bottom: 1px solid {tokens["accent"]};
            }}
            """
        )


class ActionButton(QWidget):
    """Send / start / stop button — styled by mode + active + master state."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._inner = QPushButton(self)
        self._inner.setObjectName("TxActionBtn")
        self._inner.setFixedHeight(_CONTROL_H)
        self._inner.setMinimumWidth(86)
        self._inner.setCursor(Qt.CursorShape.PointingHandCursor)
        self._inner.clicked.connect(self.clicked.emit)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._inner)
        self._mode = "one-shot"
        self._active = False
        self._master_on = False
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())
        self.set_state("one-shot", False, master_on=False)

    def set_state(self, mode: str, active: bool, *, master_on: bool) -> None:
        self._mode = mode
        self._active = active
        self._master_on = master_on
        if mode == "one-shot":
            label, icon, kind = "send", "send", "accent"
        elif active:
            label, icon, kind = "stop", "stop", "ok"
        else:
            label, icon, kind = "start", "play", "accent"
        if not master_on:
            kind = "disabled"
        self._inner.setText(label)
        # Themed icon so dark/light theme swap recolours via ThemeManager.
        themed_icon(icon, self._inner.setIcon)
        self._inner.setIconSize(QSize(12, 12))
        self._inner.setEnabled(master_on)
        self._inner.setProperty("kind", kind)
        self._inner.style().unpolish(self._inner)
        self._inner.style().polish(self._inner)

    def _restyle(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        self._inner.setStyleSheet(
            f"""
            QPushButton#TxActionBtn {{
                border-radius: 3px;
                padding: 0 14px;
                font-weight: 600;
                letter-spacing: 0.6px;
            }}
            QPushButton#TxActionBtn[kind="accent"] {{
                background: {tokens["accent_bg"]};
                border: 1px solid {tokens["accent_dim"]};
                color: {tokens["accent"]};
            }}
            QPushButton#TxActionBtn[kind="ok"] {{
                background: {tokens["ok"]};
                border: 1px solid {tokens["ok"]};
                color: {tokens["bg_0"]};
            }}
            QPushButton#TxActionBtn[kind="disabled"] {{
                background: {tokens["bg_2"]};
                border: 1px solid {tokens["border_1"]};
                color: {tokens["fg_3"]};
            }}
            """
        )


__all__ = ["ActionButton", "ModeSegmented", "PeriodField"]
