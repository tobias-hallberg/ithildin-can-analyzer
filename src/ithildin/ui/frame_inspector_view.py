# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Frame Inspector view widget.

A plain ``QWidget`` rendering one ``FrameRecord``: a small header line,
a 3-column × 2-row identity grid, eight byte cells, and a
decoded-signals table. Embedded as the detail pane at the bottom of
the Trace window.

The identity grid keeps the dim-label / value visual that lets the eye
scan to a known column ("Period…" sits in the same spot every time),
instead of forcing the reader to parse a long bullet-separated string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.types import FrameRecord
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import themed_callback
from ithildin.ui.theme import ThemeManager

if TYPE_CHECKING:
    from cantools.database.can.message import Message

    from ithildin.app_state.context import AppContext


class FrameInspectorView(QWidget):
    """Render a single ``FrameRecord``. Updates via :meth:`set_frame`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx: AppContext | None = None
        self._byte_cells: list[QLabel] = []
        self._build_body()

    def bind(self, ctx: AppContext) -> None:
        """Wire to an ``AppContext`` so the renderer can resolve decoders
        and the session ``t0`` for relative timestamps. The view does
        not subscribe to ``frame_selected`` itself — its host calls
        :meth:`set_frame` directly when a row is selected — but the
        cached last-selected frame is hydrated here so an out-of-band
        binding still shows something useful.
        """
        self._ctx = ctx
        if ctx.events.last_selected_frame is not None:
            self.set_frame(ctx.events.last_selected_frame)

    # ---- Build ----

    def _build_body(self) -> None:
        # The whole inspector lives inside a QScrollArea so a single
        # scrollbar carries the header strip, identity grid, byte cells,
        # and decoded signals together. The table's own vertical
        # scrollbar is suppressed and its height is pinned to its row
        # count so the outer scroll is authoritative.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self._scroll)

        body = QWidget(self._scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)

        self._header = QLabel("Frame —", body)
        self._header.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._header)

        # Identity grid: 3 columns × 2 rows of (dim label · value) pairs.
        # Lays out horizontally so the pane uses its wide-but-short
        # aspect; cells stay in fixed positions so the eye can scan to
        # a known column.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(4)

        self._id_val = self._add_field(grid, "ID", 0, 0)
        self._name_val = self._add_field(grid, "Name", 0, 1)
        self._dlc_val = self._add_field(grid, "DLC", 0, 2)
        self._dir_val = self._add_field(grid, "Direction", 1, 0)
        self._sender_val = self._add_field(grid, "Sender", 1, 1)
        self._period_val = self._add_field(grid, "Period", 1, 2)

        for col in range(3):
            grid.setColumnStretch(col, 1)
        layout.addLayout(grid)

        # Byte cells.
        bytes_box = QFrame(self)
        bytes_box.setFrameShape(QFrame.Shape.NoFrame)
        bytes_layout = QHBoxLayout(bytes_box)
        bytes_layout.setContentsMargins(0, 0, 0, 0)
        bytes_layout.setSpacing(4)
        cell_font = tabular_font()
        for i in range(8):
            cell = QLabel(self._format_cell(i, None), bytes_box)
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setFixedSize(40, 40)
            cell.setFont(cell_font)
            bytes_layout.addWidget(cell)
            self._byte_cells.append(cell)
        bytes_layout.addStretch(1)
        layout.addWidget(bytes_box)
        # Byte-cell chrome from theme tokens, re-applied on theme switch. The
        # previous hardcoded white rgba fill was invisible on the light theme.
        themed_callback(self._restyle_byte_cells)

        # Decoded signals table. The table's own vertical scrollbar is
        # suppressed; its height is pinned to its row count by
        # `_resize_table_to_rows` so the surrounding QScrollArea is the
        # single scroll surface for the whole inspector.
        self._table = QTableWidget(0, 4, body)
        self._table.setHorizontalHeaderLabels(["Signal", "Raw", "Phys", "Unit"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._resize_table_to_rows()
        layout.addWidget(self._table)
        # Bottom stretch so when the inspector pane is taller than the
        # body's preferred height the extra space sits below the table
        # instead of stretching the table itself.
        layout.addStretch(1)

        self._scroll.setWidget(body)

    def _restyle_byte_cells(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        qss = f"background: {tokens['bg_2']}; border: 1px solid {tokens['border_1']};"
        for cell in self._byte_cells:
            cell.setStyleSheet(qss)

    def _add_field(self, grid: QGridLayout, label_text: str, row: int, col: int) -> QLabel:
        """Drop a ``[dim label] [value]`` cell into the identity grid.

        Returns the value label so callers can update it later.
        """
        cell = QWidget(self)
        cell.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        h = QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        label = QLabel(label_text, cell)
        label.setStyleSheet("color: palette(mid);")
        value = QLabel("—", cell)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(label)
        h.addWidget(value, 1)
        grid.addWidget(cell, row, col)
        return value

    @staticmethod
    def _format_cell(offset: int, byte_value: int | None) -> str:
        if byte_value is None:
            return f"{offset}\n--"
        return f"{offset}\n{byte_value:02X}"

    def _resize_table_to_rows(self) -> None:
        """Pin the table's height to exactly fit its header + rows.

        Called whenever the row count changes. The QScrollArea wrapping
        the inspector body is the single scroll surface; the table must
        not introduce a second scroll region. Always reserves at least
        one row of height so the empty-DBC state still shows the column
        headers cleanly.
        """
        header_h = self._table.horizontalHeader().height()
        row_h = self._table.verticalHeader().defaultSectionSize()
        frame_h = self._table.frameWidth() * 2
        rows = max(self._table.rowCount(), 1)
        self._table.setFixedHeight(header_h + rows * row_h + frame_h)

    # ---- Slots ----

    def set_frame(self, frame: object, observed_period_s: float | None = None) -> None:
        if not isinstance(frame, FrameRecord):
            return
        self._render(frame, observed_period_s)

    def _render(self, frame: FrameRecord, observed_period_s: float | None) -> None:
        t0 = self._ctx.bus_reader.session_t0 if self._ctx else None
        rel = frame.timestamp if t0 is None else frame.timestamp - t0
        self._header.setText(f"Frame #{frame.seq} · {rel:.3f}s · {frame.bus_name}")

        if frame.is_extended_id:
            self._id_val.setText(f"0x{frame.arbitration_id:08X} ({frame.arbitration_id})")
        else:
            self._id_val.setText(f"0x{frame.arbitration_id:03X} ({frame.arbitration_id})")

        if frame.is_error_frame:
            message = None
            name = "Error frame" if frame.coalesced == 1 else f"Error frame ×{frame.coalesced}"
        else:
            message = self._lookup_message(frame)
            name = message.name if message is not None else ""
        self._name_val.setText(name or "—")
        self._dlc_val.setText(str(len(frame.data)))
        # Direction is RX in v0.1 (TX paths land with MCP in Stage 1.D).
        self._dir_val.setText("RX")
        self._sender_val.setText(_format_senders(message))
        self._period_val.setText(_format_period(message, observed_period_s))

        for i, cell in enumerate(self._byte_cells):
            byte = frame.data[i] if i < len(frame.data) else None
            cell.setText(self._format_cell(i, byte))

        self._render_decoded(frame, message)

    def _lookup_message(self, frame: FrameRecord) -> Message | None:
        if self._ctx is None:
            return None
        decoder = self._ctx.database.decoder_for_bus(frame.bus_name)
        if decoder is None:
            return None
        try:
            return decoder.database.get_message_by_frame_id(frame.arbitration_id)
        except KeyError:
            return None

    def _render_decoded(self, frame: FrameRecord, message: Message | None) -> None:
        if message is None:
            self._table.setRowCount(0)
            self._resize_table_to_rows()
            return
        try:
            phys = message.decode(frame.data, decode_choices=True, allow_truncated=True)
            raw = message.decode(
                frame.data, decode_choices=False, scaling=False, allow_truncated=True
            )
        except (ValueError, KeyError):
            self._table.setRowCount(0)
            self._resize_table_to_rows()
            return
        if not isinstance(phys, dict) or not isinstance(raw, dict):
            self._table.setRowCount(0)
            self._resize_table_to_rows()
            return

        rows = list(phys.items())
        self._table.setRowCount(len(rows))
        for row, (sig_name, phys_value) in enumerate(rows):
            raw_value = raw.get(sig_name, "")
            unit = ""
            try:
                sig = message.get_signal_by_name(sig_name)
                unit = getattr(sig, "unit", "") or ""
            except KeyError:
                pass
            self._table.setItem(row, 0, QTableWidgetItem(sig_name))
            self._table.setItem(row, 1, QTableWidgetItem(_format_value(raw_value)))
            self._table.setItem(row, 2, QTableWidgetItem(_format_value(phys_value)))
            self._table.setItem(row, 3, QTableWidgetItem(unit))
        self._resize_table_to_rows()


def _format_value(v: object) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _format_senders(message: Message | None) -> str:
    if message is None:
        return "—"
    senders = getattr(message, "senders", None) or []
    if not senders:
        return "—"
    return ", ".join(str(s) for s in senders)


def _format_period(message: Message | None, observed_period_s: float | None) -> str:
    """Prefer the observed inter-arrival period; fall back to the DBC
    ``cycle_time`` attribute; otherwise ``—``.

    Observed beats DBC-declared even when both exist — the user is
    looking at a live trace and the measured value is what they care
    about. Sub-10 ms periods get one decimal so a nominally 0 ms
    timing isn't shown as "0 ms".
    """
    if observed_period_s is not None and observed_period_s > 0:
        ms = observed_period_s * 1000.0
        if ms < 10:
            return f"{ms:.1f} ms"
        return f"{ms:.0f} ms"
    if message is not None:
        cycle = getattr(message, "cycle_time", None)
        if cycle:
            return f"{int(cycle)} ms"
    return "—"
