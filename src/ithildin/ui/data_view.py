# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Data view child window — multi-instance.

Tabular live view of decoded signals. Each row tracks one signal:
name, current value, unit, and a thin range gauge filled according to
``(value - dbc_min) / (dbc_max - dbc_min)``. The gauge bounds come
from the DBC's declared signal range — *not* from observed traffic —
so "100% full" always means at the declared maximum, not at the
highest value seen so far. Signals without a declared range render
without a gauge.

Drop target accepts ``application/x-ithildin-signal``. Decode runs per
``frames_received`` batch, filter-then-decode so the GUI thread stays
responsive.

A value dims while it is stale: its bus is closed, or no update came for
``max(_STALE_MIN_S, _STALE_PERIODS × its period)``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ithildin.ui._child_base import ChildView
from ithildin.ui._drag_payload import MessagePayload, SignalPayload
from ithildin.ui._signal_drop import SignalDropArea
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import themed_callback
from ithildin.ui.theme import ThemeManager
from ithildin.ui.view_header import ViewHeaderBar

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.core.types import FrameRecord


_COL_SIGNAL = 0
_COL_VALUE = 1
_COL_UNIT = 2
_COL_GAUGE = 3

_STALE_MIN_S = 2.0
_STALE_PERIODS = 3
_STALE_CHECK_MS = 1000


@dataclass
class _Row:
    path: str
    message: str
    signal: str
    unit: str = ""
    dbc_min: float | None = None
    dbc_max: float | None = None
    last: float | str | None = None
    # Staleness bookkeeping, set on each value update: the frame's bus and
    # timestamp, the interval since the previous one, and the monotonic
    # time of the update.
    bus: str | None = None
    ts: float | None = None
    period: float | None = None
    updated: float | None = None
    stale: bool = False


