# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Monitor child window — large-format colour-coded signal bars.

Built for at-a-distance reading while operating machinery. Each tracked
signal occupies one full-width horizontal bar with the signal name on
the left and the current value on the right, both in a large font. Each
bar is auto-assigned a background colour from a fixed palette so signals
can be told apart at a glance.

Drop target accepts ``application/x-ithildin-signal``. Decode runs per
``frames_received`` batch — filter-then-decode so the GUI thread stays
responsive.

A bar dims while its value is stale: its bus is closed, or no update came
for ``max(_STALE_MIN_S, _STALE_PERIODS × its period)``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ithildin.ui._child_base import ChildView
from ithildin.ui._drag_payload import MessagePayload, SignalPayload
from ithildin.ui._signal_drop import SignalDropArea
from ithildin.ui.fonts import tabular_font, ui_font
from ithildin.ui.icons import themed_callback
from ithildin.ui.theme import ThemeManager
from ithildin.ui.view_header import ViewHeaderBar

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.core.types import FrameRecord


# Saturated mid-tones for dark theme — vivid bars carry their colour identity
# from across the room.
_PALETTE_DARK: tuple[str, ...] = (
    "#c4302b",  # red
    "#1f6feb",  # blue
    "#2ea043",  # green
    "#d29922",  # amber
    "#a371f7",  # purple
    "#1abc9c",  # teal
    "#db61a2",  # pink
    "#f97316",  # orange
)
# Pastel siblings for light theme — same hue ordering so a colour-index
# saved on dark renders as a recognisable pastel after a theme switch.
_PALETTE_LIGHT: tuple[str, ...] = (
    "#f5b4b3",  # coral
    "#b3cdf5",  # sky
    "#b8dfbf",  # mint
    "#f0dba6",  # sand
    "#d9c9f8",  # lavender
    "#aedfd5",  # mint-teal
    "#f2c6dc",  # pink
    "#f9c89a",  # peach
)
_TEXT_ON_DARK_PALETTE = "#ffffff"
_TEXT_ON_LIGHT_PALETTE = "#1c2024"


def _is_light_theme(tokens: dict[str, str]) -> bool:
    return QColor(tokens["bg_0"]).lightness() > 128


def _bar_colors(idx: int, tokens: dict[str, str]) -> tuple[str, str]:
    """Return ``(background_hex, text_hex)`` for bar ``idx`` under ``tokens``."""
    if _is_light_theme(tokens):
        return _PALETTE_LIGHT[idx % len(_PALETTE_LIGHT)], _TEXT_ON_LIGHT_PALETTE
    return _PALETTE_DARK[idx % len(_PALETTE_DARK)], _TEXT_ON_DARK_PALETTE


_NAME_POINT_SIZE = 22
_VALUE_POINT_SIZE = 32
_UNIT_POINT_SIZE = 20
_BAR_MIN_HEIGHT = 72

_STALE_MIN_S = 2.0
_STALE_PERIODS = 3
_STALE_CHECK_MS = 1000
_STALE_OPACITY = 0.35


class _WrapAnywhereLabel(QLabel):
    """Right-aligned value label that wraps long strings onto extra lines.

    Stock ``QLabel.setWordWrap`` only breaks at word boundaries, so a
    value-table label like ``FaultDetected_VeryLongDescription`` (one
    unbroken token) would never wrap and would clip off the right edge.
    This paints with ``TextWrapAnywhere`` and reports its wrapped height
    so the parent bar grows to fit.

    Its ``minimumSizeHint`` width is 0 so the box layout can shrink it
    below the (huge, single-line) text width — that squeeze is what
    triggers wrapping. The name label is a plain ``QLabel`` whose minimum
    *is* its natural width and so won't shrink, so a long value yields to
    the name (wrapping) rather than pushing it off the row. A value that
    fits stays on one line at its natural width.
    """

    _FLAGS = (
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWrapAnywhere
    )

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        line = self.fontMetrics().height()
        if width <= 0 or not self.text():
            return line
        rect = self.fontMetrics().boundingRect(QRect(0, 0, width, 0), self._FLAGS, self.text())
        return max(line, rect.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height())

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)
        self.updateGeometry()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        painter.setFont(self.font())
        painter.drawText(self.rect(), self._FLAGS, self.text())


