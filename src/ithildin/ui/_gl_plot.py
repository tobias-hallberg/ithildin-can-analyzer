# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""GPU-accelerated 2D line plot widget with tiered per-curve Y axes.

Drop-in rendering surface for ``SignalPlotWindow``. Each data segment
is rendered as a pixel-space quad (two triangles) built in the vertex
shader from (a_pos, a_other, a_side) — `glLineWidth > 1.0` is a no-op
in core profile and a horizontal line drawn as a 1-pixel ``GL_LINE``
can fall on a pixel-row boundary and disappear, so we draw thick lines
ourselves. Axes, ticks, labels, and grid are painted on top with
``QPainter``. Every pixel ours, no QML.

Each curve carries its **own Y range and unit label** — the layout
follows Claude Design's chosen direction (option C, "tiered left
axes"; see ``design_handoff_signal_plot/``): one Y axis per visible
signal, stacked left to right, each in the curve's colour, each
with its own ticks. A single time axis renders at the bottom.

Public API (consumed by ``signal_plot.py``):

- ``add_curve(path, color)`` / ``remove_curve(path)`` /
  ``set_curve_color(path, color)`` / ``set_curve_unit(path, unit)``
- ``append_point(path, x, y)`` / ``clear_curve(path)``
- ``set_x_range(lo, hi)``
- ``set_y_numeric(path, lo, hi)`` / ``set_y_categorical(path, cats)``
- ``set_theme_colors(bg, fg, grid)``

The widget calls ``update()`` from each mutator; Qt batches paints.

Uses ``QOpenGLFunctions_3_3_Core`` so we don't pull in a PyOpenGL
dependency. The handful of GL constants we touch are defined as
module-level integers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPen,
    QSurfaceFormat,
    QVector3D,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFunctions_3_3_Core,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget


# ---- GL constants we touch (Qt's QOpenGLFunctions doesn't expose them) -
_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_TRIANGLES = 0x0004
_GL_FLOAT = 0x1406
_GL_FALSE = 0
_GL_BLEND = 0x0BE2
_GL_SRC_ALPHA = 0x0302
_GL_ONE_MINUS_SRC_ALPHA = 0x0303
_GL_SCISSOR_TEST = 0x0C11


# Each segment between two consecutive data points is an independent
# pixel-space quad (4 corners → 2 triangles → 6 vertices). The shader
# expands each vertex perpendicular to (a_other - a_pos) by
# ``u_view_pack.z`` (half-width in px) on the side carried in ``a_side``.
# Adjacent segments don't share corner vertices: that's our "bevel"
# join — the small triangular gap on the outside of a turn is invisible
# at 1-px line widths and avoids miter spikes at sharp angles.
# Known issue: see ``KNOWN_ISSUE_plot_line_dashing.md`` at repo root.
_VERTEX_SHADER = """\
#version 330 core
layout(location = 0) in vec2  a_pos;
layout(location = 1) in vec2  a_other;
layout(location = 2) in float a_side;
uniform vec4 u_range;     // (x_min, x_max, y_min, y_max)
uniform vec3 u_view_pack; // (plot_w_px, plot_h_px, half_width_px)

vec2 to_ndc(vec2 p) {
    return vec2(
        (p.x - u_range.x) / (u_range.y - u_range.x) * 2.0 - 1.0,
        (p.y - u_range.z) / (u_range.w - u_range.z) * 2.0 - 1.0
    );
}

void main() {
    vec2 p_ndc = to_ndc(a_pos);
    vec2 q_ndc = to_ndc(a_other);
    vec2 to_px = u_view_pack.xy * 0.5;
    vec2 dir_px = (q_ndc - p_ndc) * to_px;
    float len = length(dir_px);
    vec2 tan_px = (len > 1e-6) ? (dir_px / len) : vec2(1.0, 0.0);
    vec2 perp_px = vec2(-tan_px.y, tan_px.x);
    vec2 perp_offset_ndc = (perp_px * (a_side * u_view_pack.z)) / to_px;
    // Each vertex extends 1 px AWAY from a_other along the tangent,
    // so adjacent segments overlap by 2 px at their shared data point.
    // Without this, short segments (sub-pixel at typical zoom, or
    // sparse signals like 10 Hz CAN data) miss MSAA sample positions
    // and the line looks dashed — especially for high-contrast colours.
    vec2 extend_ndc = -tan_px * 1.0 / to_px;
    gl_Position = vec4(p_ndc + perp_offset_ndc + extend_ndc, 0.0, 1.0);
}
"""

_FRAGMENT_SHADER = """\
#version 330 core
uniform vec4 u_color;
out vec4 fragColor;
void main() {
    fragColor = u_color;
}
"""

