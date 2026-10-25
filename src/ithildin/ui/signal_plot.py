# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Signal Plot child window — multi-instance, GPU-accelerated, stackable.

The window has two parallel vertical splitters inside a horizontal
splitter:

- Left column: a vertical stack of legend sections (one per row).
- Right column: a vertical stack of plot rows (top strip + GL canvas).

The two vertical splitters are kept in sync so each legend section
lines up with its plot row. The X (time) axis is shared across all
rows so events line up across panels.

Curves are draggable: grab a row in legend section A and drop it on
legend section B (or B's plot canvas) to move that signal between
sub-plots, preserving its sample history.

Backend: our own ``QOpenGLWidget``-based renderer (see ``_gl_plot.py``)
— replaces the previous QtGraphs wiring after we hit too many upstream
quirks.

Decode discipline (handoff §"Cross-window data"): on every
``frames_received`` batch, filter to the message IDs we care about
*first*, then decode only those frames. Decoding all frames every
tick is the obvious way to melt the GUI thread; we do not do that.

Samples outlive their bus: a bus that stops or degrades breaks its
curves' lines (a NaN marker) instead of clearing them, and only a new
capture session clears the plot. The X axis keeps moving while a bus is
open but silent.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from cantools.database.namedsignalvalue import NamedSignalValue
from PySide6.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QFont,
    QFontMetrics,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QColorDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from ithildin.core.types import BusHealth
from ithildin.ui._child_base import ChildView
from ithildin.ui._drag_payload import MessagePayload, SignalPayload, pack_into_mime
from ithildin.ui._gl_plot import GLPlotWidget
from ithildin.ui._signal_drop import SignalDropArea
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import icon, themed_callback, themed_icon
from ithildin.ui.theme import ThemeManager, pyqtgraph_palette
from ithildin.ui.view_header import ViewHeaderBar

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.core.types import FrameRecord


_DEFAULT_WINDOW_SECONDS = 12.0
_MIN_WINDOW_SECONDS = 0.1
_MAX_WINDOW_SECONDS = 3600.0
_DEFAULT_LEFT_PANE_WIDTH = 280
_MIN_LEFT_PANE_WIDTH = 200

_POINTS_PER_SECOND = 1000
"""Worst-case sample density assumed when sizing per-curve ring
buffers — 1 ms granularity is a generous upper bound for CAN signals."""

_STALL_S = 0.5
"""Without a new sample for this long while a bus is open, the X axis
moves on with wall-clock time."""

_MIN_POINTS_PER_CURVE = 1024
_MAX_POINTS_PER_CURVE_CAP = 200_000
"""Absolute ceiling on per-curve buffer length. Bounds worst-case memory
at long windows × high frame rates."""


def _maxlen_for_window(window_seconds: float) -> int:
    return max(
        _MIN_POINTS_PER_CURVE,
        min(_MAX_POINTS_PER_CURVE_CAP, int(window_seconds * _POINTS_PER_SECOND)),
    )


# Toolbar combobox presets — (label, seconds).
_WINDOW_PRESETS: tuple[tuple[str, float], ...] = (
    ("1 s", 1.0),
    ("5 s", 5.0),
    ("12 s", 12.0),
    ("30 s", 30.0),
    ("1 min", 60.0),
    ("5 min", 300.0),
    ("15 min", 900.0),
)


def _format_window_seconds(seconds: float) -> str:
    """Render a seconds value in the most natural unit for the combo."""
    for label, value in _WINDOW_PRESETS:
        if abs(value - seconds) < 1e-6:
            return label
    if seconds >= 60.0 and abs(seconds - round(seconds / 60.0) * 60.0) < 1e-6:
        return f"{round(seconds / 60.0)} min"
    if abs(seconds - round(seconds)) < 1e-6:
        return f"{round(seconds)} s"
    return f"{seconds:g} s"


def _parse_window_text(text: str) -> float | None:
    """Parse "30 s", "2 min", "0.5" → seconds. Returns None on garbage."""
    s = text.strip().lower()
    if not s:
        return None
    multiplier = 1.0
    for suffix, mult in (("min", 60.0), ("m", 60.0), ("s", 1.0)):
        if s.endswith(suffix):
            multiplier = mult
            s = s[: -len(suffix)].strip()
            break
    try:
        value = float(s) * multiplier
    except ValueError:
        return None
    if value <= 0 or value > _MAX_WINDOW_SECONDS:
        return None
    return max(value, _MIN_WINDOW_SECONDS)


_Y_HEADROOM = 0.1
"""When a new sample exceeds the current Y range we expand by this
fraction of the range so the line doesn't sit on the axis edge."""

_PATH_ROLE = Qt.ItemDataRole.UserRole


def _color_swatch(color: QColor, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


class _ColorBar(QAbstractButton):
    """A legend row's colour indicator; clicking it opens the colour menu.

    Paints a 3 px bar but also covers the gap up to the eye, so the
    click target isn't a pixel hunt. Widens on hover to show it's live.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor()
        self.setFixedSize(11, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Change colour")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        width = 5 if self.underMouse() else 3
        p.drawRoundedRect(QRectF(0, 0, width, self.height()), 1, 1)


class _SwatchButton(QAbstractButton):
    """One round colour choice in the colour menu; checked = current."""

    def __init__(self, color: QColor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self.setCheckable(True)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def color(self) -> QColor:
        return QColor(self._color)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        outer = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if self.isChecked() or self.underMouse():
            text = self.palette().color(QPalette.ColorRole.Text)
            p.setPen(QPen(text if self.isChecked() else self._color, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(outer)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(outer.adjusted(3.5, 3.5, -3.5, -3.5))


class _SignalCurve:
    """One subscribed signal: its time series + colour + DBC metadata.

    The actual rendered geometry lives in the ``GLPlotWidget`` keyed
    by path; this is the model side that supports the ingest, list
    pane, and per-curve Y-range tracking.
    """

    def __init__(
        self,
        path: str,
        color: str | QColor,
        maxlen: int = _MIN_POINTS_PER_CURVE,
    ) -> None:
        # path is "MessageName.SignalName".
        self.path = path
        self.message, self.signal = path.split(".", 1)
        self.times: deque[float] = deque(maxlen=maxlen)
        self.values: deque[float] = deque(maxlen=maxlen)
        # Populated when the signal is added / re-resolved after a
        # DBC change. ``None`` for plain numeric signals;
        # ``{int: name}`` for value-table enums.
        self.enum_choices: dict[int, str] | None = None
        # Pulled from ``signal.unit`` in the DBC; rendered at the top
        # of this curve's Y axis.
        self.unit: str = ""
        # CAN message arbitration ID for the "msg (0xID)" line in the
        # legend row. Resolved from the DBC at add time.
        self.arbitration_id: int | None = None
        # Per-curve Y range. Set by ingest as samples arrive (grows
        # only; the axis disappears when the curve is removed, so no
        # shrink path needed). ``None`` means "seed from the first
        # sample".
        self.y_min: float | None = None
        self.y_max: float | None = None
        # Visibility toggled via the eye icon in the legend pane.
        # Hidden curves stay in ``_curves`` (ingest keeps appending so
        # un-hiding instantly resumes), but the plot widget skips
        # them in rendering and the axis layout re-tiers around
        # what's visible.
        self.visible: bool = True
        self._color = QColor(color)
        # Set when the user picked the colour from the legend menu. Theme
        # refreshes re-cycle only the automatic colours.
        self.user_color: bool = False
        self.last_value: float | None = None
        # Bus of the newest sample; ``None`` once its line was broken.
        self.last_bus: str | None = None

    def color(self) -> QColor:
        return QColor(self._color)

    def set_color(self, color: str | QColor) -> None:
        self._color = QColor(color)


def _eye_pixmap(color: QColor, open_: bool, size: int = 14) -> QPixmap:
    """Render a small eye glyph (open or crossed-out) in ``color``."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color)
    pen.setWidthF(1.2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    margin = 2.0
    cx, cy = size / 2.0, size / 2.0
    path = QPainterPath()
    path.moveTo(margin, cy)
    path.quadTo(cx, margin, size - margin, cy)
    path.quadTo(cx, size - margin, margin, cy)
    p.drawPath(path)
    p.setBrush(color)
    p.drawEllipse(QPointF(cx, cy), 1.5, 1.5)
    if not open_:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(margin, size - margin), QPointF(size - margin, margin))
    p.end()
    return pm


def _x_pixmap(color: QColor, size: int = 10) -> QPixmap:
    """Small X glyph in ``color`` — used as the row's remove button."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(color)
    pen.setWidthF(1.2)
    p.setPen(pen)
    margin = 2.0
    p.drawLine(QPointF(margin, margin), QPointF(size - margin, size - margin))
    p.drawLine(QPointF(margin, size - margin), QPointF(size - margin, margin))
    p.end()
    return pm


def _format_y_label(value: float | None, unit: str) -> str:
    """Mirror of ``_gl_plot._format_y_tick`` for legend rows.

    Inlined to avoid pulling rendering internals into this module; the
    rules need to match so live values and active-range numbers read
    the same as the axis ticks.
    """
    if value is None:
        return "—"
    if value == 0:
        return "0"
    abs_v = abs(value)
    if unit == "rpm":
        return f"{value:.0f}"
    if abs_v >= 100:
        return f"{value:.0f}"
    if abs_v >= 10:
        return f"{value:.1f}"
    if abs_v >= 0.01:
        return f"{value:.2f}"
    return f"{value:.2e}"


class _ElidingLabel(QLabel):
    """QLabel that right-elides to its current width and surfaces the
    full text via tooltip. Used for the curve name and meta lines, which
    are routinely longer than the signals pane is wide. The Ignored
    horizontal size policy lets the surrounding layout shrink it (and
    hand the slack to the value) instead of clipping."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self._full, Qt.TextElideMode.ElideRight, self.width())
        # Only carry a tooltip when text is actually truncated, so short
        # names don't shadow the row-level tooltip.
        self.setToolTip(self._full if elided != self._full else "")
        super().setText(elided)


class _CurveRow(QFrame):
    """Per-curve legend row — colour bar, eye, name/value, msg+ID/range, X.

    Whole row is a clickable surface (toggles selection). Eye and X
    buttons swallow their own clicks so they don't trip the row
    selection. The row is also a drag source: pressing on the body and
    moving past the drag threshold starts a QDrag carrying one or more
    ``SignalPayload``s. When the row is part of a multi-selection in
    its owning pane, every selected row's payload rides along so a
    single drag gesture moves them all into a sibling sub-plot.
    Clicking the colour bar asks the owning sub-plot for the colour menu.
    """

    # Carries the active Qt.KeyboardModifier flags at click time so the
    # pane can implement plain / Ctrl / Shift / Ctrl+Shift semantics.
    clicked = Signal(object)
    eye_toggled = Signal(bool)  # new visibility state
    remove_clicked = Signal()
    color_menu_requested = Signal(QPoint)  # global position

    def __init__(
        self,
        curve: _SignalCurve,
        parent: QWidget | None = None,
        *,
        signal_centric: bool = True,
        tooltip: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("CurveRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._curve = curve
        self._signal_centric = signal_centric
        self._selected = False
        self._drag_press_pos: QPoint | None = None
        # Deferred-click slot: a plain press on an already-selected row
        # stashes its modifiers here instead of committing the click
        # immediately. The defer is cancelled when a drag actually
        # starts (the multi-selection rides along) and flushed on
        # release otherwise (the user just clicked — collapse to one).
        self._pending_click_mods: Qt.KeyboardModifier | None = None
        self.setToolTip(tooltip)

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 7, 10, 7)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(0)
        grid.setColumnStretch(1, 1)

        # Column 1 — colour bar + visibility eye. No spacing between them:
        # the bar's click target spans the gap up to the eye.
        lead = QHBoxLayout()
        lead.setContentsMargins(0, 0, 0, 0)
        lead.setSpacing(0)
        self._color_bar = _ColorBar(self)
        self._color_bar.clicked.connect(self._on_color_bar_clicked)
        lead.addWidget(self._color_bar)
        self._eye_btn = QToolButton(self)
        self._eye_btn.setAutoRaise(True)
        self._eye_btn.setIconSize(QSize(14, 14))
        self._eye_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._eye_btn.setToolTip("Hide / show this curve")
        self._eye_btn.clicked.connect(self._on_eye_clicked)
        lead.addWidget(self._eye_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addLayout(lead, 0, 0, 2, 1)

        # Column 2 — name + live value over an optional meta line. Wrapped
        # in a vertical stack with stretches so the block centres vertically
        # when the meta label is hidden (signal-centric DBC + no range).
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(6)
        self._name_label = _ElidingLabel(curve.signal, self)
        self._name_label.setObjectName("CurveRowName")
        self._value_label = QLabel("—", self)
        self._value_label.setObjectName("CurveRowValue")
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        name_row.addWidget(self._name_label, 1)
        name_row.addWidget(self._value_label, 0)

        self._meta_label = _ElidingLabel("", self)
        self._meta_label.setObjectName("CurveRowMeta")

        text_stack = QVBoxLayout()
        text_stack.setContentsMargins(0, 0, 0, 0)
        text_stack.setSpacing(0)
        text_stack.addStretch(1)
        text_stack.addLayout(name_row)
        text_stack.addWidget(self._meta_label)
        text_stack.addStretch(1)
        grid.addLayout(text_stack, 0, 1, 2, 1)

        # Column 3 — remove X.
        self._x_btn = QToolButton(self)
        self._x_btn.setAutoRaise(True)
        self._x_btn.setIconSize(QSize(10, 10))
        self._x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._x_btn.setToolTip("Remove from plot")
        self._x_btn.clicked.connect(self.remove_clicked.emit)
        grid.addWidget(self._x_btn, 0, 2, 2, 1, Qt.AlignmentFlag.AlignVCenter)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity)

        self._apply_styles()
        self.update_color(curve.color())
        self.update_meta(curve)
        self.update_visible(curve.visible)
        self.update_live(None)

    # ---- Public API ----

    def curve_path(self) -> str:
        return self._curve.path

    def curve(self) -> _SignalCurve:
        return self._curve

    def update_color(self, color: QColor) -> None:
        self._curve.set_color(color)
        self._color_bar.set_color(color)
        # Eye and X glyphs adopt a muted theme colour so they don't
        # compete with the coloured bar.
        muted = self.palette().color(self.foregroundRole())
        muted.setAlpha(160)
        self._eye_btn.setIcon(QIcon(_eye_pixmap(muted, self._curve.visible)))
        self._x_btn.setIcon(QIcon(_x_pixmap(muted)))

    def update_meta(
        self,
        curve: _SignalCurve,
        *,
        signal_centric: bool | None = None,
        tooltip: str | None = None,
    ) -> None:
        """Refresh the static (post-add) parts of the row."""
        self._curve = curve
        if signal_centric is not None:
            self._signal_centric = signal_centric
        if tooltip is not None:
            self.setToolTip(tooltip)
        self._name_label.setText(curve.signal)
        self._update_meta_text()

    def update_live(self, value: float | None) -> None:
        choices = self._curve.enum_choices
        if choices is not None:
            if value is None:
                self._value_label.setText("—")
                return
            key = round(value)
            self._value_label.setText(choices.get(key, str(key)))
            return
        unit = self._curve.unit
        formatted = _format_y_label(value, unit)
        if unit:
            self._value_label.setText(
                f"<span>{formatted}</span>"
                f"<span style='color:palette(mid); margin-left:3px;'> {unit}</span>"
            )
        else:
            self._value_label.setText(formatted)

    def update_visible(self, visible: bool) -> None:
        # Sync the eye glyph + dim the whole row when hidden.
        muted = self.palette().color(self.foregroundRole())
        muted.setAlpha(160)
        self._eye_btn.setIcon(QIcon(_eye_pixmap(muted, visible)))
        self._opacity.setOpacity(1.0 if visible else 0.45)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    # ---- Internal ----

    def _on_eye_clicked(self) -> None:
        new_visible = not self._curve.visible
        self.eye_toggled.emit(new_visible)

    def _update_meta_text(self) -> None:
        if self._signal_centric:
            id_part = ""
        elif self._curve.arbitration_id is not None:
            id_part = f"{self._curve.message} (0x{self._curve.arbitration_id:X})"
        else:
            id_part = self._curve.message
        self._meta_label.setText(id_part)
        self._meta_label.setVisible(bool(id_part))

    def _apply_styles(self) -> None:
        # Tabular font for value, mono for meta text.
        mono = tabular_font()
        mono.setPointSizeF(self.font().pointSizeF())
        self._name_label.setFont(mono)
        self._value_label.setFont(mono)
        meta_font = QFont(mono)
        meta_font.setPointSizeF(meta_font.pointSizeF() - 1.5)
        self._meta_label.setFont(meta_font)
        self.setStyleSheet(
            """
            QFrame#CurveRow { background: transparent; }
            QFrame#CurveRow:hover { background: palette(mid-light); }
            QFrame#CurveRow[selected="true"] { background: palette(highlight); }
            QLabel#CurveRowName { color: palette(text); }
            QLabel#CurveRowValue { color: palette(text); }
            QLabel#CurveRowMeta { color: palette(mid); }
            """
        )

    # ---- Mouse events: click + start drag ----

    def _on_color_bar_clicked(self) -> None:
        bar = self._color_bar
        self.color_menu_requested.emit(bar.mapToGlobal(QPoint(0, bar.height())))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_press_pos = event.pos()
            mods = event.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            pane = _pane_of_row(self)
            already_selected = pane is not None and self._curve.path in pane.selected_paths_set()
            if pane is None or ctrl or shift or not already_selected:
                self._pending_click_mods = None
                self.clicked.emit(mods)
            else:
                # Plain press on an already-selected row: defer commit
                # so a follow-up drag can pick up the multi-selection.
                self._pending_click_mods = mods
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        if self._drag_press_pos is None:
            super().mouseMoveEvent(event)
            return
        if (
            event.pos() - self._drag_press_pos
        ).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return
        self._pending_click_mods = None
        self._start_drag(self._drag_press_pos)
        self._drag_press_pos = None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._pending_click_mods is not None:
            mods = self._pending_click_mods
            self._pending_click_mods = None
            self.clicked.emit(mods)
        self._drag_press_pos = None
        super().mouseReleaseEvent(event)

    def _start_drag(self, hotspot: QPoint) -> None:
        own_payload = SignalPayload(
            message=self._curve.message,
            message_id=self._curve.arbitration_id or 0,
            signal=self._curve.signal,
            unit=self._curve.unit,
        )
        # When the row is part of the owning pane's selection, drag
        # every selected row at once; otherwise drag just this one
        # without disturbing the existing selection (matches
        # QListView behaviour for drag-of-unselected-item).
        pane = _pane_of_row(self)
        if pane is not None and own_payload.path() in pane.selected_paths_set():
            payloads = pane.selected_payloads()
            if not payloads:
                payloads = [own_payload]
        else:
            payloads = [own_payload]
        drag = QDrag(self)
        drag.setMimeData(pack_into_mime(list(payloads)))
        pm = self.grab()
        drag.setPixmap(pm)
        drag.setHotSpot(hotspot)
        drag.exec(
            Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
            Qt.DropAction.MoveAction,
        )


class _CurveListHeader(QWidget):
    """``Plotted signals · <visible>/<total>`` strip."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(26)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: palette(window); border-bottom: 1px solid palette(mid); ")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(6)

        title = QLabel("Plotted signals", self)
        title_font = QFont(self.font())
        title_font.setPointSizeF(9.0)
        title.setFont(title_font)
        title.setStyleSheet("color: palette(mid);")
        row.addWidget(title)

        self._count = QLabel("0/0", self)
        mono = tabular_font()
        mono.setPointSizeF(9.0)
        self._count.setFont(mono)
        self._count.setStyleSheet("color: palette(mid);")
        row.addWidget(self._count)
        row.addStretch(1)

    def set_count(self, visible: int, total: int) -> None:
        self._count.setText(f"{visible}/{total}")


def _pane_of_row(row: _CurveRow) -> _CurveListPane | None:
    """Walk parents until we find the ``_CurveListPane`` that owns this row."""
    p: QWidget | None = row.parentWidget()
    while p is not None:
        if isinstance(p, _CurveListPane):
            return p
        p = p.parentWidget()
    return None


def _subplot_of_row(row: _CurveRow) -> _SubPlot | None:
    """Walk parents until we find a ``_CurveListPane`` and return its
    owning ``_SubPlot``."""
    pane = _pane_of_row(row)
    return None if pane is None else pane.subplot()


class _SubPlotDropArea(SignalDropArea):
    """Drop area that distinguishes external drops (add a new curve)
    from drops originating in a ``_CurveRow`` (move those curves into
    the target sub-plot, preserving their sample history). The list
    of paths covers the multi-row case: if the dragged row is part of
    its pane's selection, every selected row moves at once."""

    move_requested = Signal(object, list)  # source _SubPlot, list[str]

    def __init__(self, subplot: _SubPlot, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._subplot = subplot

    def dropEvent(self, event: Any) -> None:
        src = event.source()
        if isinstance(src, _CurveRow):
            from_subplot = _subplot_of_row(src)
            if from_subplot is None:
                event.ignore()
                return
            if from_subplot is self._subplot:
                # Dropping on own section is a no-op.
                event.acceptProposedAction()
                return
            pane = _pane_of_row(src)
            src_path = src.curve_path()
            if pane is not None and src_path in pane.selected_paths_set():
                paths = pane.selected_paths()
                if not paths:
                    paths = [src_path]
            else:
                paths = [src_path]
            self.move_requested.emit(from_subplot, paths)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        super().dropEvent(event)


class _CurveRowsHost(QWidget):
    """Scroll viewport content; left-clicks on empty area clear selection."""

    def __init__(
        self,
        on_empty_click: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_empty_click = on_empty_click

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.childAt(event.pos()) is None:
            self._on_empty_click()
        super().mousePressEvent(event)


class _CurveListPane(QWidget):
    """One section of the unified left column — legend rows for a
    single sub-plot. Header strip · scrollable rich rows · footer.
    """

    selection_changed = Signal(object)  # frozenset[str]
    visibility_toggled = Signal(str, bool)
    remove_requested = Signal(str)
    color_menu_requested = Signal(str, QPoint)  # path, global position
    # Emitted when one or more _CurveRows from a sibling sub-plot are
    # dropped onto this section. The host moves the curves.
    move_requested = Signal(object, list)  # source _SubPlot, list[str]

    def __init__(
        self,
        on_payload_dropped: Callable[[object], None],
        subplot: _SubPlot,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._rows: dict[str, _CurveRow] = {}
        self._selected_paths: set[str] = set()
        # Anchor for Shift-range selection — the most recently
        # plain-or-Ctrl-clicked path, or None when nothing is anchored.
        self._anchor_path: str | None = None
        self._subplot = subplot

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = _CurveListHeader(self)
        layout.addWidget(self._header)

        # Drops on rows / empty state should register as either adds
        # (external SignalPayload) or moves (cross-section _CurveRow).
        self._drop = _SubPlotDropArea(subplot=subplot, parent=self)
        self._drop.payload_dropped.connect(on_payload_dropped)
        self._drop.move_requested.connect(self.move_requested)

        self._stack = QStackedWidget(self._drop)

        # Scrollable column of rows.
        self._scroll = QScrollArea(self._stack)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows_host = _CurveRowsHost(self._clear_selection, self._scroll)
        self._rows_layout = QVBoxLayout(rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(rows_host)
        self._stack.addWidget(self._scroll)

        self._empty_hint = QLabel(
            "Drop signals here from the Database tree",
            self._stack,
        )
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet("color: palette(mid); padding: 16px;")
        self._stack.addWidget(self._empty_hint)

        self._drop.set_inner(self._stack)
        layout.addWidget(self._drop, 1)

        self._update_stack()
        self._refresh_count()

    # ---- Public API ----

    def subplot(self) -> _SubPlot:
        return self._subplot

    def add_row(
        self,
        curve: _SignalCurve,
        *,
        signal_centric: bool = True,
        tooltip: str = "",
    ) -> None:
        if curve.path in self._rows:
            return
        row = _CurveRow(curve, self, signal_centric=signal_centric, tooltip=tooltip)
        row.clicked.connect(lambda mods, p=curve.path: self._on_row_clicked(p, mods))
        row.eye_toggled.connect(
            lambda visible, p=curve.path: self.visibility_toggled.emit(p, visible)
        )
        row.remove_clicked.connect(lambda p=curve.path: self.remove_requested.emit(p))
        row.color_menu_requested.connect(
            lambda pos, p=curve.path: self.color_menu_requested.emit(p, pos)
        )
        # Insert above the stretch.
        insert_at = self._rows_layout.count() - 1
        self._rows_layout.insertWidget(max(0, insert_at), row)
        self._rows[curve.path] = row
        self._update_stack()
        self._refresh_count()

    def remove_row(self, path: str) -> None:
        row = self._rows.pop(path, None)
        if row is None:
            return
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        changed = path in self._selected_paths
        self._selected_paths.discard(path)
        if self._anchor_path == path:
            self._anchor_path = None
        if changed:
            self.selection_changed.emit(frozenset(self._selected_paths))
        self._update_stack()
        self._refresh_count()

    def update_meta(
        self,
        curve: _SignalCurve,
        *,
        signal_centric: bool | None = None,
        tooltip: str | None = None,
    ) -> None:
        row = self._rows.get(curve.path)
        if row is not None:
            row.update_meta(curve, signal_centric=signal_centric, tooltip=tooltip)

    def update_live(self, path: str, value: float | None) -> None:
        row = self._rows.get(path)
        if row is not None:
            row.update_live(value)

    def set_visible(self, path: str, visible: bool) -> None:
        row = self._rows.get(path)
        if row is not None:
            row.update_visible(visible)
        self._refresh_count()

    def refresh_colors(self, color_for: Callable[[str], QColor | None]) -> None:
        for path, row in self._rows.items():
            color = color_for(path)
            if color is not None:
                row.update_color(color)

    def selected_paths(self) -> list[str]:
        """Selected paths in visual top-to-bottom order."""
        return [p for p in self._rows if p in self._selected_paths]

    def selected_paths_set(self) -> set[str]:
        return set(self._selected_paths)

    def selected_payloads(self) -> list[SignalPayload]:
        """``SignalPayload`` for each selected row, in visual order."""
        out: list[SignalPayload] = []
        for path in self.selected_paths():
            row = self._rows.get(path)
            if row is None:
                continue
            curve = row.curve()
            out.append(
                SignalPayload(
                    message=curve.message,
                    message_id=curve.arbitration_id or 0,
                    signal=curve.signal,
                    unit=curve.unit,
                )
            )
        return out

    # ---- Internal ----

    def _on_row_clicked(self, path: str, modifiers: Qt.KeyboardModifier) -> None:
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if shift:
            anchor = self._anchor_path if self._anchor_path in self._rows else path
            range_paths = self._paths_between(anchor, path)
            if ctrl:
                self._selected_paths.update(range_paths)
            else:
                self._selected_paths = set(range_paths)
                self._anchor_path = anchor
        elif ctrl:
            if path in self._selected_paths:
                self._selected_paths.discard(path)
            else:
                self._selected_paths.add(path)
            self._anchor_path = path
        else:
            if path in self._selected_paths:
                self._selected_paths.discard(path)
            else:
                self._selected_paths = {path}
            self._anchor_path = path
        self._repaint_selection()
        self.selection_changed.emit(frozenset(self._selected_paths))

    def _paths_between(self, a: str, b: str) -> list[str]:
        order = list(self._rows)
        try:
            i, j = order.index(a), order.index(b)
        except ValueError:
            return [b]
        lo, hi = (i, j) if i <= j else (j, i)
        return order[lo : hi + 1]

    def _repaint_selection(self) -> None:
        for p, row in self._rows.items():
            row.set_selected(p in self._selected_paths)

    def _clear_selection(self) -> None:
        if not self._selected_paths:
            return
        self._selected_paths.clear()
        self._anchor_path = None
        self._repaint_selection()
        self.selection_changed.emit(frozenset(self._selected_paths))

    def _refresh_count(self) -> None:
        total = len(self._rows)
        visible = sum(1 for row in self._rows.values() if row._curve.visible)
        self._header.set_count(visible, total)

    def _update_stack(self) -> None:
        empty = not self._rows
        self._stack.setCurrentWidget(self._empty_hint if empty else self._scroll)


class _SubPlot(QObject):
    """One row of a stacked plot — owns its legend section and plot row.

    Not a ``QWidget``: holds two sibling widgets (legend pane + plot
    row) that the host inserts into two parallel vertical splitters,
    plus the curve state shared between them.
    """

    remove_requested = Signal()
    # Inter-sub-plot move — emitted up to the host when one or more
    # _CurveRows from a different sub-plot are dropped into this one.
    move_in_requested = Signal(object, object, list)  # source, dest, list[str]

    def __init__(
        self,
        palette: dict[str, Any],
        can_remove: bool,
        window_seconds: float,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._ctx: AppContext | None = None
        self._curves: dict[str, _SignalCurve] = {}
        self._palette = palette
        self._window_seconds = window_seconds
        # Lazily-built per-bus set of arbitration IDs this sub-plot
        # cares about. Keeps the per-frame admission check to one set
        # membership test.
        self._wanted_aids: dict[str, set[int]] = {}

        # ---- Legend pane (lives in the host's left V-splitter) ----
        self._curve_list = _CurveListPane(
            on_payload_dropped=self._on_external_drop,
            subplot=self,
        )
        self._curve_list.remove_requested.connect(self.remove_signal)
        self._curve_list.visibility_toggled.connect(self._on_visibility_toggled)
        self._curve_list.selection_changed.connect(self._on_selection_changed)
        self._curve_list.move_requested.connect(self._on_drop_move_in)
        self._curve_list.color_menu_requested.connect(self._on_color_menu_requested)

        # ---- Plot row: just the GL canvas inside its drop area.
        # No top strip; the per-sub-plot remove (✕) sits as a small
        # overlay button in the canvas's own header strip — sharing
        # horizontal space with the Y-axis unit chips.
        self._plot_row = QWidget()
        row_layout = QVBoxLayout(self._plot_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)

        self._plot_drop = _SubPlotDropArea(subplot=self, parent=self._plot_row)
        self._plot_drop.payload_dropped.connect(self._on_external_drop)
        self._plot_drop.move_requested.connect(self._on_drop_move_in)
        row_layout.addWidget(self._plot_drop, 1)

        self._plot = GLPlotWidget(self._plot_drop)
        self._plot.set_x_range(0.0, self._window_seconds)
        self._plot.set_close_button_visible(can_remove)
        self._plot.close_clicked.connect(self.remove_requested)
        self._plot_drop.set_inner(self._plot)

        self._apply_theme_colors()
        themed_callback(self._refresh_plot_theme)

    # ---- Widget accessors for the host's splitters ----

    @property
    def legend_widget(self) -> _CurveListPane:
        return self._curve_list

    @property
    def plot_widget(self) -> QWidget:
        return self._plot_row

    # ---- Wiring from the host ----

    def bind_context(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def set_can_remove(self, can_remove: bool) -> None:
        self._plot.set_close_button_visible(can_remove)

    def teardown(self) -> None:
        """Drop the legend + plot widgets when this sub-plot is removed."""
        self._curve_list.setParent(None)
        self._curve_list.deleteLater()
        self._plot_row.setParent(None)
        self._plot_row.deleteLater()

    # ---- Subscription set ----

    def add_signal(self, path: str) -> None:
        """Add ``MessageName.SignalName`` as a curve. Idempotent."""
        if "." not in path or path in self._curves:
            return
        color = self._next_color()
        curve = _SignalCurve(path, color, maxlen=_maxlen_for_window(self._window_seconds))
        curve.enum_choices, curve.unit, curve.arbitration_id = self._lookup_signal_meta(
            curve.message, curve.signal
        )
        self._curves[path] = curve
        self._plot.add_curve(path, curve.color())
        self._plot.set_curve_unit(path, curve.unit)
        self._apply_curve_y_axis(curve)
        self.invalidate_wanted_aids()
        sc, tt = self._display_extras(curve.message)
        self._curve_list.add_row(curve, signal_centric=sc, tooltip=tt)

    def add_message(self, message_name: str) -> None:
        """Add every signal in a message as a curve."""
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
        curve = self._curves.pop(path, None)
        if curve is None:
            return
        # Removing a curve takes its axis with it. Surviving curves
        # keep their own ranges — no global rescale needed.
        self._plot.remove_curve(path)
        self._curve_list.remove_row(path)
        self.invalidate_wanted_aids()

    def signal_paths(self) -> list[str]:
        return list(self._curves.keys())

    def release_curve(self, path: str) -> _SignalCurve | None:
        """Detach a curve from this sub-plot, returning its state.

        The host hands the returned ``_SignalCurve`` to another
        sub-plot's ``receive_curve`` to move the signal across the
        stack while preserving its sample history.
        """
        curve = self._curves.pop(path, None)
        if curve is None:
            return None
        self._plot.remove_curve(path)
        self._curve_list.remove_row(path)
        self.invalidate_wanted_aids()
        return curve

    def receive_curve(self, curve: _SignalCurve) -> None:
        """Adopt a curve that was just released by another sub-plot.

        Sample history (``times`` / ``values`` deques) is preserved
        and re-fed into the GL canvas, so the user sees the same
        line move to the new panel.
        """
        if curve.path in self._curves:
            return
        # Keep the curve's existing colour — it's the same data, just
        # in a different sub-plot, and the colour is the user's anchor.
        self._curves[curve.path] = curve
        self._plot.add_curve(curve.path, curve.color())
        self._plot.set_curve_unit(curve.path, curve.unit)
        self._plot.set_curve_visible(curve.path, curve.visible)
        self._apply_curve_y_axis(curve)
        for t, val in zip(curve.times, curve.values, strict=False):
            self._plot.append_point(curve.path, t, val)
        self.invalidate_wanted_aids()
        sc, tt = self._display_extras(curve.message)
        self._curve_list.add_row(curve, signal_centric=sc, tooltip=tt)
        if not curve.visible:
            self._curve_list.set_visible(curve.path, False)

    def _on_external_drop(self, payload: object) -> None:
        if isinstance(payload, SignalPayload):
            self.add_signal(payload.path())
        elif isinstance(payload, MessagePayload):
            self.add_message(payload.message)

    def _on_drop_move_in(self, source_subplot: object, paths: list[str]) -> None:
        if not isinstance(source_subplot, _SubPlot):
            return
        if source_subplot is self:
            return
        if not paths:
            return
        self.move_in_requested.emit(source_subplot, self, paths)

    def _on_visibility_toggled(self, path: str, visible: bool) -> None:
        curve = self._curves.get(path)
        if curve is None:
            return
        curve.visible = visible
        self._plot.set_curve_visible(path, visible)
        self._curve_list.set_visible(path, visible)

    def _on_selection_changed(self, paths: object) -> None:
        # Selection only affects row highlight today; plot emphasis
        # (dim non-selected curves) is a deferred polish item.
        del paths

    # ---- Curve colour ----

    def set_curve_color(self, path: str, color: QColor | None) -> None:
        """Pin ``path`` to ``color``; ``None`` hands it back to the theme cycle."""
        curve = self._curves.get(path)
        if curve is None:
            return
        curve.user_color = color is not None
        if color is None:
            cycle = self._palette["curves"]
            assert isinstance(cycle, list)
            color = QColor(str(cycle[list(self._curves).index(path) % len(cycle)]))
        curve.set_color(color)
        self._plot.set_curve_color(path, color)
        self._curve_list.refresh_colors(self._color_for_path)

    def _on_color_menu_requested(self, path: str, pos: QPoint) -> None:
        menu = self._build_color_menu(path)
        if menu is None:
            return
        menu.exec(pos)
        menu.deleteLater()

    def _build_color_menu(self, path: str) -> QMenu | None:
        curve = self._curves.get(path)
        if curve is None:
            return None
        cycle = self._palette["curves"]
        assert isinstance(cycle, list)
        menu = QMenu(self._curve_list)
        row = QWidget(menu)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 6, 8, 6)
        row_layout.setSpacing(2)
        current = curve.color()
        for entry in cycle:
            color = QColor(str(entry))
            swatch = _SwatchButton(color, row)
            swatch.setChecked(curve.user_color and color == current)
            swatch.clicked.connect(lambda _checked=False, c=color: self.set_curve_color(path, c))
            swatch.clicked.connect(menu.close)
            row_layout.addWidget(swatch)
        swatches = QWidgetAction(menu)
        swatches.setDefaultWidget(row)
        menu.addAction(swatches)
        menu.addSeparator()
        custom = menu.addAction("Custom colour…")
        custom.triggered.connect(lambda: self._pick_custom_color(path))
        auto = menu.addAction("Automatic colour")
        auto.setCheckable(True)
        auto.setChecked(not curve.user_color)
        auto.triggered.connect(lambda: self.set_curve_color(path, None))
        return menu

    def _pick_custom_color(self, path: str) -> None:
        curve = self._curves.get(path)
        if curve is None:
            return
        color = QColorDialog.getColor(curve.color(), self._curve_list, "Curve colour")
        if color.isValid():
            self.set_curve_color(path, color)

    def refresh_legend_live(self) -> None:
        for path, curve in self._curves.items():
            self._curve_list.update_live(path, curve.last_value)

    # ---- Ingest ----

    def ingest_frame(self, frame: FrameRecord) -> None:
        if self._ctx is None or not self._curves:
            return
        aids = self._wanted_aids_for_bus(frame.bus_name)
        if frame.arbitration_id not in aids:
            return
        decoder = self._ctx.database.decoder_for_bus(frame.bus_name)
        if decoder is None:
            return
        decoded = frame.decoded
        if decoded is None:
            decoded = decoder.decode(frame.arbitration_id, frame.data)
            frame.decoded = decoded
        if not decoded:
            return
        msg_name = decoder.message_name(frame.arbitration_id)
        t0 = self._ctx.bus_reader.session_t0
        if t0 is None:
            return
        for curve in self._curves.values():
            if curve.message != msg_name:
                continue
            value = decoded.get(curve.signal)
            if value is None:
                continue
            # Value-table signals decode to ``NamedSignalValue``;
            # ``.value`` gives the underlying raw int. Trace/Data/
            # Monitor use ``str(value)`` so they keep the named form.
            if isinstance(value, NamedSignalValue):
                value = value.value
            try:
                fvalue = float(value)
            except (TypeError, ValueError):
                continue
            curve.last_value = fvalue
            curve.last_bus = frame.bus_name
            curve.times.append(frame.timestamp - t0)
            curve.values.append(fvalue)
            self._plot.append_point(curve.path, frame.timestamp - t0, fvalue)
            self._maybe_expand_curve_y(curve, fvalue)

    def break_bus(self, bus_name: str) -> None:
        """Break the line of every curve last fed by ``bus_name``.

        The NaN marker stays in the deques, so the gap survives a move to
        another sub-plot; it never touches ``last_value``.
        """
        for curve in self._curves.values():
            if curve.last_bus != bus_name or not curve.times:
                continue
            curve.last_bus = None
            t = curve.times[-1]
            curve.times.append(t)
            curve.values.append(math.nan)
            self._plot.append_point(curve.path, t, math.nan)

    def latest_time(self) -> float | None:
        return max(
            (curve.times[-1] for curve in self._curves.values() if curve.times),
            default=None,
        )

    def set_x_range(self, lo: float, hi: float) -> None:
        self._plot.set_x_range(lo, hi)

    # ---- Theme ----

    def _apply_theme_colors(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        pal = pyqtgraph_palette(tokens)
        self._plot.set_theme_colors(
            background=QColor(str(pal["background"])),
            foreground=QColor(str(pal["foreground"])),
            grid=QColor(str(pal["grid"])),
            header_bg=QColor(tokens["bg_1"]),
        )

    def _refresh_plot_theme(self) -> None:
        self._palette = pyqtgraph_palette(ThemeManager.instance().current_tokens())
        self._apply_theme_colors()
        cycle = self._palette["curves"]
        assert isinstance(cycle, list)
        for i, (path, curve) in enumerate(self._curves.items()):
            if curve.user_color:
                continue
            color = QColor(str(cycle[i % len(cycle)]))
            curve.set_color(color)
            self._plot.set_curve_color(path, color)
        self._curve_list.refresh_colors(self._color_for_path)

    def _color_for_path(self, path: str) -> QColor | None:
        curve = self._curves.get(path)
        return curve.color() if curve is not None else None

    # ---- DBC metadata / session ----

    def refresh_enum_metadata(self) -> None:
        """Re-resolve DBC metadata for each curve after a DBC change."""
        for curve in self._curves.values():
            curve.enum_choices, curve.unit, curve.arbitration_id = self._lookup_signal_meta(
                curve.message, curve.signal
            )
            self._plot.set_curve_unit(curve.path, curve.unit)
            self._apply_curve_y_axis(curve)
            sc, tt = self._display_extras(curve.message)
            self._curve_list.update_meta(curve, signal_centric=sc, tooltip=tt)

    def _display_extras(self, message: str) -> tuple[bool, str]:
        """Resolve signal-centric flag + tooltip text for ``message``.

        Returns the default (True, "") before a database is bound so the
        first paint matches the eventual steady state — refresh on
        ``database_changed`` will replace these with real values.
        """
        if self._ctx is None:
            return True, ""
        sc = self._ctx.database.signal_centric_for_message(message)
        origin = self._ctx.database.origin_for_message(message)
        if origin is None:
            return sc, ""
        buses, path = origin
        bus_label = ", ".join(buses) if buses else "—"
        return sc, f"Bus: {bus_label} · DBC: {path.name}"

    def clear_session(self) -> None:
        self.invalidate_wanted_aids()
        for curve in self._curves.values():
            curve.times.clear()
            curve.values.clear()
            curve.last_value = None
            curve.last_bus = None
            self._plot.clear_curve(curve.path)
            # Reset numeric range so the next sample re-seeds it.
            # Categorical curves keep their pinned [0, N-1] range.
            if curve.enum_choices is None:
                curve.y_min = None
                curve.y_max = None
                self._plot.set_y_numeric(curve.path, -1.0, 1.0)
        self._plot.set_x_range(0.0, self._window_seconds)

    def set_window_seconds(self, seconds: float) -> None:
        """Resize per-curve buffers, preserving the most recent samples."""
        self._window_seconds = seconds
        new_max = _maxlen_for_window(seconds)
        for curve in self._curves.values():
            if curve.times.maxlen == new_max:
                continue
            curve.times = deque(curve.times, maxlen=new_max)
            curve.values = deque(curve.values, maxlen=new_max)

    def invalidate_wanted_aids(self) -> None:
        self._wanted_aids.clear()

    def _lookup_signal_meta(
        self, message: str, signal: str
    ) -> tuple[dict[int, str] | None, str, int | None]:
        if self._ctx is None:
            return None, "", None
        decoder = self._ctx.database.decoder_for_message(message)
        if decoder is None:
            return None, "", None
        try:
            msg = decoder.database.get_message_by_name(message)
        except KeyError:
            return None, "", None
        arb_id = int(getattr(msg, "frame_id", 0)) or None
        for sig in msg.signals:
            if sig.name != signal:
                continue
            unit = getattr(sig, "unit", "") or ""
            choices = getattr(sig, "choices", None)
            if choices:
                return {int(k): str(v) for k, v in choices.items()}, unit, arb_id
            return None, unit, arb_id
        return None, "", arb_id

    @staticmethod
    def _categories_for_choices(choices: dict[int, str]) -> list[str]:
        if not choices:
            return []
        max_key = max(choices)
        return [choices.get(i, "") for i in range(max_key + 1)]

    def _apply_curve_y_axis(self, curve: _SignalCurve) -> None:
        """Push the curve's axis kind + initial range to the plot widget."""
        if curve.enum_choices is not None:
            cats = self._categories_for_choices(curve.enum_choices)
            self._plot.set_y_categorical(curve.path, cats)
            curve.y_min = None
            curve.y_max = None
        else:
            lo = curve.y_min if curve.y_min is not None else -1.0
            hi = curve.y_max if curve.y_max is not None else 1.0
            self._plot.set_y_numeric(curve.path, lo, hi)

    def _maybe_expand_curve_y(self, curve: _SignalCurve, value: float) -> None:
        if curve.enum_choices is not None:
            return
        if curve.y_min is None or curve.y_max is None:
            curve.y_min = value - 1.0
            curve.y_max = value + 1.0
            self._plot.set_y_numeric(curve.path, curve.y_min, curve.y_max)
            return
        if curve.y_min <= value <= curve.y_max:
            return
        span = max(curve.y_max - curve.y_min, 1.0)
        pad = span * _Y_HEADROOM
        if value < curve.y_min:
            curve.y_min = value - pad
        else:
            curve.y_max = value + pad
        self._plot.set_y_numeric(curve.path, curve.y_min, curve.y_max)

    def _wanted_aids_for_bus(self, bus_name: str) -> set[int]:
        cached = self._wanted_aids.get(bus_name)
        if cached is not None:
            return cached
        aids: set[int] = set()
        if self._ctx is not None:
            decoder = self._ctx.database.decoder_for_bus(bus_name)
            if decoder is not None:
                wanted_messages = {curve.message for curve in self._curves.values()}
                for name in wanted_messages:
                    try:
                        msg = decoder.database.get_message_by_name(name)
                    except KeyError:
                        continue
                    aids.add(msg.frame_id)
        self._wanted_aids[bus_name] = aids
        return aids

    def _next_color(self) -> str:
        cycle = self._palette["curves"]
        assert isinstance(cycle, list)
        return str(cycle[len(self._curves) % len(cycle)])

    # ---- Persistence ----

    def save_state(self) -> dict[str, object]:
        state: dict[str, object] = {"signals": list(self._curves.keys())}
        hidden = [p for p, c in self._curves.items() if not c.visible]
        if hidden:
            state["hidden_signals"] = hidden
        colors = {p: c.color().name() for p, c in self._curves.items() if c.user_color}
        if colors:
            state["colors"] = colors
        if self._plot.is_merge_requested():
            state["axes_merged"] = True
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        signals = state.get("signals")
        if isinstance(signals, list):
            for path in signals:
                if isinstance(path, str):
                    self.add_signal(path)
        hidden = state.get("hidden_signals")
        if isinstance(hidden, list):
            for path in hidden:
                if isinstance(path, str) and path in self._curves:
                    self._on_visibility_toggled(path, False)
        colors = state.get("colors")
        if isinstance(colors, dict):
            for path, name in colors.items():
                color = QColor(str(name))
                if path in self._curves and color.isValid():
                    self.set_curve_color(path, color)
        # Re-apply the merge intent after curves exist; it takes effect
        # only while the restored curves share a type.
        if state.get("axes_merged"):
            self._plot.set_y_merged(True)


class SignalPlotWindow(ChildView):
    """One left column (split into N legend sections) next to a right
    column (split into N plot rows). The two vertical splitters mirror
    each other so each legend section lines up with its plot row.

    The X (time) axis is shared across all rows.
    """

    KIND = "signal_plot"
    DEFAULT_SIZE = (820, 380)
    TITLE = "Signal Plot"

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, params=params)
        self._ctx: AppContext | None = None
        self._palette = pyqtgraph_palette(ThemeManager.instance().current_tokens())
        self._subplots: list[_SubPlot] = []
        # Picked up from the project default in ``bind``; the per-window
        # combobox can then override. Starts at the module default so a
        # window constructed without a context still has a sane value.
        self._window_seconds: float = _DEFAULT_WINDOW_SECONDS
        # Tracks whether the value was set by the user / a restore (in
        # which case ``bind`` must not overwrite it with the project
        # default).
        self._window_seconds_user_set: bool = False
        # Last user-set width for the left column. Re-applied when the
        # column is toggled back on after a hide.
        self._last_left_width = _DEFAULT_LEFT_PANE_WIDTH
        self._left_visible = True
        # Anti-recursion guard for the two synchronised V-splitters.
        self._syncing_v_splitters = False
        # User-intended splitter sizes, updated only on restore_state,
        # explicit user drag (``splitterMoved``), and add/remove sub-
        # plot. Persisted in ``save_state`` instead of live ``sizes()``
        # — the live values drift with Qt's layout pass (e.g. report
        # minimum-size-hint values before the widget is shown), which
        # would otherwise show up as a phantom "dirty" diff on close.
        self._committed_h_sizes: list[int] = [_DEFAULT_LEFT_PANE_WIDTH, 700]
        self._committed_v_sizes: list[int] = []
        self._paused = False
        # The X axis follows the newest sample, and wall-clock time while
        # none arrives: the newest sample time, when it was first seen, and
        # the right edge shown, which never moves back.
        self._sample_hi: float | None = None
        self._sample_wall = 0.0
        self._shown_hi: float | None = None
        # 10 Hz legend-refresh heartbeat. The plot widget paints at
        # its own (GPU-driven) cadence; the legend just needs to look
        # "live", not frame-accurate.
        self._legend_timer = QTimer(self)
        self._legend_timer.setInterval(100)
        self._legend_timer.timeout.connect(self._refresh_legend_live)
        self._build_body()

    def _format_title(self, params: dict[str, object]) -> str:
        name = params.get("name")
        return f"Signal Plot · {name}" if name else "Signal Plot"

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        ctx.bus_reader.frames_received.connect(self._on_frames_received)
        ctx.bus_reader.session_t0_changed.connect(self._on_t0_changed)
        ctx.bus_reader.bus_stopped.connect(self._on_bus_stopped)
        ctx.bus_reader.bus_health_changed.connect(self._on_bus_health_changed)
        ctx.database.database_changed.connect(self._invalidate_wanted_aids)
        ctx.database.database_changed.connect(self._refresh_enum_metadata)
        for sp in self._subplots:
            sp.bind_context(ctx)
        # Pick up the project default — but only if the user (or a
        # restore) hasn't already chosen something for this window.
        if not self._window_seconds_user_set:
            project_default = ctx.project.workspace.preferences.default_plot_window_seconds
            self._apply_window_seconds(project_default, mark_user=False)
        self._legend_timer.start()

    # ---- Body construction ----

    def _build_body(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Window-level header bar — the show/hide-pane and add-plot
        # buttons belong to the window as a whole, not to any one
        # sub-plot.
        self.header = ViewHeaderBar(self)
        top_strip = self.header
        top_strip.add_stretch(1)

        self._pause_segment = QFrame(top_strip)
        self._pause_segment.setObjectName("ModeSegment")
        self._pause_segment.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        pause_layout = QHBoxLayout(self._pause_segment)
        pause_layout.setContentsMargins(0, 0, 0, 0)
        pause_layout.setSpacing(0)

        self._running_btn = QToolButton(self._pause_segment)
        self._running_btn.setText("Running")
        self._running_btn.setCheckable(True)
        self._running_btn.setChecked(True)
        self._running_btn.setAutoExclusive(False)
        self._running_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._running_btn.setToolTip("Frames update the plot as they arrive.")

        self._paused_btn = QToolButton(self._pause_segment)
        self._paused_btn.setObjectName("SegmentRightBtn")
        self._paused_btn.setText("Paused")
        self._paused_btn.setCheckable(True)
        self._paused_btn.setAutoExclusive(False)
        self._paused_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._paused_btn.setToolTip("Freeze the plot; new frames are dropped.")

        self._pause_group = QButtonGroup(self)
        self._pause_group.setExclusive(True)
        self._pause_group.addButton(self._running_btn)
        self._pause_group.addButton(self._paused_btn)
        self._running_btn.clicked.connect(lambda: self._on_pause_selected(False))
        self._paused_btn.clicked.connect(lambda: self._on_pause_selected(True))

        pause_layout.addWidget(self._running_btn)
        pause_layout.addWidget(self._paused_btn)
        top_strip.add_widget(self._pause_segment)

        self._window_btn = QToolButton(top_strip)
        self._window_btn.setAutoRaise(True)
        self._window_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._window_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        # Right-to-left so the chevron icon sits to the right of the
        # value text. Latin glyphs are not directional so the text
        # itself still reads LTR.
        self._window_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._window_btn.setIconSize(QSize(10, 10))
        self._window_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        # Hide the native menu-indicator (Windows' style otherwise
        # paints a tiny, misaligned arrow). We render our own themed
        # chevron via setIcon below — same pattern as MainWindow's
        # hamburger button.
        self._window_btn.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        themed_icon("down", self._window_btn.setIcon)
        self._window_btn.setToolTip("Visible time window (pick a preset or enter a custom value)")
        # Tabular numerals so widths stay stable as the value changes
        # (e.g. "1 s" vs "15 min").
        mono_font = tabular_font()
        mono_font.setPointSizeF(self._window_btn.font().pointSizeF())
        self._window_btn.setFont(mono_font)

        self._window_menu = QMenu(self._window_btn)
        for label, value in _WINDOW_PRESETS:
            act = self._window_menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, v=value: self._apply_window_seconds(v, mark_user=True)
            )
        self._window_menu.addSeparator()
        custom_act = self._window_menu.addAction("Custom…")
        custom_act.triggered.connect(self._prompt_custom_window_seconds)
        self._window_btn.setMenu(self._window_menu)
        # Reserve enough width for the widest preset so the toolbar
        # doesn't jitter when the value changes.
        self._reserve_window_button_width()
        self._sync_window_button_text()
        top_strip.add_widget(self._window_btn)

        self._add_btn = QToolButton(top_strip)
        self._add_btn.setToolTip("Add plot")
        self._add_btn.setAutoRaise(True)
        self._add_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._add_btn.setIcon(icon("plus"))
        self._add_btn.clicked.connect(self._on_add_btn_clicked)
        top_strip.add_widget(self._add_btn)

        self._toggle_left_btn = top_strip.add_pane_toggle(
            "Show/hide signals pane", self._on_toggle_left_pane
        )
        themed_callback(self._refresh_add_icon)

        layout.addWidget(top_strip)

        self._h_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._h_splitter.setChildrenCollapsible(False)
        layout.addWidget(self._h_splitter, 1)

        # Left column: stacked legend sections.
        self._left_v_splitter = QSplitter(Qt.Orientation.Vertical, self._h_splitter)
        self._left_v_splitter.setChildrenCollapsible(False)
        self._left_v_splitter.setMinimumWidth(_MIN_LEFT_PANE_WIDTH)
        self._h_splitter.addWidget(self._left_v_splitter)

        # Right column: stacked plot rows (top strip + GL canvas each).
        self._right_v_splitter = QSplitter(Qt.Orientation.Vertical, self._h_splitter)
        self._right_v_splitter.setChildrenCollapsible(False)
        self._h_splitter.addWidget(self._right_v_splitter)

        self._h_splitter.setStretchFactor(0, 0)
        self._h_splitter.setStretchFactor(1, 1)
        self._h_splitter.setSizes([_DEFAULT_LEFT_PANE_WIDTH, 700])

        self._h_splitter.splitterMoved.connect(self._on_h_moved)
        self._left_v_splitter.splitterMoved.connect(self._on_left_v_moved)
        self._right_v_splitter.splitterMoved.connect(self._on_right_v_moved)

        # Open with the familiar single-row layout.
        self._add_subplot()
        self._refresh_toggle_icons()
        themed_callback(self._refresh_toggle_icons)

    # ---- Sub-plot management ----

    def _add_subplot(self, after: _SubPlot | None = None) -> _SubPlot:
        idx = len(self._subplots) if after is None else self._subplots.index(after) + 1
        sp = _SubPlot(
            self._palette,
            can_remove=len(self._subplots) > 0,
            window_seconds=self._window_seconds,
            parent=self,
        )
        if self._ctx is not None:
            sp.bind_context(self._ctx)
        sp.remove_requested.connect(lambda s=sp: self._remove_subplot(s))
        sp.move_in_requested.connect(self._on_move_in)
        self._left_v_splitter.insertWidget(idx, sp.legend_widget)
        self._right_v_splitter.insertWidget(idx, sp.plot_widget)
        self._subplots.insert(idx, sp)
        self._equalise_row_heights()
        self._update_remove_buttons()
        return sp

    def _remove_subplot(self, sp: _SubPlot) -> None:
        if len(self._subplots) <= 1:
            return
        self._subplots.remove(sp)
        sp.teardown()
        sp.deleteLater()
        self._equalise_row_heights()
        self._update_remove_buttons()

    def _update_remove_buttons(self) -> None:
        can_remove = len(self._subplots) > 1
        for sp in self._subplots:
            sp.set_can_remove(can_remove)

    def _equalise_row_heights(self) -> None:
        n = len(self._subplots)
        if n == 0:
            self._committed_v_sizes = []
            return
        h = max(self._right_v_splitter.height(), n)
        sizes = [h // n] * n
        # Distribute rounding error onto the first row.
        sizes[0] += h - sum(sizes)
        self._syncing_v_splitters = True
        try:
            self._left_v_splitter.setSizes(sizes)
            self._right_v_splitter.setSizes(sizes)
        finally:
            self._syncing_v_splitters = False
        self._committed_v_sizes = list(sizes)

    def _on_h_moved(self, *_args: object) -> None:
        self._committed_h_sizes = list(self._h_splitter.sizes())

    def _on_left_v_moved(self, *_args: object) -> None:
        if self._syncing_v_splitters:
            return
        self._syncing_v_splitters = True
        try:
            sizes = self._left_v_splitter.sizes()
            self._right_v_splitter.setSizes(sizes)
        finally:
            self._syncing_v_splitters = False
        self._committed_v_sizes = list(sizes)

    def _on_right_v_moved(self, *_args: object) -> None:
        if self._syncing_v_splitters:
            return
        self._syncing_v_splitters = True
        try:
            sizes = self._right_v_splitter.sizes()
            self._left_v_splitter.setSizes(sizes)
        finally:
            self._syncing_v_splitters = False
        self._committed_v_sizes = list(sizes)

    # ---- Inter-sub-plot move ----

    def _on_move_in(self, source: object, dest: object, paths: list[str]) -> None:
        if not (isinstance(source, _SubPlot) and isinstance(dest, _SubPlot)):
            return
        if source is dest:
            return
        if source not in self._subplots or dest not in self._subplots:
            return
        for path in paths:
            curve = source.release_curve(path)
            if curve is not None:
                dest.receive_curve(curve)

    # ---- Frame dispatch + shared X ----

    def _on_frames_received(self, batch: list[Any]) -> None:
        if self._ctx is None or not self._subplots or self._paused:
            return
        for sp in self._subplots:
            if not sp._curves:
                continue
            for frame in batch:
                sp.ingest_frame(frame)
        self._advance_x_window()

    def _advance_x_window(self) -> None:
        latest_times = [t for t in (sp.latest_time() for sp in self._subplots) if t is not None]
        if not latest_times:
            return
        latest = max(latest_times)
        if self._sample_hi is None or latest > self._sample_hi:
            self._sample_hi = latest
            self._sample_wall = time.monotonic()
        self._show_x(latest)

    def _advance_x_idle(self) -> None:
        if (
            self._ctx is None
            or self._paused
            or not self._ctx.bus_reader.is_open
            or self._sample_hi is None
        ):
            return
        stalled = time.monotonic() - self._sample_wall
        if stalled > _STALL_S:
            self._show_x(self._sample_hi + stalled)

    def _show_x(self, hi: float) -> None:
        # Never back: the adapter's clock and wall-clock time differ, so the
        # first sample after a stall may land left of the idle edge.
        self._shown_hi = hi if self._shown_hi is None else max(self._shown_hi, hi)
        lo = self._shown_hi - self._window_seconds
        for sp in self._subplots:
            sp.set_x_range(lo, self._shown_hi)

    # ---- Window-seconds plumbing ----

    def _apply_window_seconds(self, seconds: float, *, mark_user: bool) -> None:
        seconds = max(_MIN_WINDOW_SECONDS, min(_MAX_WINDOW_SECONDS, float(seconds)))
        self._window_seconds = seconds
        if mark_user:
            self._window_seconds_user_set = True
        for sp in self._subplots:
            sp.set_window_seconds(seconds)
        self._sync_window_button_text()
        # Snap the visible range immediately. If no samples yet, fall
        # back to [0, window] so the empty plot still spans the window.
        if self._shown_hi is None:
            lo, hi = 0.0, seconds
        else:
            hi = self._shown_hi
            lo = hi - seconds
        for sp in self._subplots:
            sp.set_x_range(lo, hi)

    def _sync_window_button_text(self) -> None:
        btn = getattr(self, "_window_btn", None)
        if btn is None:
            return
        btn.setText(_format_window_seconds(self._window_seconds))

    def _reserve_window_button_width(self) -> None:
        btn = self._window_btn
        original = btn.text()
        widest = 0
        for label, _ in _WINDOW_PRESETS:
            btn.setText(label)
            widest = max(widest, btn.sizeHint().width())
        btn.setText(original)
        btn.setMinimumWidth(widest)

    def _prompt_custom_window_seconds(self) -> None:
        current = _format_window_seconds(self._window_seconds)
        text, ok = QInputDialog.getText(
            self,
            "Custom time window",
            "Window length (e.g. '45 s' or '2 min'):",
            text=current,
        )
        if not ok:
            return
        parsed = _parse_window_text(text)
        if parsed is None:
            return
        self._apply_window_seconds(parsed, mark_user=True)

    def _on_t0_changed(self, _t0: object) -> None:
        self._sample_hi = None
        self._shown_hi = None
        for sp in self._subplots:
            sp.clear_session()

    def _on_bus_stopped(self, bus_name: str) -> None:
        for sp in self._subplots:
            sp.break_bus(bus_name)

    def _on_bus_health_changed(self, bus_name: str, state: str, _detail: str) -> None:
        if BusHealth(state) is not BusHealth.OK:
            for sp in self._subplots:
                sp.break_bus(bus_name)

    def _invalidate_wanted_aids(self) -> None:
        for sp in self._subplots:
            sp.invalidate_wanted_aids()

    def _refresh_enum_metadata(self) -> None:
        for sp in self._subplots:
            sp.refresh_enum_metadata()

    def _refresh_legend_live(self) -> None:
        for sp in self._subplots:
            sp.refresh_legend_live()
        self._advance_x_idle()

    # ---- Left column toggle ----

    def _on_toggle_left_pane(self) -> None:
        sizes = self._h_splitter.sizes()
        if self._left_visible:
            if sizes and sizes[0] > 0:
                self._last_left_width = sizes[0]
            self._left_v_splitter.setVisible(False)
            self._left_visible = False
        else:
            self._left_v_splitter.setVisible(True)
            self._left_visible = True
            total = sum(sizes) if sizes else 0
            if total > 0:
                right = max(total - self._last_left_width, 0)
                self._h_splitter.setSizes([self._last_left_width, right])
        self._refresh_toggle_icons()

    def _refresh_toggle_icons(self) -> None:
        self._toggle_left_btn.setIcon(
            icon("pane_left_on" if self._left_visible else "pane_left_off")
        )

    def _refresh_add_icon(self) -> None:
        self._add_btn.setIcon(icon("plus"))

    def _on_add_btn_clicked(self) -> None:
        self._add_subplot()

    def _on_pause_selected(self, paused: bool) -> None:
        self._paused = paused

    # ---- Public API (delegates) ----

    def add_signal(self, path: str) -> None:
        """Add a curve to the first sub-plot."""
        if not self._subplots:
            self._add_subplot()
        self._subplots[0].add_signal(path)

    def add_message(self, message_name: str) -> None:
        if not self._subplots:
            self._add_subplot()
        self._subplots[0].add_message(message_name)

    def remove_signal(self, path: str) -> None:
        for sp in self._subplots:
            if path in sp._curves:
                sp.remove_signal(path)
                return

    def signal_paths(self) -> list[str]:
        result: list[str] = []
        for sp in self._subplots:
            result.extend(sp.signal_paths())
        return result

    # ---- Legacy accessors (kept for tests and any callers that hit
    # internals on the single-plot shape) ----

    @property
    def _curves(self) -> dict[str, _SignalCurve]:
        merged: dict[str, _SignalCurve] = {}
        for sp in self._subplots:
            merged.update(sp._curves)
        return merged

    @property
    def _curve_list(self) -> _CurveListPane:
        return self._subplots[0]._curve_list

    @property
    def _drop(self) -> SignalDropArea:
        return self._subplots[0]._curve_list._drop

    @property
    def _plot(self) -> GLPlotWidget:
        return self._subplots[0]._plot

    def _on_visibility_toggled(self, path: str, visible: bool) -> None:
        for sp in self._subplots:
            if path in sp._curves:
                sp._on_visibility_toggled(path, visible)
                return

    # ---- Persistence ----

    def save_state(self) -> dict[str, object]:
        state = super().save_state()
        sub_states = [sp.save_state() for sp in self._subplots]
        state["subplots"] = sub_states
        # Persist the user-intended (committed) sizes rather than
        # ``sizes()``. The live values drift across the lifetime of
        # the window (Qt returns minimum-size hints before show,
        # actual sizes after layout) and would otherwise mark the
        # workspace dirty even when the user changed nothing.
        state["h_splitter_sizes"] = list(self._committed_h_sizes)
        state["v_splitter_sizes"] = list(self._committed_v_sizes)
        state["left_pane_visible"] = self._left_visible
        if self._window_seconds_user_set:
            state["window_seconds"] = self._window_seconds
        # Mirror the first sub-plot's keys at the top level so old
        # consumers (and tests asserting on ``signals`` / etc.)
        # continue to work.
        if sub_states:
            for key in ("signals", "hidden_signals"):
                if key in sub_states[0]:
                    state[key] = sub_states[0][key]
        # Hold onto the H-splitter sizes as ``splitter_sizes`` too for
        # legacy callers; tests inspect this key.
        state["splitter_sizes"] = list(self._committed_h_sizes)
        state["paused"] = self._paused_btn.isChecked()
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        super().restore_state(state)
        # Restore window_seconds before any subplot/curve restore so
        # new curves' deques are sized correctly the first time.
        window_seconds = state.get("window_seconds")
        if isinstance(window_seconds, int | float):
            self._apply_window_seconds(float(window_seconds), mark_user=True)
        subplots_state = state.get("subplots")
        if isinstance(subplots_state, list) and subplots_state:
            while len(self._subplots) > len(subplots_state):
                self._remove_subplot(self._subplots[-1])
            while len(self._subplots) < len(subplots_state):
                self._add_subplot()
            for sp, st in zip(self._subplots, subplots_state, strict=False):
                if isinstance(st, dict):
                    sp.restore_state(st)
        elif self._subplots:
            # Legacy single-plot state — drop it into the first
            # sub-plot.
            self._subplots[0].restore_state(state)

        # H-splitter (left column width vs right column).
        h_sizes = state.get("h_splitter_sizes") or state.get("splitter_sizes")
        if isinstance(h_sizes, list) and len(h_sizes) == 2:
            try:
                int_sizes = [int(s) for s in h_sizes]
            except (TypeError, ValueError):
                int_sizes = None
            if int_sizes is not None:
                self._h_splitter.setSizes(int_sizes)
                self._committed_h_sizes = int_sizes
                if int_sizes[0] > 0:
                    self._last_left_width = int_sizes[0]

        # V-splitter (row heights). Synced across left and right.
        v_sizes = state.get("v_splitter_sizes")
        if isinstance(v_sizes, list) and len(v_sizes) == len(self._subplots):
            try:
                int_v = [int(s) for s in v_sizes]
            except (TypeError, ValueError):
                int_v = None
            if int_v is not None:
                self._syncing_v_splitters = True
                try:
                    self._left_v_splitter.setSizes(int_v)
                    self._right_v_splitter.setSizes(int_v)
                finally:
                    self._syncing_v_splitters = False
                self._committed_v_sizes = int_v

        # Left column visibility.
        visible = state.get("left_pane_visible")
        if isinstance(visible, bool) and visible != self._left_visible:
            self._on_toggle_left_pane()

        paused = state.get("paused")
        if isinstance(paused, bool):
            if paused:
                self._paused_btn.setChecked(True)
            else:
                self._running_btn.setChecked(True)
            self._on_pause_selected(paused)