class _SignalBar(QFrame):
    """One coloured horizontal bar: signal name left, value + unit right."""

    def __init__(
        self,
        path: str,
        color_index: int,
        unit: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._color_index = color_index
        self.setMinimumHeight(_BAR_MIN_HEIGHT)
        sp = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setAutoFillBackground(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        name_font = ui_font(_NAME_POINT_SIZE)
        name_font.setBold(True)
        self.name_label = QLabel(path.split(".", 1)[-1] if "." in path else path, self)
        self.name_label.setFont(name_font)
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.name_label.setToolTip(path)
        layout.addWidget(self.name_label, 1)

        value_font = tabular_font(_VALUE_POINT_SIZE)
        value_font.setBold(True)
        self.value_label = _WrapAnywhereLabel("—", self)
        self.value_label.setFont(value_font)
        layout.addWidget(self.value_label, 0)

        unit_font = ui_font(_UNIT_POINT_SIZE)
        unit_font.setBold(True)
        self.unit_label = QLabel(unit, self)
        self.unit_label.setFont(unit_font)
        self.unit_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.unit_label.setVisible(bool(unit))
        layout.addWidget(self.unit_label, 0)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.apply_theme(ThemeManager.instance().current_tokens())

        self._stale_effect = QGraphicsOpacityEffect(self)
        self._stale_effect.setOpacity(_STALE_OPACITY)
        self._stale_effect.setEnabled(False)
        self.setGraphicsEffect(self._stale_effect)

    def apply_theme(self, tokens: dict[str, str]) -> None:
        bg_hex, fg_hex = _bar_colors(self._color_index, tokens)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(bg_hex))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(fg_hex))
        self.setPalette(pal)
        # value_label custom-paints from its inherited palette WindowText,
        # so it stays out of the stylesheet loop.
        for label in (self.name_label, self.unit_label):
            label.setStyleSheet(f"color: {fg_hex};")
        self.value_label.update()

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)

    def set_stale(self, age: float | None) -> None:
        """Dim the bar while its value is stale; ``age`` is the seconds since
        the last update, ``None`` once fresh."""
        self._stale_effect.setEnabled(age is not None)
        self.setToolTip("" if age is None else f"No update for {age:.0f} s")

    def path(self) -> str:
        return self._path


# Default decimals for an IEEE-754 float DBC signal — its encoding
# doesn't imply a precision, so we pick a sensible-for-vehicle-signals
# default. Integer-encoded signals derive their decimals from the DBC
# scale factor instead.
_FLOAT_SIGNAL_DECIMALS = 3


def _decimals_from_scale(scale: float) -> int:
    """How many fractional digits a DBC scale factor implies.

    ``0.1`` → 1, ``0.01`` → 2, ``0.125`` → 3, ``1`` → 0, ``2`` → 0.
    Uses :class:`Decimal` on the string form of the scale so binary-
    float artefacts (``0.1`` ≈ ``0.10000000000000001``) don't bleed
    into the count.
    """
    try:
        d = Decimal(str(abs(scale))).normalize()
    except (InvalidOperation, ValueError):
        return _FLOAT_SIGNAL_DECIMALS
    exp = d.as_tuple().exponent
    if not isinstance(exp, int):
        return _FLOAT_SIGNAL_DECIMALS
    return max(0, -exp)


@dataclass
class _Row:
    path: str
    message: str
    signal: str
    color_index: int
    bar: _SignalBar
    unit: str = ""
    # Fractional digits to render for ``last`` when it's a float.
    # Sourced from the DBC signal's scale (integer-encoded) or fixed
    # to ``_FLOAT_SIGNAL_DECIMALS`` for IEEE-754 float signals.
    decimals: int = 0
    last: int | float | str | None = None
    # Staleness bookkeeping, set on each value update: the frame's bus and
    # timestamp, the interval since the previous one, and the monotonic
    # time of the update.
    bus: str | None = None
    ts: float | None = None
    period: float | None = None
    updated: float | None = None
    stale: bool = False


