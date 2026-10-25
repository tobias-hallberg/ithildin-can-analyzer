# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Merged trace tree model — frames as top-level, signals as children.

Replaces ``MessageTraceTreeModel`` and ``BusTraceTreeModel``. The model
is parameterised by an admission predicate (``admits(bus, aid)``) supplied
by the owning window from its ``TrackedTreeModel.effective()`` snapshot;
the model itself stays agnostic to the shape of the subscription.

Two display modes:

- ``append`` (default): every admitted frame becomes a top-level row,
  newest at row 0; oldest rows evicted at ``max_rows``. Time column is
  the absolute timestamp.
- ``overwrite``: one row per ``(bus, arbitration_id)`` pair, sorted by
  ``(arbitration_id, bus_name)``. New arrivals update in place. Time
  column is the delta to the previous arrival of the same key
  (``—`` for the first sighting).

Per-bus decoders: messages on bus A use the DBC bound to A; same ID on
bus B uses B's decoder. ``decoders`` is mutable via ``set_decoders`` so
the window can swap in a new map when the project's DBC bindings
change without rebuilding the model.

Rows outlive their bus: ``set_rings`` only swaps where new frames come
from, and a closed bus's rows stay, drawn dim, until ``clear`` drops
everything for a new capture session. In ``overwrite`` mode a row also
dims while its ID has gone quiet.
"""

from __future__ import annotations

import bisect
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Literal, Protocol

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor

from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord
from ithildin.ui.icons import themed_callback
from ithildin.ui.theme import ThemeManager

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ithildin.app_state.bus_reader import BusReader
    from ithildin.core.decoder import Decoder

Mode = Literal["append", "overwrite"]


class AdmitPredicate(Protocol):
    def __call__(self, bus_name: str, arbitration_id: int) -> bool: ...


class _AdmitNone:
    def __call__(self, bus_name: str, arbitration_id: int) -> bool:
        del bus_name, arbitration_id
        return False


_admit_none: AdmitPredicate = _AdmitNone()

_STALE_MIN_S = 2.0
_STALE_PERIODS = 3
"""A Latest row goes stale after ``max(_STALE_MIN_S, _STALE_PERIODS * period)``
without a new frame."""
_STALE_SWEEP_MS = 1000


class TraceTreeModel(QAbstractItemModel):
    COLUMNS = ("Time", "Bus", "Dir", "ID", "DLC", "Data", "Name")

    COL_TIME = 0
    COL_BUS = 1
    COL_DIR = 2
    COL_ID = 3
    COL_DLC = 4
    COL_DATA = 5
    COL_NAME = 6

    counters_changed = Signal(int, int)  # rx_total, err_total

    def __init__(
        self,
        rings: Mapping[str, RingBuffer],
        decoders: Mapping[str, Decoder | None] | None = None,
        admit: AdmitPredicate | None = None,
        max_rows: int = 100_000,
        update_hz: int = 30,
        bus_reader: BusReader | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if max_rows <= 0:
            raise ValueError("max_rows must be > 0")
        if update_hz <= 0:
            raise ValueError("update_hz must be > 0")
        self._rings = dict(rings)
        self._decoders: dict[str, Decoder | None] = dict(decoders) if decoders else {}
        self._admit: AdmitPredicate = admit if admit is not None else _admit_none
        self._bus_reader = bus_reader
        self._max_rows = max_rows
        self._mode: Mode = "append"
        # Append-mode storage: chronological deque (oldest at index 0,
        # newest at index -1). Display order is inverted in the
        # QAbstractItemModel methods.
        self._rows: deque[FrameRecord] = deque(maxlen=max_rows)
        # id(frame) -> nominal row index. real_row = nominal - row_offset.
        # The offset trick keeps parent() lookups O(1) across head evictions.
        self._row_of_frame: dict[int, int] = {}
        self._row_offset = 0
        # Overwrite-mode storage: keyed by (bus_name, arbitration_id).
        # ``_sorted_keys`` is the canonical visible-row → key mapping.
        # Sort order: (arbitration_id, bus_name) so same-ID-different-bus
        # rows sit adjacent.
        self._rows_by_key: dict[tuple[str, int], FrameRecord] = {}
        self._sorted_keys: list[tuple[int, str]] = []
        self._last_ts: dict[tuple[str, int], float] = {}
        self._delta_for_key: dict[tuple[str, int], float | None] = {}
        self._cursors: dict[str, int] = {name: -1 for name in self._rings}
        self._signal_units: dict[tuple[str, int], dict[str, str]] = {}
        # Sidecar caches keyed by id(FrameRecord). _signal_keys stores the
        # decoded signal-name order so paint doesn't allocate a list per
        # call. Evict alongside the owning frame to keep id reuse safe.
        self._signal_keys: dict[int, tuple[str, ...]] = {}
        self._name_cache: dict[tuple[str, int], str] = {}
        # Overwrite mode: when each key last drained, and the keys gone quiet.
        self._arrival: dict[tuple[str, int], float] = {}
        self._stale_keys: set[tuple[str, int]] = set()
        self._rx_count = 0
        self._err_count = 0
        # Direction-column tints, and the dim of a row whose bus is closed
        # or whose Latest ID went quiet; refreshed on theme change.
        self._rx_color = QColor()
        self._tx_color = QColor()
        self._dim_color = QColor()
        # Held weakly: a lambda on the app-lifetime ThemeManager would keep
        # every replaced model, and its rows and rings, alive.
        themed_callback(self._refresh_colors)
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, 1000 // update_hz))
        self._timer.timeout.connect(self._drain)
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(_STALE_SWEEP_MS)
        self._stale_timer.timeout.connect(self._sweep_stale)

    # ---- Lifecycle ----

    def start(self) -> None:
        self._timer.start()
        self._stale_timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._stale_timer.stop()

    def drain_now(self) -> None:
        self._drain()

    # ---- Public API ----

    def frame_at(
        self,
        row_or_index: QModelIndex | QPersistentModelIndex | int,
    ) -> FrameRecord | None:
        if isinstance(row_or_index, int):
            row = row_or_index
        else:
            if not row_or_index.isValid() or row_or_index.parent().isValid():
                return None
            row = row_or_index.row()
        return self._frame_at_visible_row(row)

    def mode(self) -> Mode:
        return self._mode

    def set_mode(self, mode: Mode) -> None:
        if mode not in ("append", "overwrite"):
            raise ValueError(f"unknown mode: {mode!r}")
        if mode == self._mode:
            return
        self.beginResetModel()
        self._mode = mode
        self._reset_rows()
        self.endResetModel()
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, self.COL_TIME, self.COL_TIME)

    def clear(self) -> None:
        """Drop every row and zero the counters, for a new capture session.

        Cursors stay where they are, so no ring is replayed.
        """
        self.beginResetModel()
        self._reset_rows()
        self.endResetModel()
        self.reset_counters()

    def _reset_rows(self) -> None:
        self._rows.clear()
        self._row_of_frame.clear()
        self._row_offset = 0
        self._rows_by_key.clear()
        self._sorted_keys.clear()
        self._last_ts.clear()
        self._delta_for_key.clear()
        self._signal_keys.clear()
        self._arrival.clear()
        self._stale_keys.clear()

    def set_admit(self, admit: AdmitPredicate) -> None:
        """Swap the admission predicate.

        Triggered by the window after the tracked-tree's effective set
        changes. Drops every buffered row that the new predicate doesn't
        accept so the visible state agrees with the subscription set.
        """
        self._admit = admit
        if self._mode == "append":
            if any(not admit(f.bus_name, f.arbitration_id) for f in self._rows):
                self.beginResetModel()
                self._rows = deque(
                    (f for f in self._rows if admit(f.bus_name, f.arbitration_id)),
                    maxlen=self._max_rows,
                )
                self._row_of_frame.clear()
                self._row_offset = 0
                for i, f in enumerate(self._rows):
                    self._row_of_frame[id(f)] = i
                survivors = {id(f) for f in self._rows}
                for old_id in list(self._signal_keys):
                    if old_id not in survivors:
                        self._signal_keys.pop(old_id, None)
                self.endResetModel()
                self._rx_count = len(self._rows)
                self._err_count = sum(1 for f in self._rows if f.is_error_frame)
                self.counters_changed.emit(self._rx_count, self._err_count)
            return
        # Overwrite mode: drop stale (bus, aid) entries.
        stale = [(aid, bus) for (aid, bus) in self._sorted_keys if not admit(bus, aid)]
        if stale:
            self.beginResetModel()
            for aid, bus in stale:
                old = self._rows_by_key.pop((bus, aid), None)
                if old is not None:
                    self._signal_keys.pop(id(old), None)
                self._last_ts.pop((bus, aid), None)
                self._delta_for_key.pop((bus, aid), None)
                self._arrival.pop((bus, aid), None)
                self._stale_keys.discard((bus, aid))
            self._sorted_keys = [k for k in self._sorted_keys if admit(k[1], k[0])]
            self.endResetModel()

    def set_decoders(self, decoders: Mapping[str, Decoder | None]) -> None:
        """Replace the per-bus decoder map; invalidates lookup caches."""
        self._decoders = dict(decoders)
        self._signal_units.clear()
        self._name_cache.clear()

    def set_rings(self, rings: Mapping[str, RingBuffer]) -> None:
        """Replace the per-bus ring map when buses open or close.

        Rows of a bus that left stay, drawn dim. A cursor is kept only for
        the same ring object: a reopened bus has a new ring whose seq
        restarts at 0.
        """
        self._cursors = {
            name: self._cursors.get(name, -1) if self._rings.get(name) is ring else -1
            for name, ring in rings.items()
        }
        self._rings = dict(rings)

    def reset_counters(self) -> None:
        self._rx_count = 0
        self._err_count = 0
        self.counters_changed.emit(0, 0)

    def rx_count(self) -> int:
        return self._rx_count

    def err_count(self) -> int:
        return self._err_count

    def period_for(self, bus_name: str, arbitration_id: int) -> float | None:
        """Latest observed inter-arrival period for ``(bus, ID)``, in seconds.

        Returns ``None`` until at least two frames with this key have
        been admitted. Tracked in both append and overwrite modes, so
        the frame inspector can show a measured period regardless of
        which trace mode is active.
        """
        return self._delta_for_key.get((bus_name, arbitration_id))

    def latest_frame_for(self, bus_name: str, arbitration_id: int) -> FrameRecord | None:
        """Most recent ``FrameRecord`` admitted for ``(bus, ID)``.

        Tracked in both append and overwrite modes so the frame
        inspector can pin to a message ID and follow its latest
        payload as new arrivals drain in. Returns ``None`` if no
        frame for this key has been admitted yet.
        """
        return self._rows_by_key.get((bus_name, arbitration_id))

    # ---- Drain ----

    def _drain(self) -> None:
        admit = self._admit
        new_frames: list[FrameRecord] = []
        for name, ring in self._rings.items():
            frames, _dropped = ring.read_since(self._cursors[name])
            if not frames:
                continue
            self._cursors[name] = frames[-1].seq
            for f in frames:
                if not admit(f.bus_name, f.arbitration_id):
                    continue
                new_frames.append(f)
        if not new_frames:
            return
        new_frames.sort(key=lambda f: f.timestamp)

        if self._mode == "append":
            self._drain_append(new_frames)
        else:
            self._drain_overwrite(new_frames)

        self._rx_count += len(new_frames)
        self._err_count += sum(1 for f in new_frames if f.is_error_frame)
        self.counters_changed.emit(self._rx_count, self._err_count)

    def _drain_append(self, new_frames: list[FrameRecord]) -> None:
        # Track inter-arrival deltas and the latest frame per (bus, ID)
        # even in append mode so consumers like the frame inspector can
        # show observed period and follow the latest payload without
        # switching the trace to overwrite mode. Cardinality is bounded
        # by the number of distinct message IDs on the bus.
        for f in new_frames:
            key = (f.bus_name, f.arbitration_id)
            prev_ts = self._last_ts.get(key)
            self._delta_for_key[key] = None if prev_ts is None else f.timestamp - prev_ts
            self._last_ts[key] = f.timestamp
            self._rows_by_key[key] = f

        before = len(self._rows)
        overflow = max(0, before + len(new_frames) - self._max_rows)
        if overflow:
            self.beginRemoveRows(QModelIndex(), before - overflow, before - 1)
            for _ in range(overflow):
                dropped = self._rows.popleft()
                self._row_of_frame.pop(id(dropped), None)
                self._signal_keys.pop(id(dropped), None)
            self._row_offset += overflow
            self.endRemoveRows()
            before = len(self._rows)

        self.beginInsertRows(QModelIndex(), 0, len(new_frames) - 1)
        for i, f in enumerate(new_frames):
            self._rows.append(f)
            self._row_of_frame[id(f)] = before + i + self._row_offset
        self.endInsertRows()

    def _drain_overwrite(self, new_frames: list[FrameRecord]) -> None:
        # Overwrite mode is bounded by the number of distinct (bus, ID)
        # pairs on the bus (typically tens to hundreds), so eager decode
        # here is cheap. Keeping it eager lets the dataChanged emit below
        # invalidate expanded child rows so a pinned/expanded row tracks
        # the latest payload without needing the view to refetch via
        # rowCount.
        now = time.monotonic()
        for f in new_frames:
            key = (f.bus_name, f.arbitration_id)
            prev_ts = self._last_ts.get(key)
            self._delta_for_key[key] = None if prev_ts is None else f.timestamp - prev_ts
            self._last_ts[key] = f.timestamp
            self._arrival[key] = now
            self._stale_keys.discard(key)
            sort_key = (f.arbitration_id, f.bus_name)
            pos = bisect.bisect_left(self._sorted_keys, sort_key)
            if pos < len(self._sorted_keys) and self._sorted_keys[pos] == sort_key:
                old = self._rows_by_key.get(key)
                if old is not None and old is not f:
                    self._signal_keys.pop(id(old), None)
                self._ensure_decoded(f)
                self._rows_by_key[key] = f
                top = self.createIndex(pos, 0, None)
                bot = self.createIndex(pos, self.columnCount() - 1, None)
                self.dataChanged.emit(top, bot)
                decoded = f.decoded
                if decoded:
                    parent_idx = self.createIndex(pos, 0, None)
                    c_top = self.index(0, self.COL_TIME, parent_idx)
                    c_bot = self.index(len(decoded) - 1, self.COL_TIME, parent_idx)
                    if c_top.isValid() and c_bot.isValid():
                        self.dataChanged.emit(c_top, c_bot)
                continue
            self._ensure_decoded(f)
            self.beginInsertRows(QModelIndex(), pos, pos)
            self._sorted_keys.insert(pos, sort_key)
            self._rows_by_key[key] = f
            self.endInsertRows()

    def _sweep_stale(self) -> None:
        """Dim the Latest rows whose ID went quiet; repaints only on a change."""
        if not self._sorted_keys:
            return
        now = time.monotonic()
        stale = {
            key
            for key, arrived in self._arrival.items()
            if now - arrived
            > max(_STALE_MIN_S, _STALE_PERIODS * (self._delta_for_key.get(key) or 0.0))
        }
        if stale == self._stale_keys:
            return
        self._stale_keys = stale
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(self._sorted_keys) - 1, self.COL_NAME),
            [Qt.ItemDataRole.ForegroundRole],
        )

    # ---- QAbstractItemModel ----

    def _frame_at_visible_row(self, row: int) -> FrameRecord | None:
        if self._mode == "overwrite":
            if not (0 <= row < len(self._sorted_keys)):
                return None
            aid, bus = self._sorted_keys[row]
            return self._rows_by_key.get((bus, aid))
        if not (0 <= row < len(self._rows)):
            return None
        backing = len(self._rows) - 1 - row
        return self._rows[backing]

    def _top_row_count(self) -> int:
        return len(self._sorted_keys) if self._mode == "overwrite" else len(self._rows)

    def index(
        self,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            if 0 <= row < self._top_row_count():
                return self.createIndex(row, column, None)
            return QModelIndex()
        if parent.parent().isValid():
            return QModelIndex()
        parent_frame = self._frame_at_visible_row(parent.row())
        if parent_frame is None:
            return QModelIndex()
        decoded = self._ensure_decoded(parent_frame)
        if not decoded:
            return QModelIndex()
        if not (0 <= row < len(decoded)):
            return QModelIndex()
        return self.createIndex(row, column, parent_frame)

    def parent(  # type: ignore[override]
        self,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        ptr = index.internalPointer()
        if not isinstance(ptr, FrameRecord):
            return QModelIndex()
        if self._mode == "overwrite":
            sort_key = (ptr.arbitration_id, ptr.bus_name)
            pos = bisect.bisect_left(self._sorted_keys, sort_key)
            if 0 <= pos < len(self._sorted_keys) and self._sorted_keys[pos] == sort_key:
                return self.createIndex(pos, 0, None)
            return QModelIndex()
        nominal = self._row_of_frame.get(id(ptr))
        if nominal is None:
            return QModelIndex()
        backing = nominal - self._row_offset
        if not (0 <= backing < len(self._rows)):
            return QModelIndex()
        visible = len(self._rows) - 1 - backing
        return self.createIndex(visible, 0, None)

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        if not parent.isValid():
            return self._top_row_count()
        if parent.parent().isValid():
            return 0
        frame = self._frame_at_visible_row(parent.row())
        if frame is None:
            return 0
        decoded = self._ensure_decoded(frame)
        return len(decoded) if decoded else 0

    def columnCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        return len(self.COLUMNS)

    def hasChildren(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> bool:
        if not parent.isValid():
            return self._top_row_count() > 0
        if parent.parent().isValid():
            return False
        frame = self._frame_at_visible_row(parent.row())
        if frame is None:
            return False
        if frame.decoded:
            return True
        # Show the expander chevron for DBC-known IDs without forcing a
        # decode — actual decode happens when the user expands and Qt
        # calls rowCount.
        decoder = self._decoders.get(frame.bus_name)
        return decoder is not None and decoder.has_message(frame.arbitration_id)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.COLUMNS)
        ):
            if section == self.COL_TIME and self._mode == "overwrite":
                return "Δt"
            return self.COLUMNS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        col = index.column()
        parent = index.parent()
        if role == Qt.ItemDataRole.ForegroundRole:
            # Top-level frame rows only: dim when their bus is closed or their
            # Latest ID went quiet; otherwise only the direction cell is tinted.
            if parent.isValid():
                return None
            frame = self._frame_at_visible_row(index.row())
            if frame is None:
                return None
            if (
                frame.bus_name not in self._rings
                or (frame.bus_name, frame.arbitration_id) in self._stale_keys
            ):
                return self._dim_color
            if col != self.COL_DIR:
                return None
            return self._rx_color if frame.is_rx else self._tx_color
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if not parent.isValid():
            frame = self._frame_at_visible_row(index.row())
            if frame is None:
                return None
            return self._frame_cell(frame, col)
        if col != self.COL_TIME:
            return None
        parent_frame = self._frame_at_visible_row(parent.row())
        if parent_frame is None:
            return None
        decoded = self._ensure_decoded(parent_frame)
        if not decoded:
            return None
        keys = self._signal_keys.get(id(parent_frame))
        if keys is None or not (0 <= index.row() < len(keys)):
            return None
        sig_name = keys[index.row()]
        return self._format_signal(
            parent_frame.bus_name,
            parent_frame.arbitration_id,
            sig_name,
            decoded[sig_name],
        )

    # ---- Helpers ----

    def _refresh_colors(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        self._rx_color = QColor(tokens["rx"])
        self._tx_color = QColor(tokens["tx"])
        self._dim_color = QColor(tokens["fg_3"])

    def _ensure_decoded(self, f: FrameRecord) -> dict[str, Any] | None:
        if f.decoded is not None:
            return f.decoded
        decoder = self._decoders.get(f.bus_name)
        if decoder is None:
            return None
        decoded = decoder.decode(f.arbitration_id, f.data)
        f.decoded = decoded
        if decoded:
            self._signal_keys[id(f)] = tuple(decoded.keys())
        return decoded

    def _frame_cell(self, f: FrameRecord, col: int) -> Any:
        if col == self.COL_TIME:
            if self._mode == "overwrite":
                delta = self._delta_for_key.get((f.bus_name, f.arbitration_id))
                return "—" if delta is None else f"{delta:.6f}"
            t0 = self._bus_reader.session_t0 if self._bus_reader else None
            rel = f.timestamp if t0 is None else f.timestamp - t0
            return f"{rel:.6f}"
        if col == self.COL_BUS:
            return f.bus_name
        if col == self.COL_DIR:
            return "RX" if f.is_rx else "TX"
        if col == self.COL_ID:
            return f"{f.arbitration_id:08X}" if f.is_extended_id else f"{f.arbitration_id:03X}"
        if col == self.COL_DLC:
            return str(len(f.data))
        if col == self.COL_DATA:
            return f.data.hex(" ").upper()
        if col == self.COL_NAME:
            key = (f.bus_name, f.arbitration_id)
            cached = self._name_cache.get(key)
            if cached is None:
                decoder = self._decoders.get(f.bus_name)
                cached = "" if decoder is None else (decoder.message_name(f.arbitration_id) or "")
                self._name_cache[key] = cached
            return cached
        return None

    def _format_signal(
        self,
        bus_name: str,
        arbitration_id: int,
        name: str,
        value: Any,
    ) -> str:
        unit = self._unit_for(bus_name, arbitration_id, name)
        if isinstance(value, float):
            text = f"{value:g}"
        else:
            text = str(value)
        if unit:
            return f"    • {name} = {text} {unit}"
        return f"    • {name} = {text}"

    def _unit_for(self, bus_name: str, arbitration_id: int, signal_name: str) -> str:
        key = (bus_name, arbitration_id)
        cache = self._signal_units.get(key)
        if cache is None:
            cache = {}
            decoder = self._decoders.get(bus_name)
            if decoder is not None:
                try:
                    msg = decoder.database.get_message_by_frame_id(arbitration_id)
                except KeyError:
                    msg = None
                if msg is not None:
                    for sig in msg.signals:
                        cache[sig.name] = sig.unit or ""
            self._signal_units[key] = cache
        return cache.get(signal_name, "")