# Visible line width in CSS pixels. We scale by devicePixelRatio when
# pushing to the shader so the on-screen thickness is constant across
# DPI settings. Kept at 1.0 because half-pixel widths blur into both
# adjacent rows via AA, which reads as a soft / fuzzy line.
_LINE_WIDTH_PX = 1.0
# Bytes per vertex in the segment VBO: vec2 a_pos + vec2 a_other +
# float a_side = 5 floats.
_VERTEX_STRIDE = 5 * 4
_VERTICES_PER_SEGMENT = 6
_FLOATS_PER_SEGMENT = _VERTICES_PER_SEGMENT * 5

# Layout constants — from Claude Design's chosen prototypes
# (``design_handoff_signal_plot/`` for the tiered axes and
# ``design_handoff_signal_plot_2/`` for the header-strip unit chips).
# Keep these names so a future implementer can cross-reference.
_AXIS_COL_W = 56.0  # base / numeric Y axis column width
_AXIS_COL_MAX_W = 160.0  # cap for an auto-widened categorical column
_AXIS_LABEL_PAD = 15.0  # spine→col-right (4) + label gap (5) + left margin
_HEADER_H = 22.0  # top header strip that holds the unit chips
_PAD_RIGHT = 16.0
_PAD_TOP = _HEADER_H + 8.0  # gap below the chip strip before ticks start
_PAD_BOTTOM = 26.0

# Unit chip dimensions inside the header strip.
_UNIT_CHIP_W = 38.0
_UNIT_CHIP_H = 14.0
_UNIT_CHIP_Y = 4.0
_UNIT_CHIP_RX = 2.0
# Default (non-selected) opacity; selection emphasis isn't wired yet.
_UNIT_CHIP_FILL_ALPHA = 0.14
_UNIT_CHIP_STROKE_ALPHA = 0.55

_NUMERIC_TICK_COUNT = 5
_AXIS_TICK_LEN = 2.0
_AXIS_LABEL_PT = 9.5
_AXIS_UNIT_PT = 10.0
_TIME_AXIS_TICK_LEN = 3.0
_TIME_AXIS_TICKS = 5
_TIME_AXIS_LABEL_PT = 10.0


@dataclass
class _Curve:
    color: QColor
    # Float32 interleaved vertex buffer: each completed segment owns
    # ``_VERTICES_PER_SEGMENT`` vertices, each a (pos, other, side)
    # tuple = 5 floats. The shader builds the segment quad from this.
    data: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float32))
    segment_count: int = 0
    segment_capacity: int = 0
    # Last data point appended via ``append_point``. The next call
    # builds a segment from (last_x, last_y) → (new_x, new_y).
    last_x: float = 0.0
    last_y: float = 0.0
    has_last: bool = False
    vbo: QOpenGLBuffer | None = None
    uploaded: bool = False
    # Per-curve Y range. ``categories`` is non-None when the curve
    # is a value-table enum — in that case the consumer feeds inverted
    # values (``plotted = (N-1) - raw_index``) and the axis range is
    # pinned to ``[0, N-1]``.
    y_min: float = -1.0
    y_max: float = 1.0
    categories: list[str] | None = None
    unit: str = ""
    # Hidden curves keep their data + VBO but don't get rendered,
    # don't claim an axis column, and aren't tracked by the axis
    # painter's visible-index enumeration.
    visible: bool = True


