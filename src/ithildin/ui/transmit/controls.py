# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Per-signal-type editor controls for the TX window.

Each control:

* Is a single :class:`QWidget` subclass that emits ``value_changed(...)``
  whenever the user edits the value.
* Exposes a ``value`` property for read/write access — the window calls
  ``set_value`` on theme/restore and ``value`` after a user edit.
* Re-styles itself in response to :meth:`ThemeManager.theme_changed` so
  light/dark swaps stay coherent without per-widget object names.

The :func:`control_for_signal` factory dispatches on
:class:`TxSignalKind` per the handoff §7 table.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.transmit import TxSignalKind, TxSignalSpec
from ithildin.ui.fonts import tabular_font
from ithildin.ui.theme import ThemeManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tokens() -> dict[str, str]:
    return ThemeManager.instance().current_tokens()


def _readout_pill(text: str, parent: QWidget | None = None) -> QLabel:
    """Right-aligned mono numeric readout used by sliders and steppers."""
    lbl = QLabel(text, parent)
    lbl.setFont(tabular_font())
    lbl.setMinimumWidth(82)
    lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lbl.setContentsMargins(8, 2, 8, 2)
    lbl.setObjectName("TxReadoutPill")
    return lbl


def _format_number(value: float, scale: float) -> str:
    """Render numeric value with a decimal count derived from ``scale``."""
    if scale >= 1.0 and scale == int(scale):
        return f"{value:.0f}"
    if scale >= 0.1:
        return f"{value:.1f}"
    if scale >= 0.01:
        return f"{value:.2f}"
    return f"{value:.3f}"


# ---------------------------------------------------------------------------
# Bool — pill toggle switch
# ---------------------------------------------------------------------------


class TxToggle(QAbstractButton):
    """Pill switch + ON/OFF label."""

    value_changed = Signal(bool)

    _PILL_W = 44
    _PILL_H = 22

    def __init__(self, value: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(value))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(self._PILL_H + 4)
        self.toggled.connect(self.value_changed)
        self.toggled.connect(lambda _v: self.update())

    @property
    def value(self) -> bool:
        return self.isChecked()

    def set_value(self, v: bool) -> None:
        if v != self.isChecked():
            self.setChecked(bool(v))

    def sizeHint(self) -> Any:  # Pill width + gap + "OFF" label slot.
        fm = QFontMetrics(tabular_font())
        label_w = fm.horizontalAdvance("OFF") + 4
        return QRect(0, 0, self._PILL_W + 10 + label_w, self._PILL_H + 4).size()

    def paintEvent(self, _event: QPaintEvent) -> None:
        tokens = _tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        on = self.isChecked()
        # Pill background.
        pill_rect = QRectF(0, (self.height() - self._PILL_H) / 2, self._PILL_W, self._PILL_H)
        pill_color = QColor(tokens["accent"] if on else tokens["bg_3"])
        border = QColor(tokens["accent_dim"] if on else tokens["border_1"])
        p.setBrush(QBrush(pill_color))
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(pill_rect, self._PILL_H / 2, self._PILL_H / 2)
        # Thumb.
        thumb_d = self._PILL_H - 6
        thumb_x = pill_rect.left() + (self._PILL_W - thumb_d - 3) if on else pill_rect.left() + 3
        thumb_rect = QRectF(thumb_x, pill_rect.center().y() - thumb_d / 2, thumb_d, thumb_d)
        p.setBrush(QBrush(QColor(tokens["bg_0"] if on else tokens["fg_2"])))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(thumb_rect)
        # Label.
        p.setFont(tabular_font())
        p.setPen(QPen(QColor(tokens["accent"] if on else tokens["fg_3"])))
        label_rect = QRectF(
            pill_rect.right() + 8,
            0,
            self.width() - pill_rect.right() - 8,
            self.height(),
        )
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        p.drawText(label_rect, align, "ON" if on else "OFF")