class DataViewWindow(ChildView):
    KIND = "data_view"
    DEFAULT_SIZE = (720, 360)
    TITLE = "Data view"

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, params=params)
        self._ctx: AppContext | None = None
        self._rows: dict[str, _Row] = {}
        self._dim_color = QColor()
        self._build_body()
        themed_callback(self._restyle_stale)

    def _format_title(self, params: dict[str, object]) -> str:
        name = params.get("name")
        return f"Data view · {name}" if name else "Data view"

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        ctx.bus_reader.frames_received.connect(self._on_frames_received)
        ctx.database.database_changed.connect(self._rebuild_table)
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(_STALE_CHECK_MS)
        self._stale_timer.timeout.connect(self._refresh_staleness)
        self._stale_timer.start()

    # ---- Public API ----

    def add_signal(self, path: str) -> None:
        if "." not in path or path in self._rows:
            return
        message, signal = path.split(".", 1)
        unit, dbc_min, dbc_max = self._lookup_signal_meta(message, signal)
        self._rows[path] = _Row(
            path=path,
            message=message,
            signal=signal,
            unit=unit,
            dbc_min=dbc_min,
            dbc_max=dbc_max,
        )
        self._rebuild_table()

    def add_message(self, message_name: str) -> None:
        """Add every signal of a message as a row.

        Works pre-capture: when no bus is open we resolve the message
        through any loaded DBC.
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

    def _on_payload_dropped(self, payload: object) -> None:
        if isinstance(payload, SignalPayload):
            self.add_signal(payload.path())
        elif isinstance(payload, MessagePayload):
            self.add_message(payload.message)

    def signal_paths(self) -> list[str]:
        return list(self._rows.keys())

    def save_state(self) -> dict[str, object]:
        state = super().save_state()
        state["signals"] = list(self._rows.keys())
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        super().restore_state(state)
        signals = state.get("signals")
        if not isinstance(signals, list):
            return
        for path in signals:
            if isinstance(path, str):
                self.add_signal(path)

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

        host = QWidget(drop)
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)

        self._table = QTableWidget(0, 4, host)
        self._table.setHorizontalHeaderLabels(["Signal", "Value", "Unit", "Range"])
        self._table.setFont(tabular_font())
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(_COL_SIGNAL, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(_COL_VALUE, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(_COL_UNIT, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(_COL_GAUGE, QHeaderView.ResizeMode.Stretch)
        host_layout.addWidget(self._table, 1)
        self._grow_columns()

        self._hint = QLabel("Drop signals here from the Database tree", host)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet("color: palette(mid); padding: 6px;")
        host_layout.addWidget(self._hint)

        drop.set_inner(host)

    def _lookup_signal_meta(
        self,
        message: str,
        signal: str,
    ) -> tuple[str, float | None, float | None]:
        """Return ``(unit, minimum, maximum)`` for a signal from any DBC.

        Searches the bus's bound DBC first (when a bus is open); falls back
        to every loaded DBC otherwise. Missing fields come back as
        ``("", None, None)``.
        """
        if self._ctx is None:
            return ("", None, None)
        bus = self._ctx.bus_reader.bus_name
        if bus is None:
            for bus_name in {
                b
                for path in self._ctx.database.loaded_paths()
                for b in self._ctx.database.buses_for_path(path)
            }:
                meta = self._lookup_signal_meta_in(bus_name, message, signal)
                if meta != ("", None, None):
                    return meta
            return ("", None, None)
        return self._lookup_signal_meta_in(bus, message, signal)

    def _lookup_signal_meta_in(
        self,
        bus: str,
        message: str,
        signal: str,
    ) -> tuple[str, float | None, float | None]:
        if self._ctx is None:
            return ("", None, None)
        decoder = self._ctx.database.decoder_for_bus(bus)
        if decoder is None:
            return ("", None, None)
        try:
            msg = decoder.database.get_message_by_name(message)
        except KeyError:
            return ("", None, None)
        for sig in msg.signals:
            if sig.name == signal:
                unit = getattr(sig, "unit", "") or ""
                lo = getattr(sig, "minimum", None)
                hi = getattr(sig, "maximum", None)
                return (
                    unit,
                    float(lo) if lo is not None else None,
                    float(hi) if hi is not None else None,
                )
        return ("", None, None)

    # ---- Slots ----

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
        if self._ctx is None or frame.is_error_frame:
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
            elif isinstance(value, (int, float)):
                row.last = float(value)
            else:
                row.last = str(value)

    def _render(self) -> None:
        for r, row in enumerate(self._rows.values()):
            self._set_value_cells(r, row)
        self._grow_columns()

    def _refresh_staleness(self) -> None:
        if self._ctx is None:
            return
        open_buses = set(self._ctx.bus_reader.bus_names())
        now = time.monotonic()
        for r, row in enumerate(self._rows.values()):
            item = self._table.item(r, _COL_VALUE)
            if row.updated is None or item is None:
                continue
            age = now - row.updated
            stale = row.bus not in open_buses or age > max(
                _STALE_MIN_S, _STALE_PERIODS * (row.period or 0.0)
            )
            item.setToolTip(f"Last update {age:.0f} s ago")
            if stale == row.stale:
                continue
            row.stale = stale
            if stale:
                item.setForeground(self._dim_color)
            else:
                item.setData(Qt.ItemDataRole.ForegroundRole, None)

    def _restyle_stale(self) -> None:
        self._dim_color = QColor(ThemeManager.instance().current_tokens()["fg_3"])
        for r, row in enumerate(self._rows.values()):
            item = self._table.item(r, _COL_VALUE)
            if row.stale and item is not None:
                item.setForeground(self._dim_color)

    def _set_value_cells(self, r: int, row: _Row) -> None:
        if row.last is not None:
            cell = self._table.item(r, _COL_VALUE)
            if cell is not None:
                if isinstance(row.last, float):
                    cell.setText(f"{row.last:.4g}")
                else:
                    cell.setText(row.last)
        gauge_widget = self._table.cellWidget(r, _COL_GAUGE)
        if isinstance(gauge_widget, QProgressBar):
            self._update_gauge(gauge_widget, row)

    def _rebuild_table(self) -> None:
        self._table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows.values()):
            # The fresh Value item isn't dimmed; the next refresh re-applies it.
            row.stale = False
            display, tooltip = self._display_for(row)
            signal_item = QTableWidgetItem(display)
            if tooltip:
                signal_item.setToolTip(tooltip)
            self._table.setItem(r, _COL_SIGNAL, signal_item)
            self._table.setItem(r, _COL_VALUE, QTableWidgetItem("—"))
            self._table.setItem(r, _COL_UNIT, QTableWidgetItem(row.unit))
            gauge = QProgressBar(self._table)
            gauge.setRange(0, 1000)
            gauge.setValue(0)
            gauge.setTextVisible(False)
            gauge.setFixedHeight(8)
            gauge.setStyleSheet(
                "QProgressBar { background: palette(dark); border: 1px solid palette(mid); }"
                "QProgressBar::chunk { background: palette(link); }"
            )
            self._table.setCellWidget(r, _COL_GAUGE, gauge)
            # Signals without a declared range never show a gauge — a
            # permanently-empty bar would be misleading. Must run after
            # setCellWidget, which forces the widget visible.
            if row.dbc_min is None or row.dbc_max is None or row.dbc_max <= row.dbc_min:
                gauge.setVisible(False)
        self._hint.setVisible(not self._rows)
        self._grow_columns()

    def _grow_columns(self) -> None:
        """Widen content-sized columns when content needs more room.

        Columns never auto-shrink: removing a long signal or short-lived
        wide value leaves the column at its peak width so labels don't
        jitter as rows are added and removed.
        """
        h = self._table.horizontalHeader()
        for c in (_COL_SIGNAL, _COL_VALUE, _COL_UNIT):
            needed = max(self._table.sizeHintForColumn(c), h.sectionSizeHint(c))
            if needed > h.sectionSize(c):
                h.resizeSection(c, needed)

    def _display_for(self, row: _Row) -> tuple[str, str]:
        """Resolve display label + tooltip honouring per-DBC signal-centric.

        Falls back to the canonical ``message.signal`` path before the
        view has been bound, so an unbound DataViewWindow still renders
        a sensible label.
        """
        if self._ctx is None:
            return row.path, ""
        db = self._ctx.database
        sc = db.signal_centric_for_message(row.message)
        display = row.signal if sc else row.path
        origin = db.origin_for_message(row.message)
        if origin is None:
            return display, ""
        buses, path = origin
        bus_label = ", ".join(buses) if buses else "—"
        return display, f"Bus: {bus_label} · DBC: {path.name}"

    @staticmethod
    def _update_gauge(gauge: QProgressBar, row: _Row) -> None:
        if row.dbc_min is None or row.dbc_max is None or row.dbc_max <= row.dbc_min:
            gauge.setVisible(False)
            return
        if not isinstance(row.last, float):
            gauge.setVisible(False)
            return
        gauge.setVisible(True)
        ratio = (row.last - row.dbc_min) / (row.dbc_max - row.dbc_min)
        ratio = max(0.0, min(1.0, ratio))
        gauge.setValue(int(ratio * 1000))