class GLPlotWidget(QOpenGLWidget):
    """OpenGL plot widget with one Y axis per curve.

    Optionally collapses to a single shared Y axis ("merged" mode) when
    every visible curve is the same type — see ``set_y_merged`` and the
    ⧉ header-strip button.
    """

    close_clicked = Signal()
    """Emitted when the optional close button in the header strip is
    clicked (only fires when ``set_close_button_visible(True)`` is in
    effect)."""

    merge_toggled = Signal(bool)
    """Emitted with the new merged state whenever it changes — by the
    header ⧉ button or by auto-reset when the curves stop sharing a
    type. Lets the owning sub-plot persist the flag."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setSamples(4)  # MSAA; driver may downgrade
        self.setFormat(fmt)

        self._curves: dict[str, _Curve] = {}
        self._x_min: float = 0.0
        self._x_max: float = 1.0

        self._bg = QColor("#0d1117")
        self._fg = QColor("#c9d1d9")
        self._grid = QColor(255, 255, 255, 40)
        # Header strip background (one notch lighter than the canvas)
        # so the unit chips sit on a subtly-distinct band — matches
        # the design's ``var(--bg-1)``.
        self._header_bg = QColor("#1a1e23")

        # Close button — painted onto the header strip when enabled.
        # Painted (not a QToolButton child) because child widgets on a
        # QOpenGLWidget composite unreliably on Windows.
        self._close_visible: bool = False
        self._close_hover: bool = False
        # Merged Y-axis mode: all visible curves share one axis column +
        # one scale. ``_merge_requested`` is the sticky user intent; the
        # effective state (``_merge_active``) additionally requires the
        # visible curves to share a type, so a transient type mismatch
        # only suspends the join rather than discarding the intent.
        self._merge_requested: bool = False
        self._merge_hover: bool = False
        self.setMouseTracking(True)

        # GL state — only valid after ``initializeGL``.
        self._gl: QOpenGLFunctions_3_3_Core | None = None
        self._program: QOpenGLShaderProgram | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._u_range_loc = -1
        self._u_color_loc = -1
        self._u_view_pack_loc = -1

    # ---- Public API ----------------------------------------------------

    def add_curve(self, path: str, color: QColor) -> None:
        if path in self._curves:
            return
        self._curves[path] = _Curve(color=QColor(color))
        self.update()

    def remove_curve(self, path: str) -> None:
        curve = self._curves.pop(path, None)
        if curve is not None and curve.vbo is not None:
            self.makeCurrent()
            try:
                curve.vbo.destroy()
            finally:
                self.doneCurrent()
        self.update()

    def append_point(self, path: str, x: float, y: float) -> None:
        """Extend the curve to ``(x, y)``. A non-finite ``y`` breaks the line:
        the next point starts a new chain."""
        curve = self._curves.get(path)
        if curve is None:
            return
        if not math.isfinite(y):
            curve.has_last = False
            self.update()
            return
        if curve.has_last:
            if curve.segment_count >= curve.segment_capacity:
                self._grow(curve)
            ax, ay = curve.last_x, curve.last_y
            bx, by = x, y
            o = curve.segment_count * _FLOATS_PER_SEGMENT
            # Quad NW-SW-NE / SW-SE-NE for segment A→B. "N" / "S" are
            # pixel-space sides set by the shader's perpendicular; the
            # side bit flips between vertices anchored at A vs. B
            # because perp(B→A) = -perp(A→B).
            curve.data[o : o + _FLOATS_PER_SEGMENT] = (
                ax,
                ay,
                bx,
                by,
                +1.0,  # NW: at A, a_other=B, +1
                ax,
                ay,
                bx,
                by,
                -1.0,  # SW: at A, a_other=B, -1
                bx,
                by,
                ax,
                ay,
                -1.0,  # NE: at B, a_other=A, -1
                ax,
                ay,
                bx,
                by,
                -1.0,  # SW (repeat)
                bx,
                by,
                ax,
                ay,
                +1.0,  # SE: at B, a_other=A, +1
                bx,
                by,
                ax,
                ay,
                -1.0,  # NE (repeat)
            )
            curve.segment_count += 1
            curve.uploaded = False
        curve.last_x = x
        curve.last_y = y
        curve.has_last = True
        self.update()

    def clear_curve(self, path: str) -> None:
        curve = self._curves.get(path)
        if curve is None:
            return
        curve.segment_count = 0
        curve.has_last = False
        curve.uploaded = False
        self.update()

    def set_curve_color(self, path: str, color: QColor) -> None:
        curve = self._curves.get(path)
        if curve is None:
            return
        curve.color = QColor(color)
        self.update()

    def set_curve_unit(self, path: str, unit: str) -> None:
        curve = self._curves.get(path)
        if curve is None:
            return
        curve.unit = unit
        self.update()

    def set_curve_visible(self, path: str, visible: bool) -> None:
        curve = self._curves.get(path)
        if curve is None or curve.visible == visible:
            return
        curve.visible = visible
        self.update()

    def set_close_button_visible(self, visible: bool) -> None:
        """Show or hide the close (✕) button in the header strip."""
        if self._close_visible == visible:
            return
        self._close_visible = visible
        if not visible:
            self._close_hover = False
            self.unsetCursor()
        self.update()

    def set_x_range(self, lo: float, hi: float) -> None:
        if hi <= lo:
            return
        self._x_min = lo
        self._x_max = hi
        self.update()

    def set_y_numeric(self, path: str, lo: float, hi: float) -> None:
        curve = self._curves.get(path)
        if curve is None or hi <= lo:
            return
        curve.categories = None
        curve.y_min = lo
        curve.y_max = hi
        self.update()

    def set_y_categorical(self, path: str, categories: list[str]) -> None:
        curve = self._curves.get(path)
        if curve is None or not categories:
            return
        curve.categories = list(categories)
        # Consumer feeds inverted values, so axis range stays
        # [0, N-1] with N-1 at the top.
        curve.y_min = 0.0
        curve.y_max = float(len(categories) - 1) if len(categories) > 1 else 1.0
        self.update()

    def set_y_merged(self, merged: bool) -> None:
        """Request (or clear) the merged single-axis view.

        The request is *sticky*: it is honoured whenever the visible
        curves share a type (``_merge_enabled``) and ignored otherwise,
        so adding a same-type signal keeps the axes joined while adding a
        different-type one only suspends the join until it's removed.
        Emits ``merge_toggled`` with the resulting effective state."""
        req = bool(merged)
        if req == self._merge_requested:
            return
        self._merge_requested = req
        self.merge_toggled.emit(self._merge_active())
        self.update()

    def is_y_merged(self) -> bool:
        """Effective merged state — requested *and* currently mergeable."""
        return self._merge_active()

    def is_merge_requested(self) -> bool:
        """The sticky user intent, regardless of current mergeability."""
        return self._merge_requested

    def _type_key(self, curve: _Curve) -> tuple[str, object]:
        if curve.categories is not None:
            return ("cat", tuple(curve.categories))
        return ("num", curve.unit)

    def _merge_enabled(self) -> bool:
        visible = self._visible_curves()
        if len(visible) < 2:
            return False
        return len({self._type_key(c) for c in visible}) == 1

    def _merge_active(self) -> bool:
        return self._merge_requested and self._merge_enabled()

    def _merged_range(self, visible: list[_Curve]) -> tuple[float, float]:
        lo = min(c.y_min for c in visible)
        hi = max(c.y_max for c in visible)
        if hi <= lo:
            hi = lo + 1.0
        return lo, hi

    def set_theme_colors(
        self,
        background: QColor,
        foreground: QColor,
        grid: QColor,
        header_bg: QColor | None = None,
    ) -> None:
        self._bg = QColor(background)
        self._fg = QColor(foreground)
        self._grid = QColor(grid)
        if header_bg is not None:
            self._header_bg = QColor(header_bg)
        self.update()

    # ---- GL lifecycle --------------------------------------------------

    def initializeGL(self) -> None:
        self._gl = QOpenGLFunctions_3_3_Core()
        self._gl.initializeOpenGLFunctions()

        self._program = QOpenGLShaderProgram(self)
        self._program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_SHADER)
        self._program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, _FRAGMENT_SHADER
        )
        if not self._program.link():
            raise RuntimeError(f"GLPlotWidget: shader link failed: {self._program.log()}")
        self._u_range_loc = self._program.uniformLocation("u_range")
        self._u_color_loc = self._program.uniformLocation("u_color")
        self._u_view_pack_loc = self._program.uniformLocation("u_view_pack")

        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()

    def resizeGL(self, w: int, h: int) -> None:
        del w, h

    def paintGL(self) -> None:
        if self._gl is None or self._program is None or self._vao is None:
            return
        gl = self._gl
        plot_rect = self._plot_rect()

        gl.glClearColor(self._bg.redF(), self._bg.greenF(), self._bg.blueF(), 1.0)
        gl.glClear(_GL_COLOR_BUFFER_BIT)

        # Scissor the plot area so curves can't bleed into the axis
        # margins or the top/bottom padding.
        dpr = self.devicePixelRatio()
        widget_h_px = int(self.height() * dpr)
        sx = int(plot_rect.left() * dpr)
        sy = widget_h_px - int((plot_rect.top() + plot_rect.height()) * dpr)
        sw = int(plot_rect.width() * dpr)
        sh = int(plot_rect.height() * dpr)
        gl.glEnable(_GL_SCISSOR_TEST)
        gl.glScissor(sx, sy, sw, sh)
        gl.glViewport(sx, sy, sw, sh)

        gl.glEnable(_GL_BLEND)
        gl.glBlendFunc(_GL_SRC_ALPHA, _GL_ONE_MINUS_SRC_ALPHA)

        # Pack viewport size + half-line-width into a single vec3
        # uniform: PySide6's QOpenGLShaderProgram.setUniformValue does
        # land QVector3D reliably, but the single-scalar and two-scalar
        # overloads silently no-op (leaving the uniform at zero — which
        # collapses every quad's offset to 0 in the shader).
        view_pack = QVector3D(float(sw), float(sh), float(_LINE_WIDTH_PX * dpr * 0.5))

        merged = self._merge_active()
        m_lo, m_hi = self._merged_range(self._visible_curves()) if merged else (0.0, 1.0)

        self._program.bind()
        self._vao.bind()
        try:
            if self._u_view_pack_loc >= 0:
                self._program.setUniformValue(self._u_view_pack_loc, view_pack)
            for curve in self._curves.values():
                if curve.segment_count < 1 or not curve.visible:
                    continue
                self._ensure_uploaded(curve)
                if curve.vbo is None:
                    continue
                # Per-curve uniform: every curve gets transformed by
                # its own Y range. X range stays shared (the time
                # axis is one). When merged, all curves share one scale.
                y_lo, y_hi = (m_lo, m_hi) if merged else (curve.y_min, curve.y_max)
                self._program.setUniformValue(
                    self._u_range_loc,
                    float(self._x_min),
                    float(self._x_max),
                    float(y_lo),
                    float(y_hi),
                )
                curve.vbo.bind()
                self._program.enableAttributeArray(0)
                self._program.enableAttributeArray(1)
                self._program.enableAttributeArray(2)
                # a_pos (vec2)    at offset 0
                # a_other (vec2)  at offset 8
                # a_side (float)  at offset 16
                self._program.setAttributeBuffer(0, _GL_FLOAT, 0, 2, _VERTEX_STRIDE)
                self._program.setAttributeBuffer(1, _GL_FLOAT, 8, 2, _VERTEX_STRIDE)
                self._program.setAttributeBuffer(2, _GL_FLOAT, 16, 1, _VERTEX_STRIDE)
                self._program.setUniformValue(
                    self._u_color_loc,
                    curve.color.redF(),
                    curve.color.greenF(),
                    curve.color.blueF(),
                    1.0,
                )
                gl.glDrawArrays(_GL_TRIANGLES, 0, curve.segment_count * _VERTICES_PER_SEGMENT)
                self._program.disableAttributeArray(0)
                self._program.disableAttributeArray(1)
                self._program.disableAttributeArray(2)
                curve.vbo.release()
        finally:
            self._vao.release()
            self._program.release()

        gl.glDisable(_GL_SCISSOR_TEST)
        gl.glViewport(0, 0, int(self.width() * dpr), widget_h_px)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._paint_overlay(painter, plot_rect)
        painter.end()

    # ---- GL buffer management ------------------------------------------

    def _ensure_uploaded(self, curve: _Curve) -> None:
        if curve.uploaded and curve.vbo is not None:
            return
        if curve.vbo is None:
            vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            vbo.setUsagePattern(QOpenGLBuffer.UsagePattern.DynamicDraw)
            vbo.create()
            curve.vbo = vbo
        curve.vbo.bind()
        n_floats = curve.segment_count * _FLOATS_PER_SEGMENT
        slice_bytes = curve.data[:n_floats].tobytes()
        curve.vbo.allocate(slice_bytes, len(slice_bytes))
        curve.vbo.release()
        curve.uploaded = True

    @staticmethod
    def _grow(curve: _Curve) -> None:
        new_capacity = max(256, curve.segment_capacity * 2)
        new_data = np.zeros(new_capacity * _FLOATS_PER_SEGMENT, dtype=np.float32)
        if curve.segment_count > 0:
            n = curve.segment_count * _FLOATS_PER_SEGMENT
            new_data[:n] = curve.data[:n]
        curve.data = new_data
        curve.segment_capacity = new_capacity

    # ---- Layout --------------------------------------------------------

    def _visible_curves(self) -> list[_Curve]:
        """Curves currently rendered, in insertion order.

        Hidden curves keep their data + VBO but drop out of the axis
        enumeration so the layout re-tiers from ``i = 0`` when a curve
        is toggled off.
        """
        return [c for c in self._curves.values() if c.visible]

    def _axis_column_widths(self) -> list[float]:
        """Per-visible-curve Y axis column width. Numeric columns keep
        the base width; categorical columns grow to fit their longest
        value label, clamped to ``[_AXIS_COL_W, _AXIS_COL_MAX_W]``."""
        fm = QFontMetricsF(_scaled_font(self.font(), _AXIS_LABEL_PT))

        def col_width(curve: _Curve) -> float:
            if curve.categories:
                longest = max(
                    (fm.horizontalAdvance(c) for c in curve.categories),
                    default=0.0,
                )
                return min(max(_AXIS_COL_W, longest + _AXIS_LABEL_PAD), _AXIS_COL_MAX_W)
            return _AXIS_COL_W

        visible = self._visible_curves()
        if self._merge_active():
            # One shared column; same-type curves all share its width.
            return [col_width(visible[0])]
        return [col_width(c) for c in visible]

    def _plot_rect(self) -> QRectF:
        """Inner drawing rect — one Y axis column per *visible* curve."""
        left = sum(self._axis_column_widths())
        return QRectF(
            left,
            _PAD_TOP,
            max(1.0, self.width() - left - _PAD_RIGHT),
            max(1.0, self.height() - _PAD_TOP - _PAD_BOTTOM),
        )

    # ---- Overlay painting ----------------------------------------------

    def _paint_overlay(self, painter: QPainter, plot: QRectF) -> None:
        self._paint_background_grid(painter, plot)
        self._paint_y_axes(painter, plot)
        self._paint_time_axis(painter, plot)
        # Drawn last so the chips sit on top of any axis spine that
        # happens to extend up into the header strip area.
        self._paint_header_strip(painter)
        if self._close_visible:
            self._paint_close_button(painter)
        if self._visible_curves():
            self._paint_merge_button(painter)

    def _close_button_rect(self) -> QRectF:
        # 18×18 button right-aligned in the 22 px header strip; sits
        # to the right of any unit chip thanks to ``_PAD_RIGHT``.
        size = 18.0
        margin = 4.0
        y = (_HEADER_H - size) / 2.0
        return QRectF(self.width() - size - margin, y, size, size)

    def _paint_close_button(self, painter: QPainter) -> None:
        rect = self._close_button_rect()
        painter.save()
        if self._close_hover:
            painter.setBrush(QColor(self._fg.red(), self._fg.green(), self._fg.blue(), 40))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 3.0, 3.0)
        # X glyph centred in the rect.
        pen = QPen(self._fg)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        inset = 4.0
        p1 = QPointF(rect.left() + inset, rect.top() + inset)
        p2 = QPointF(rect.right() - inset, rect.bottom() - inset)
        p3 = QPointF(rect.left() + inset, rect.bottom() - inset)
        p4 = QPointF(rect.right() - inset, rect.top() + inset)
        painter.drawLine(p1, p2)
        painter.drawLine(p3, p4)
        painter.restore()

    def _merge_button_rect(self) -> QRectF:
        # 18×18 button in the header strip, just left of the close
        # button when it's shown, otherwise at the same right margin.
        size = 18.0
        margin = 4.0
        gap = 4.0
        y = (_HEADER_H - size) / 2.0
        if self._close_visible:
            right = self._close_button_rect().left() - gap
        else:
            right = self.width() - margin
        return QRectF(right - size, y, size, size)

    def _paint_merge_button(self, painter: QPainter) -> None:
        """Two overlapping squares (⧉) — collapse same-type axes into one.

        Greyed when the curves don't share a type; filled when merged."""
        rect = self._merge_button_rect()
        enabled = self._merge_enabled()
        engaged = self._merge_active()
        painter.save()

        if engaged:
            bg = QColor(self._fg.red(), self._fg.green(), self._fg.blue(), 64)
            painter.setBrush(bg)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 3.0, 3.0)
        elif self._merge_hover and enabled:
            bg = QColor(self._fg.red(), self._fg.green(), self._fg.blue(), 40)
            painter.setBrush(bg)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 3.0, 3.0)

        glyph = QColor(self._fg)
        glyph.setAlpha(235 if enabled else 70)
        inset = 4.0
        s = 7.0
        off = 3.0
        back = QRectF(rect.left() + inset + off, rect.top() + inset, s, s)
        front = QRectF(rect.left() + inset, rect.top() + inset + off, s, s)
        pen = QPen(glyph)
        pen.setWidthF(1.3)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(back, 1.5, 1.5)
        # Front square fills with the header background so the overlap
        # reads as layered rather than crosshatched.
        painter.setBrush(self._header_bg)
        painter.drawRoundedRect(front, 1.5, 1.5)
        painter.restore()

    def _paint_header_strip(self, painter: QPainter) -> None:
        """Top band with one unit chip centred on each Y-axis column.

        The chip is the *only* place the unit appears now — replaces
        the old above-spine text label that crowded the topmost tick
        number (see ``design_handoff_signal_plot_2/`` rationale).
        """
        visible = self._visible_curves()
        if not visible:
            return
        header_rect = QRectF(0.0, 0.0, self.width(), _HEADER_H)
        painter.save()
        painter.setBrush(self._header_bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(header_rect)

        # Separator line at the bottom of the strip.
        pen_sep = QPen(self._grid)
        pen_sep.setWidthF(0.5)
        pen_sep.setCosmetic(True)
        painter.setPen(pen_sep)
        painter.drawLine(QPointF(0.0, _HEADER_H), QPointF(self.width(), _HEADER_H))

        chip_font = _scaled_font(self.font(), _AXIS_UNIT_PT)
        chip_font.setWeight(QFont.Weight.Medium)
        fm = QFontMetricsF(chip_font)
        painter.setFont(chip_font)

        if self._merge_active():
            self._paint_merged_unit_chip(painter, fm)
            painter.restore()
            return

        widths = self._axis_column_widths()
        acc = 0.0
        for curve, col_w in zip(visible, widths, strict=True):
            acc += col_w
            if not curve.unit:
                continue
            # Right-align the chip with the spine: chip's right edge
            # sits at ``spine_x``, so the chip lives entirely within
            # its own column (left half) without overhanging the
            # column boundary.
            spine_x = acc - 4.0
            chip = QRectF(
                spine_x - _UNIT_CHIP_W,
                _UNIT_CHIP_Y,
                _UNIT_CHIP_W,
                _UNIT_CHIP_H,
            )
            fill = QColor(curve.color)
            fill.setAlphaF(_UNIT_CHIP_FILL_ALPHA)
            stroke = QColor(curve.color)
            stroke.setAlphaF(_UNIT_CHIP_STROKE_ALPHA)
            painter.setBrush(fill)
            pen = QPen(stroke)
            pen.setWidthF(0.75)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawRoundedRect(chip, _UNIT_CHIP_RX, _UNIT_CHIP_RX)
            # Centred unit text.
            tw = fm.horizontalAdvance(curve.unit)
            text_x = chip.center().x() - tw / 2.0
            # Vertically centre using ascent / 2 plus half the chip
            # height adjustment; small fudge keeps glyphs visually
            # midline-aligned.
            text_y = chip.center().y() + fm.ascent() / 2.0 - 2.0
            painter.setPen(QColor(curve.color))
            painter.drawText(QPointF(text_x, text_y), curve.unit)
        painter.restore()

    def _paint_merged_unit_chip(self, painter: QPainter, fm: QFontMetricsF) -> None:
        """Single neutral unit chip for the shared merged axis."""
        first = self._visible_curves()[0]
        unit = first.unit
        if first.categories is not None or not unit:
            return
        spine_x = self._axis_column_widths()[0] - 4.0
        chip = QRectF(spine_x - _UNIT_CHIP_W, _UNIT_CHIP_Y, _UNIT_CHIP_W, _UNIT_CHIP_H)
        fill = QColor(self._fg)
        fill.setAlphaF(_UNIT_CHIP_FILL_ALPHA)
        stroke = QColor(self._fg)
        stroke.setAlphaF(_UNIT_CHIP_STROKE_ALPHA)
        painter.setBrush(fill)
        pen = QPen(stroke)
        pen.setWidthF(0.75)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRoundedRect(chip, _UNIT_CHIP_RX, _UNIT_CHIP_RX)
        tw = fm.horizontalAdvance(unit)
        text_x = chip.center().x() - tw / 2.0
        text_y = chip.center().y() + fm.ascent() / 2.0 - 2.0
        painter.setPen(self._fg)
        painter.drawText(QPointF(text_x, text_y), unit)

    def _paint_background_grid(self, painter: QPainter, plot: QRectF) -> None:
        """5×5 dashed background grid — fixed, independent of axis ticks."""
        pen = QPen(self._grid)
        pen.setWidthF(0.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([2.0, 3.0])
        pen.setCosmetic(True)
        painter.setPen(pen)
        for p in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = plot.top() + p * plot.height()
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            x = plot.left() + p * plot.width()
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

    def _paint_y_axes(self, painter: QPainter, plot: QRectF) -> None:
        if self._merge_active():
            self._paint_merged_y_axis(painter, plot)
            return
        label_font = _scaled_font(self.font(), _AXIS_LABEL_PT)
        fm_label = QFontMetricsF(label_font)
        widths = self._axis_column_widths()
        acc = 0.0
        for curve, col_w in zip(self._visible_curves(), widths, strict=True):
            acc += col_w
            spine_x = acc - 4.0
            color = QColor(curve.color)

            pen_spine = QPen(color)
            pen_spine.setWidthF(1.0)
            pen_spine.setCosmetic(True)
            painter.setPen(pen_spine)
            painter.drawLine(QPointF(spine_x, plot.top()), QPointF(spine_x, plot.bottom()))

            # Tick + label colour: 85 % alpha per the handoff so
            # labels sit a touch quieter than the spine.
            label_color = QColor(color)
            label_color.setAlpha(int(0.85 * 255))
            painter.setFont(label_font)
            painter.setPen(color)

            tick_values, tick_labels = self._y_tick_values_and_labels(curve)
            for v, text in zip(tick_values, tick_labels, strict=True):
                y = self._data_to_px_y(v, curve, plot)
                painter.setPen(color)
                painter.drawLine(QPointF(spine_x - _AXIS_TICK_LEN, y), QPointF(spine_x, y))
                painter.setPen(label_color)
                if curve.categories is not None:
                    text = fm_label.elidedText(
                        text,
                        Qt.TextElideMode.ElideRight,
                        int(col_w - _AXIS_LABEL_PAD),
                    )
                tw = fm_label.horizontalAdvance(text)
                painter.drawText(
                    QPointF(spine_x - 5.0 - tw, y + fm_label.ascent() / 2.0 - 2.0),
                    text,
                )

    def _paint_merged_y_axis(self, painter: QPainter, plot: QRectF) -> None:
        """One shared, neutral-coloured Y axis for all same-type curves."""
        visible = self._visible_curves()
        first = visible[0]
        col_w = self._axis_column_widths()[0]
        spine_x = col_w - 4.0
        color = QColor(self._fg)

        label_font = _scaled_font(self.font(), _AXIS_LABEL_PT)
        fm_label = QFontMetricsF(label_font)
        painter.setFont(label_font)

        pen_spine = QPen(color)
        pen_spine.setWidthF(1.0)
        pen_spine.setCosmetic(True)
        painter.setPen(pen_spine)
        painter.drawLine(QPointF(spine_x, plot.top()), QPointF(spine_x, plot.bottom()))

        label_color = QColor(color)
        label_color.setAlpha(int(0.85 * 255))

        if first.categories is not None:
            lo, hi = first.y_min, first.y_max
            values = [float(i) for i in range(len(first.categories))]
            labels = list(first.categories)
        else:
            lo, hi = self._merged_range(visible)
            values = _nice_ticks(lo, hi, _NUMERIC_TICK_COUNT)
            labels = [_format_y_tick(v, first.unit) for v in values]

        span = hi - lo if hi > lo else 1.0
        for v, text in zip(values, labels, strict=True):
            y = plot.bottom() - (v - lo) / span * plot.height()
            painter.setPen(color)
            painter.drawLine(QPointF(spine_x - _AXIS_TICK_LEN, y), QPointF(spine_x, y))
            painter.setPen(label_color)
            if first.categories is not None:
                text = fm_label.elidedText(
                    text, Qt.TextElideMode.ElideRight, int(col_w - _AXIS_LABEL_PAD)
                )
            tw = fm_label.horizontalAdvance(text)
            painter.drawText(
                QPointF(spine_x - 5.0 - tw, y + fm_label.ascent() / 2.0 - 2.0),
                text,
            )

    def _y_tick_values_and_labels(self, curve: _Curve) -> tuple[list[float], list[str]]:
        if curve.categories is not None:
            n = len(curve.categories)
            values = [float(i) for i in range(n)]
            return values, list(curve.categories)
        ticks = _nice_ticks(curve.y_min, curve.y_max, _NUMERIC_TICK_COUNT)
        return ticks, [_format_y_tick(v, curve.unit) for v in ticks]

    def _paint_time_axis(self, painter: QPainter, plot: QRectF) -> None:
        font = _scaled_font(self.font(), _TIME_AXIS_LABEL_PT)
        fm = QFontMetricsF(font)
        painter.setFont(font)

        pen = QPen(self._fg)
        pen.setWidthF(0.75)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))

        label_color = QColor(self._fg)
        label_color.setAlpha(int(0.7 * 255))

        span = self._x_max - self._x_min
        for p_idx in range(_TIME_AXIS_TICKS):
            p = p_idx / (_TIME_AXIS_TICKS - 1)
            t = self._x_min + p * span
            x = plot.left() + p * plot.width()
            painter.setPen(pen)
            painter.drawLine(
                QPointF(x, plot.bottom()),
                QPointF(x, plot.bottom() + _TIME_AXIS_TICK_LEN),
            )
            text = f"{t:.1f}s"
            tw = fm.horizontalAdvance(text)
            painter.setPen(label_color)
            painter.drawText(
                QPointF(x - tw / 2.0, plot.bottom() + _TIME_AXIS_TICK_LEN + fm.ascent()),
                text,
            )

    # ---- Coordinate transforms ------------------------------------------

    def _data_to_px_x(self, x: float, plot: QRectF) -> float:
        span = self._x_max - self._x_min
        if span == 0:
            return plot.left()
        return plot.left() + (x - self._x_min) / span * plot.width()

    def _data_to_px_y(self, y: float, curve: _Curve, plot: QRectF) -> float:
        span = curve.y_max - curve.y_min
        if span == 0:
            return plot.bottom()
        return plot.bottom() - (y - curve.y_min) / span * plot.height()

    # ---- Mouse ----

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = QPointF(event.position())
            if (
                self._visible_curves()
                and self._merge_enabled()
                and self._merge_button_rect().contains(pos)
            ):
                event.accept()
                self.set_y_merged(not self._merge_requested)
                return
            if self._close_visible and self._close_button_rect().contains(pos):
                event.accept()
                self.close_clicked.emit()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = QPointF(event.position())
        close_in = self._close_visible and self._close_button_rect().contains(pos)
        merge_in = (
            bool(self._visible_curves())
            and self._merge_enabled()
            and self._merge_button_rect().contains(pos)
        )
        if close_in != self._close_hover or merge_in != self._merge_hover:
            self._close_hover = close_in
            self._merge_hover = merge_in
            if close_in or merge_in:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.unsetCursor()
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._close_hover or self._merge_hover:
            self._close_hover = False
            self._merge_hover = False
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)