class SignalMonitorWindow(ChildView):
    KIND = "signal_monitor"
    DEFAULT_SIZE = (520, 480)
    TITLE = "Monitor"

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, params=params)
        self._ctx: AppContext | None = None
        self._rows: dict[str, _Row] = {}
        self._next_color = 0
        self._build_body()
        themed_callback(self._refresh_bar_themes)

    def _refresh_bar_themes(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        for row in self._rows.values():
            row.bar.apply_theme(tokens)

    def _format_title(self, params: dict[str, object]) -> str:
        name = params.get("name")
        return f"Monitor · {name}" if name else "Monitor"

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        ctx.bus_reader.frames_received.connect(self._on_frames_received)
        ctx.database.database_changed.connect(self._refresh_origin_tooltips)
        self._refresh_origin_tooltips()
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(_STALE_CHECK_MS)
        self._stale_timer.timeout.connect(self._refresh_staleness)
        self._stale_timer.start()

    # ---- Public API ----

    def add_signal(self, path: str, color_index: int | None = None) -> None:
        if "." not in path or path in self._rows:
            return
        message, signal = path.split(".", 1)
        idx = color_index if color_index is not None else self._next_color
        if color_index is None:
            self._next_color += 1
        else:
            self._next_color = max(self._next_color, color_index + 1)
        unit, decimals = self._lookup_signal_meta(message, signal)
        bar = _SignalBar(path, idx, unit, self._content)
        bar.customContextMenuRequested.connect(lambda pos, p=path: self._show_bar_menu(p, pos))
        self._rows[path] = _Row(
            path=path,
            message=message,
            signal=signal,
            color_index=idx,
            bar=bar,
            unit=unit,
            decimals=decimals,
        )
        origin = self._origin_tooltip(message)
        if origin:
            bar.name_label.setToolTip(origin)
        # Insert before the trailing stretch / hint.
        insert_at = self._content_layout.count() - 2
        self._content_layout.insertWidget(max(0, insert_at), bar)
        self._update_hint()

    def _origin_tooltip(self, message: str) -> str:
        if self._ctx is None:
            return ""
        origin = self._ctx.database.origin_for_message(message)
        if origin is None:
            return ""
        buses, path = origin
        bus_label = ", ".join(buses) if buses else "—"
        return f"Bus: {bus_label} · DBC: {path.name}"

    def _refresh_origin_tooltips(self) -> None:
        for row in self._rows.values():
            tooltip = self._origin_tooltip(row.message) or row.path
            row.bar.name_label.setToolTip(tooltip)

    def add_message(self, message_name: str) -> None:
        """Add every signal of a message as a bar.

        Pre-capture path: when no bus is open, resolve the message via
        any loaded DBC.
        """
        if self._ctx is None:
            return
        bus = self._ctx.bus_reader.bus_name
        decoder = (
            self._ctx.database.decoder_for_bus(bus)
            if bus is not None
            else self._ctx.database.decoder_for_message(message_name)
        )
        if decoder is None:
            return
        try:
            msg = decoder.database.get_message_by_name(message_name)
        except KeyError:
            return
        for sig in msg.signals:
            self.add_signal(f"{message_name}.{sig.name}")

    def remove_signal(self, path: str) -> None:
        row = self._rows.pop(path, None)
        if row is None:
            return
        row.bar.setParent(None)
        row.bar.deleteLater()
        self._update_hint()

    def signal_paths(self) -> list[str]:
        return list(self._rows.keys())

    def save_state(self) -> dict[str, object]:
        state = super().save_state()
        state["signals"] = [
            {"path": row.path, "color": row.color_index} for row in self._rows.values()
        ]
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        super().restore_state(state)
        signals = state.get("signals")
        if not isinstance(signals, list):
            return
        for entry in signals:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            color = entry.get("color")
            if not isinstance(path, str) or not isinstance(color, int):
                continue
            self.add_signal(path, color_index=color)

    # ---- Build ----

    def _build_body(self) -> None:
        body = self
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = ViewHeaderBar(body, hide_when_idle=True)
        layout.addWidget(self.header)

        drop = SignalDropArea(body)
        drop.payload_dropped.connect(self._on_payload_dropped)
        layout.addWidget(drop, 1)

        scroll = QScrollArea(drop)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget(scroll)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(2)

        self._hint = QLabel("Drop signals here from the Database tree", self._content)
        self._hint.setFont(ui_font(18))
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet("color: palette(mid); padding: 32px;")
        self._content_layout.addWidget(self._hint)
        self._content_layout.addStretch(1)

        scroll.setWidget(self._content)
        drop.set_inner(scroll)

    def _update_hint(self) -> None:
        self._hint.setVisible(not self._rows)

    def _lookup_signal_meta(self, message: str, signal: str) -> tuple[str, int]:
        """Return ``(unit, decimals)`` for the named signal.

        ``decimals`` is derived from the DBC ``scale`` (e.g. 0.1 → 1
        decimal) for integer-encoded signals, or fixed to
        :data:`_FLOAT_SIGNAL_DECIMALS` for IEEE-754 float signals.
        Falls back to ``("", _FLOAT_SIGNAL_DECIMALS)`` if the DBC
        binding can't be resolved.
        """
        fallback = ("", _FLOAT_SIGNAL_DECIMALS)
        if self._ctx is None:
            return fallback
        decoder = self._ctx.database.decoder_for_message(message)
        if decoder is None:
            return fallback
        try:
            msg = decoder.database.get_message_by_name(message)
        except KeyError:
            return fallback
        for sig in msg.signals:
            if sig.name != signal:
                continue
            unit = getattr(sig, "unit", "") or ""
            if getattr(sig, "is_float", False):
                return (unit, _FLOAT_SIGNAL_DECIMALS)
            scale = float(getattr(sig, "scale", 1.0))
            return (unit, _decimals_from_scale(scale))
        return fallback

    def _show_bar_menu(self, path: str, pos: QPoint) -> None:
        row = self._rows.get(path)
        if row is None:
            return
        menu = QMenu(self)
        act = menu.addAction("Remove signal")
        chosen = menu.exec(row.bar.mapToGlobal(pos))
        if chosen is act:
            self.remove_signal(path)

    # ---- Drop / decode ----

    def _on_payload_dropped(self, payload: object) -> None:
        if isinstance(payload, SignalPayload):
            self.add_signal(payload.path())
        elif isinstance(payload, MessagePayload):
            self.add_message(payload.message)

    def _on_frames_received(self, batch: list[Any]) -> None:
        if self._ctx is None or not self._rows:
            return
        wanted_messages = {row.message for row in self._rows.values()}
        now = time.monotonic()
        for frame in batch:
            self._ingest_frame(frame, wanted_messages, now)
        self._render()
        self._refresh_staleness()

    def _ingest_frame(self, frame: FrameRecord, wanted_messages: set[str], now: float) -> None:
        if self._ctx is None:
            return
        decoder = self._ctx.database.decoder_for_bus(frame.bus_name)
        if decoder is None:
            return
        msg_name = decoder.message_name(frame.arbitration_id)
        if msg_name not in wanted_messages:
            return
        decoded = frame.decoded
        if decoded is None:
            decoded = decoder.decode(frame.arbitration_id, frame.data)
            frame.decoded = decoded
        if not decoded:
            return
        for row in self._rows.values():
            if row.message != msg_name:
                continue
            value = decoded.get(row.signal)
            if value is None:
                continue
            if row.ts is not None:
                row.period = frame.timestamp - row.ts
            row.ts = frame.timestamp
            row.bus = frame.bus_name
            row.updated = now
            if isinstance(value, bool):
                row.last = str(value)
            elif isinstance(value, int):
                # ``int`` first because ``bool`` is also ``int``; the
                # bool branch above already handled that. Preserve int
                # vs float so DBC integer signals (scale=1, offset=0)
                # render without a misleading ``.000`` suffix.
                row.last = value
            elif isinstance(value, float):
                row.last = value
            else:
                row.last = str(value)

    def _refresh_staleness(self) -> None:
        if self._ctx is None:
            return
        open_buses = set(self._ctx.bus_reader.bus_names())
        now = time.monotonic()
        for row in self._rows.values():
            if row.updated is None:
                continue
            age = now - row.updated
            stale = row.bus not in open_buses or age > max(
                _STALE_MIN_S, _STALE_PERIODS * (row.period or 0.0)
            )
            if stale or row.stale:
                row.bar.set_stale(age if stale else None)
            row.stale = stale

    def _render(self) -> None:
        for row in self._rows.values():
            if row.last is None:
                continue
            if isinstance(row.last, bool):
                # bool is a subclass of int; check first to avoid the
                # int branch coercing True/False to ``1``/``0``.
                row.bar.set_value(str(row.last))
            elif isinstance(row.last, int):
                row.bar.set_value(str(row.last))
            elif isinstance(row.last, float):
                # Fixed-decimal format so the rendered width is stable
                # — ``.4g`` previously dropped the decimal point when
                # the fractional part was zero, causing the cell text
                # to flicker width as the value crossed integer
                # boundaries. ``row.decimals`` comes from the signal's
                # DBC scale (e.g. 0.1 → 1 decimal) so the precision
                # matches what the encoding can actually represent.
                row.bar.set_value(f"{row.last:.{row.decimals}f}")
            else:
                row.bar.set_value(row.last)