# ---------------------------------------------------------------------------
# Segmented — ≤4 enum
# ---------------------------------------------------------------------------


class TxSegmented(QFrame):
    """Pill row with one button per enum value."""

    value_changed = Signal(int)

    def __init__(
        self,
        choices: dict[int, str],
        value: int | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._choices = dict(sorted(choices.items()))
        self._value = value if value in self._choices else next(iter(self._choices))
        self.setObjectName("TxSegmentedFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._buttons: dict[int, QPushButton] = {}
        for raw, label in self._choices.items():
            btn = QPushButton(label, self)
            btn.setObjectName("TxSegmentedButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("active", raw == self._value)
            btn.clicked.connect(lambda _c=False, k=raw: self._select(k))
            row.addWidget(btn)
            self._buttons[raw] = btn
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    @property
    def value(self) -> int:
        return int(self._value)

    def set_value(self, v: int) -> None:
        if v in self._choices and v != self._value:
            self._select(v, emit=False)

    def _select(self, raw: int, *, emit: bool = True) -> None:
        self._value = raw
        for k, btn in self._buttons.items():
            btn.setProperty("active", k == raw)
            btn.setChecked(k == raw)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if emit:
            self.value_changed.emit(raw)

    def _restyle(self) -> None:
        tokens = _tokens()
        self.setStyleSheet(
            f"""
            QFrame#TxSegmentedFrame {{
                background: {tokens["bg_2"]};
                border: 1px solid {tokens["border_1"]};
                border-radius: 3px;
            }}
            QPushButton#TxSegmentedButton {{
                background: transparent;
                color: {tokens["fg_2"]};
                border: 0;
                border-right: 1px solid {tokens["border_0"]};
                padding: 4px 10px;
            }}
            QPushButton#TxSegmentedButton:last-child {{ border-right: 0; }}
            QPushButton#TxSegmentedButton[active="true"] {{
                background: {tokens["accent_bg"]};
                color: {tokens["accent"]};
            }}
            """
        )


# ---------------------------------------------------------------------------
# Dropdown — >4 enum
# ---------------------------------------------------------------------------


class TxDropdown(QComboBox):
    """Themed combobox for many-value enums."""

    value_changed = Signal(int)

    def __init__(
        self,
        choices: dict[int, str],
        value: int | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFont(tabular_font())
        self._choices = dict(sorted(choices.items()))
        for raw, label in self._choices.items():
            self.addItem(f"{label}  ({raw})", int(raw))
        if value is not None:
            idx = self.findData(int(value))
            if idx >= 0:
                self.setCurrentIndex(idx)
        self.currentIndexChanged.connect(self._on_changed)

    @property
    def value(self) -> int:
        return int(self.currentData() or 0)

    def set_value(self, v: int) -> None:
        idx = self.findData(int(v))
        if idx >= 0 and idx != self.currentIndex():
            self.setCurrentIndex(idx)

    def _on_changed(self, _idx: int) -> None:
        self.value_changed.emit(self.value)


# ---------------------------------------------------------------------------
# Slider — small range
# ---------------------------------------------------------------------------


class TxSlider(QWidget):
    """Horizontal slider with a numeric readout pill on the right."""

    value_changed = Signal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        scale: float,
        unit: str,
        value: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._scale = max(scale, 1e-9)
        self._unit = unit
        self._min = minimum
        self._max = maximum
        # We work in integer raw units of `scale` so QSlider behaves.
        self._raw_min = round(minimum / self._scale)
        self._raw_max = round(maximum / self._scale)
        if self._raw_max <= self._raw_min:
            self._raw_max = self._raw_min + 1
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(self._raw_min, self._raw_max)
        self._slider.setValue(round(float(value) / self._scale))
        self._slider.setSingleStep(1)
        self._slider.setPageStep(max(1, (self._raw_max - self._raw_min) // 10))
        self._slider.valueChanged.connect(self._on_changed)
        row.addWidget(self._slider, 1)
        self._readout = _readout_pill(self._format_readout(value), self)
        row.addWidget(self._readout)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    @property
    def value(self) -> float:
        return self._slider.value() * self._scale

    def set_value(self, v: float) -> None:
        raw = round(float(v) / self._scale)
        if raw != self._slider.value():
            self._slider.setValue(raw)

    def _format_readout(self, v: float) -> str:
        unit = f" {self._unit}" if self._unit else ""
        return f"{_format_number(v, self._scale)}{unit}"

    def _on_changed(self, _raw: int) -> None:
        v = self.value
        self._readout.setText(self._format_readout(v))
        self.value_changed.emit(v)

    def _restyle(self) -> None:
        tokens = _tokens()
        self._slider.setStyleSheet(
            f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: {tokens["bg_3"]};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {tokens["accent"]};
                border-radius: 2px;
            }}
            QSlider::add-page:horizontal {{
                background: {tokens["bg_3"]};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 14px;
                height: 14px;
                margin: -6px 0;
                background: {tokens["fg_0"]};
                border: 1px solid {tokens["accent"]};
                border-radius: 7px;
            }}
            """
        )
        self._readout.setStyleSheet(
            f"""
            QLabel#TxReadoutPill {{
                background: {tokens["bg_2"]};
                border: 1px solid {tokens["border_1"]};
                border-radius: 3px;
                color: {tokens["fg_0"]};
            }}
            """
        )


# ---------------------------------------------------------------------------
# Stepper — large range
# ---------------------------------------------------------------------------


class TxStepper(QFrame):
    """−/+ buttons with a mono numeric readout and trailing range hint."""

    value_changed = Signal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        scale: float,
        unit: str,
        value: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("TxStepperFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._min = minimum
        self._max = maximum
        self._scale = max(scale, 1.0)  # large-range scale is whole-units
        self._unit = unit
        self._value = max(minimum, min(maximum, float(value)))

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._minus = QPushButton("−", self)
        self._minus.setObjectName("TxStepperBtn")
        self._minus.setFixedWidth(28)
        self._minus.clicked.connect(lambda: self._bump(-self._scale))
        row.addWidget(self._minus)
        self._readout = QLabel(self._format(), self)
        self._readout.setObjectName("TxStepperReadout")
        self._readout.setFont(tabular_font())
        self._readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._readout.setMinimumWidth(110)
        row.addWidget(self._readout, 1)
        self._plus = QPushButton("+", self)
        self._plus.setObjectName("TxStepperBtn")
        self._plus.setFixedWidth(28)
        self._plus.clicked.connect(lambda: self._bump(self._scale))
        row.addWidget(self._plus)
        self._range = QLabel(f"{minimum:g}…{maximum:g}", self)
        self._range.setObjectName("TxStepperRange")
        self._range.setFont(tabular_font())
        self._range.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._range.setMinimumWidth(80)
        row.addWidget(self._range)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    @property
    def value(self) -> float:
        return self._value

    def set_value(self, v: float) -> None:
        clamped = max(self._min, min(self._max, float(v)))
        if clamped != self._value:
            self._value = clamped
            self._readout.setText(self._format())

    def _bump(self, delta: float) -> None:
        new = max(self._min, min(self._max, self._value + delta))
        if new != self._value:
            self._value = new
            self._readout.setText(self._format())
            self.value_changed.emit(new)

    def _format(self) -> str:
        unit = f" {self._unit}" if self._unit else ""
        return f"{self._value:.0f}{unit}"

    def _restyle(self) -> None:
        tokens = _tokens()
        self.setStyleSheet(
            f"""
            QFrame#TxStepperFrame {{
                background: {tokens["bg_2"]};
                border: 1px solid {tokens["border_1"]};
                border-radius: 3px;
            }}
            QPushButton#TxStepperBtn {{
                background: transparent;
                color: {tokens["fg_2"]};
                border: 0;
                border-right: 1px solid {tokens["border_0"]};
                padding: 4px 0;
                font-weight: 600;
            }}
            QPushButton#TxStepperBtn:hover {{
                background: {tokens["bg_3"]};
            }}
            QLabel#TxStepperReadout {{
                color: {tokens["fg_0"]};
                background: transparent;
            }}
            QLabel#TxStepperRange {{
                color: {tokens["fg_3"]};
                background: transparent;
                border-left: 1px solid {tokens["border_0"]};
            }}
            """
        )


# ---------------------------------------------------------------------------
# Signed slider — centred on 0
# ---------------------------------------------------------------------------


class TxSignedSlider(QWidget):
    """Bidirectional slider with a zero detent; readout is signed."""

    value_changed = Signal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        scale: float,
        unit: str,
        value: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._scale = max(scale, 1e-9)
        self._unit = unit
        # Symmetric raw range around 0 so the visual centre matches the value 0.
        self._span = max(abs(minimum), abs(maximum))
        self._raw_min = -round(self._span / self._scale)
        self._raw_max = round(self._span / self._scale)
        if self._raw_max <= 0:
            self._raw_max = 1
            self._raw_min = -1
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._bar = _SignedBar(self._raw_min, self._raw_max, self)
        self._bar.set_value(round(float(value) / self._scale))
        self._bar.value_changed.connect(self._on_raw_changed)
        row.addWidget(self._bar, 1)
        self._readout = _readout_pill(self._format(value), self)
        row.addWidget(self._readout)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    @property
    def value(self) -> float:
        return self._bar.raw_value() * self._scale

    def set_value(self, v: float) -> None:
        raw = round(float(v) / self._scale)
        self._bar.set_value(raw)
        self._readout.setText(self._format(self.value))

    def _format(self, v: float) -> str:
        sign = "+" if v > 0 else ""
        unit = f" {self._unit}" if self._unit else ""
        return f"{sign}{_format_number(v, self._scale)}{unit}"

    def _on_raw_changed(self, _raw: int) -> None:
        v = self.value
        self._readout.setText(self._format(v))
        self.value_changed.emit(v)

    def _restyle(self) -> None:
        tokens = _tokens()
        self._readout.setStyleSheet(
            f"""
            QLabel#TxReadoutPill {{
                background: {tokens["bg_2"]};
                border: 1px solid {tokens["border_1"]};
                border-radius: 3px;
                color: {tokens["fg_0"]};
            }}
            """
        )
        self._bar.update()


class _SignedBar(QWidget):
    """The painted bar that hosts the signed slider's groove, fill and handle."""

    value_changed = Signal(int)

    def __init__(self, raw_min: int, raw_max: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._raw_min = raw_min
        self._raw_max = raw_max
        self._raw = 0
        self.setMinimumHeight(28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def raw_value(self) -> int:
        return self._raw

    def set_value(self, raw: int) -> None:
        raw = max(self._raw_min, min(self._raw_max, raw))
        if raw == self._raw:
            return
        self._raw = raw
        self.update()
        self.value_changed.emit(raw)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        self._handle_mouse(ev.position().x())

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if ev.buttons() & Qt.MouseButton.LeftButton:
            self._handle_mouse(ev.position().x())

    def _handle_mouse(self, x: float) -> None:
        w = max(1, self.width() - 2)
        frac = max(0.0, min(1.0, (x - 1) / w))
        span = self._raw_max - self._raw_min
        self.set_value(round(self._raw_min + frac * span))

    def paintEvent(self, _event: QPaintEvent) -> None:
        tokens = _tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = self.width()
        h = self.height()
        # Groove.
        groove_y = h / 2 - 2
        groove_h = 4
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(tokens["bg_3"]))
        p.drawRoundedRect(QRectF(0, groove_y, w, groove_h), 2, 2)
        # Zero detent.
        zero_x = w * (0 - self._raw_min) / max(1, self._raw_max - self._raw_min)
        p.setPen(QPen(QColor(tokens["border_2"]), 1))
        p.drawLine(QPoint(int(zero_x), int(h / 2 - 6)), QPoint(int(zero_x), int(h / 2 + 6)))
        # Fill from zero to handle.
        v_x = w * (self._raw - self._raw_min) / max(1, self._raw_max - self._raw_min)
        positive = self._raw >= 0
        fill_color = QColor(tokens["ok" if positive else "warn"])
        if positive:
            p.setBrush(fill_color)
            p.drawRoundedRect(QRectF(zero_x, groove_y, v_x - zero_x, groove_h), 2, 2)
        else:
            p.setBrush(fill_color)
            p.drawRoundedRect(QRectF(v_x, groove_y, zero_x - v_x, groove_h), 2, 2)
        # Handle.
        thumb = 12
        p.setBrush(QColor(tokens["fg_0"]))
        p.setPen(QPen(QColor(tokens["ok" if positive else "warn"]), 1))
        p.drawEllipse(QRectF(v_x - thumb / 2, h / 2 - thumb / 2, thumb, thumb))


# ---------------------------------------------------------------------------
# Hex grid — one cell per byte
# ---------------------------------------------------------------------------


class TxHexGrid(QWidget):
    """Row of mono ``HH`` cells. Editable; emits a hex string on change."""

    value_changed = Signal(str)

    def __init__(self, value: str, byte_count: int = 8, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        bytes_list = _parse_hex(value, byte_count)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._cells: list[QLineEdit] = []
        for b in bytes_list:
            cell = QLineEdit(b, self)
            cell.setFont(tabular_font())
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setMaxLength(2)
            cell.setFixedWidth(28)
            cell.setObjectName("TxHexCell")
            cell.editingFinished.connect(self._emit)
            row.addWidget(cell)
            self._cells.append(cell)
        row.addStretch(1)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    @property
    def value(self) -> str:
        return " ".join(c.text().upper().rjust(2, "0") for c in self._cells)

    def set_value(self, v: str) -> None:
        bytes_list = _parse_hex(v, len(self._cells))
        for cell, byte in zip(self._cells, bytes_list, strict=False):
            cell.setText(byte)

    def _emit(self) -> None:
        self.value_changed.emit(self.value)

    def _restyle(self) -> None:
        tokens = _tokens()
        self.setStyleSheet(
            f"""
            QLineEdit#TxHexCell {{
                background: {tokens["bg_2"]};
                color: {tokens["fg_0"]};
                border: 1px solid {tokens["border_1"]};
                border-radius: 2px;
                padding: 2px;
            }}
            QLineEdit#TxHexCell:focus {{ border-color: {tokens["accent"]}; }}
            """
        )


def _parse_hex(value: str, byte_count: int) -> list[str]:
    raw = "".join(ch for ch in (value or "") if ch in "0123456789ABCDEFabcdef")
    raw = raw.upper().ljust(byte_count * 2, "0")[: byte_count * 2]
    return [raw[i : i + 2] for i in range(0, byte_count * 2, 2)]


# ---------------------------------------------------------------------------
# Auto — read-only badge
# ---------------------------------------------------------------------------


class TxAutoBadge(QFrame):
    """Read-only display for engine-filled signals (counters, CRC)."""

    value_changed = Signal(int)  # never emits; kept for API parity

    def __init__(
        self,
        value: int | float,
        *,
        hex_format: bool = False,
        maximum: int | float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._hex = hex_format
        self._max = maximum
        self._value = value
        self.setObjectName("TxAutoFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._readout = QLabel(self._format(), self)
        self._readout.setFont(tabular_font())
        self._readout.setMinimumWidth(80)
        self._readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._readout.setObjectName("TxAutoReadout")
        row.addWidget(self._readout)
        tag = QLabel("auto", self)
        tag.setFont(tabular_font())
        tag.setObjectName("TxAutoTag")
        row.addWidget(tag)
        row.addStretch(1)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(lambda _t: self._restyle())

    @property
    def value(self) -> int | float:
        return self._value

    def set_value(self, v: int | float) -> None:
        if v == self._value:
            return
        self._value = v
        self._readout.setText(self._format())

    def _format(self) -> str:
        if self._hex:
            base = f"0x{int(self._value):02X}"
        else:
            base = str(self._value)
        return f"{base}" + (f" / {self._max}" if self._max else "")

    def _restyle(self) -> None:
        tokens = _tokens()
        self.setStyleSheet(
            f"""
            QLabel#TxAutoReadout {{
                background: {tokens["bg_2"]};
                border: 1px dashed {tokens["border_1"]};
                border-radius: 3px;
                color: {tokens["fg_2"]};
                padding: 2px 8px;
            }}
            QLabel#TxAutoTag {{
                background: transparent;
                border: 1px solid {tokens["border_0"]};
                border-radius: 2px;
                color: {tokens["fg_3"]};
                padding: 1px 4px;
            }}
            """
        )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def control_for_signal(
    spec: TxSignalSpec,
    *,
    on_change: Callable[[Any], None],
    parent: QWidget | None = None,
) -> QWidget:
    """Build a control widget for ``spec`` and wire its change signal."""
    kind: TxSignalKind = spec.kind
    if kind == "bool":
        w = TxToggle(bool(spec.value), parent=parent)
        w.value_changed.connect(lambda v: on_change(bool(v)))
        return w
    if kind in ("enum-seg", "enum-dd"):
        choices = spec.choices or {}
        value: int | None
        try:
            value = int(spec.value) if not isinstance(spec.value, str) else None
        except (TypeError, ValueError):
            value = None
        ctrl: QWidget = (
            TxSegmented(choices, value, parent=parent)
            if kind == "enum-seg"
            else TxDropdown(choices, value, parent=parent)
        )
        ctrl.value_changed.connect(lambda v: on_change(int(v)))  # type: ignore[attr-defined]
        return ctrl
    if kind == "hex":
        hex_w = TxHexGrid(str(spec.value or ""), parent=parent)
        hex_w.value_changed.connect(lambda v: on_change(str(v)))
        return hex_w
    if kind == "auto":
        return TxAutoBadge(
            int(spec.value) if not isinstance(spec.value, str) else 0,
            hex_format=False,
            maximum=int(spec.maximum) if spec.maximum is not None else None,
            parent=parent,
        )
    # Numeric kinds.
    mn = float(spec.minimum) if spec.minimum is not None else 0.0
    mx = float(spec.maximum) if spec.maximum is not None else 255.0
    scale = float(spec.scale or 1.0)
    val = float(spec.value) if isinstance(spec.value, (int, float, bool)) else mn
    if kind == "num-lg":
        stepper = TxStepper(mn, mx, scale, spec.unit, val, parent=parent)
        stepper.value_changed.connect(lambda v: on_change(float(v)))
        return stepper
    if kind == "signed":
        signed = TxSignedSlider(mn, mx, scale, spec.unit, val, parent=parent)
        signed.value_changed.connect(lambda v: on_change(float(v)))
        return signed
    slider = TxSlider(mn, mx, scale, spec.unit, val, parent=parent)
    slider.value_changed.connect(lambda v: on_change(float(v)))
    return slider


__all__ = [
    "TxAutoBadge",
    "TxDropdown",
    "TxHexGrid",
    "TxSegmented",
    "TxSignedSlider",
    "TxSlider",
    "TxStepper",
    "TxToggle",
    "control_for_signal",
]


# Silence unused imports that look unreferenced at the module level but
# are part of the public surface for callers (e.g. parent windows that
# import the QWidget bases for type checks).
_ = (QFont, QResizeEvent, QVBoxLayout)