# ---- Helpers ------------------------------------------------------------


def _scaled_font(base: QFont, pt: float, *, bold: bool = False) -> QFont:
    f = QFont(base)
    f.setPointSizeF(pt)
    if bold:
        f.setBold(True)
    return f


def _nice_ticks(lo: float, hi: float, target_count: int) -> list[float]:
    """Pick ~``target_count`` human-friendly tick values in ``[lo, hi]``."""
    if hi <= lo:
        return []
    raw_step = (hi - lo) / max(target_count, 1)
    if raw_step <= 0:
        return []
    magnitude = 10 ** math.floor(math.log10(raw_step))
    normalized = raw_step / magnitude
    if normalized < 1.5:
        nice = 1.0
    elif normalized < 3.5:
        nice = 2.0
    elif normalized < 7.5:
        nice = 5.0
    else:
        nice = 10.0
    step = nice * magnitude
    start = math.ceil(lo / step) * step
    out: list[float] = []
    v = start
    for _ in range(target_count + 4):
        if v > hi + step * 0.5:
            break
        out.append(round(v / step) * step)
        v += step
    return out


def _format_y_tick(value: float, unit: str) -> str:
    """Y-axis tick formatter — handoff's ``fmtTick`` rules.

    Snap rpm to integer; otherwise decimal precision tracks the
    magnitude of the value: ≥100 → 0 decimals, ≥10 → 1, else 2.
    Sub-milli values fall back to scientific to dodge zeros.
    """
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
