# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Merged trace tree model — frames as top-level, signals as children.

Replaces ``MessageTraceTreeModel`` and ``BusTraceTreeModel``. The model
is parameterised by an admission predicate (``admits(bus, aid, is_error=)``)
supplied by the owning window from its ``TrackedTreeModel.effective()``
snapshot; the model itself stays agnostic to the shape of the subscription.

Error frames are never decoded or expandable. Their Name cell reads "Error
frame", with "×N" when the capture worker sampled one to stand for N, and
the ERR counter sums those N.

Two display modes:

- ``append`` (default): every admitted frame is kept, oldest evicted at
  ``max_rows``, but while running only the newest ``live_rows`` of them are
  rows, newest at row 0. Pausing shows the whole history; resuming jumps
  back to live and skips the frames that arrived while paused. Time column
  is the absolute timestamp.
- ``overwrite``: one row per ``(bus, arbitration_id)`` pair, sorted by
  ``(arbitration_id, bus_name)``. New arrivals update in place. Time
  column is the delta to the previous arrival of the same key
  (``—`` for the first sighting).

The text filter (``set_text_filter``) lives in the model too: a row shows
when its ID, Name or Bus cell contains the text. Never put a proxy model over
this one: QTreeView lays out every top-level row on each insert at the root,
and a proxy maps each row through ``rowCount``, which decodes. ``index``,
``hasChildren`` and ``rowCount`` never decode a collapsed row.

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
import itertools
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
    from collections.abc import Iterable, Mapping

    from ithildin.app_state.bus_reader import BusReader
    from ithildin.core.decoder import Decoder

Mode = Literal["append", "overwrite"]


class AdmitPredicate(Protocol):
    def __call__(self, bus_name: str, arbitration_id: int, *, is_error: bool = False) -> bool: ...


class _AdmitNone:
    def __call__(self, bus_name: str, arbitration_id: int, *, is_error: bool = False) -> bool:
        del bus_name, arbitration_id, is_error
        return False


_admit_none: AdmitPredicate = _AdmitNone()

_LIVE_ROWS = 1_000
"""Rows a running Stream trace shows: each insert at the root makes QTreeView
lay out every top-level row again."""
_STALE_MIN_S = 2.0
_STALE_PERIODS = 3
"""A Latest row goes stale after ``max(_STALE_MIN_S, _STALE_PERIODS * period)``
without a new frame."""
_STALE_SWEEP_MS = 1000


def _id_text(f: FrameRecord) -> str:
    return f"{f.arbitration_id:08X}" if f.is_extended_id else f"{f.arbitration_id:03X}"


class TraceTreeModel(QAbstractItemModel):
    COLUMNS = ("Time", "Bus", "Dir", "ID", "DLC", "Data", "Name")

    COL_TIME = 0
    COL_BUS = 1
    COL_DIR = 2
    COL_ID = 3
    COL_DLC = 4
    COL_DATA = 5
    COL_NAME = 6

    counters_changed = Signal(int, int, int)  # rx_total, err_total, lost_total

    def __init__(
        self,
        rings: Mapping[str, RingBuffer],
        decoders: Mapping[str, Decoder | None] | None = None,
        admit: AdmitPredicate | None = None,
        max_rows: int = 100_000,
        live_rows: int = _LIVE_ROWS,
        update_hz: int = 30,
        bus_reader: BusReader | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if max_rows <= 0:
            raise ValueError("max_rows must be > 0")
        if live_rows <= 0:
            raise ValueError("live_rows must be > 0")
        if update_hz <= 0:
            raise ValueError("update_hz must be > 0")
        self._rings = dict(rings)
        self._decoders: dict[str, Decoder | None] = dict(decoders) if decoders else {}
        self._admit: AdmitPredicate = admit if admit is not None else _admit_none
        self._bus_reader = bus_reader
        self._max_rows = max_rows
        self._live_rows = live_rows
        self._mode: Mode = "append"
        self._paused = False
        # Text filter: the needle is lower-cased unless matching case.
        self._needle = ""
        self._case_sensitive = False
        # Append-mode storage. ``_rows`` holds every admitted frame, oldest
        # first, evicted explicitly at ``max_rows``. ``_view`` holds the
        # frames shown as rows, a subsequence of ``_rows`` in the same order,
        # so visible row r is ``_view[-1 - r]``.
        self._rows: deque[FrameRecord] = deque()
        self._view: list[FrameRecord] = []
        # id(frame) -> nominal position in _view; real = nominal - view_offset.
        # The offset trick keeps parent() lookups O(1) across evictions.
        self._view_pos: dict[int, int] = {}
        self._view_offset = 0
        # Overwrite-mode storage: keyed by (bus_name, arbitration_id).
        # ``_sorted_keys`` is the canonical visible-row → key mapping, for
        # the keys whose frame passes the text filter. Sort order:
        # (arbitration_id, bus_name) so same-ID-different-bus rows sit
        # adjacent.
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
        self._expandable_by_key: dict[tuple[str, int], bool] = {}
        # Keys whose signal rows the view has asked for; overwrite mode
        # decodes a new frame only for these.
        self._decoded_keys: set[tuple[str, int]] = set()
        # Signal rows carry a nonzero internal id naming their frame row, top
        # rows 0: id(frame) in append mode, a number per key in overwrite mode.
        # Never a pointer: the view keeps a Latest row's signal-row indexes
        # across frame replacements, and a pointer to a frame the ring has
        # since freed crashes the next paint.
        self._key_ids: dict[tuple[str, int], int] = {}
        self._keys_by_id: dict[int, tuple[str, int]] = {}
        self._next_key_id = itertools.count(1)
        # Overwrite mode: when each key last drained, and the keys gone quiet.
        self._arrival: dict[tuple[str, int], float] = {}
        self._stale_keys: set[tuple[str, int]] = set()
        self._rx_count = 0
        self._err_count = 0
        self._lost_count = 0
        # Direction-column tints, the error-frame Name tint, and the dim of a
        # row whose bus is closed or whose Latest ID went quiet; refreshed on
        # theme change.
        self._rx_color = QColor()
        self._tx_color = QColor()
        self._err_color = QColor()
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

    def set_paused(self, paused: bool) -> None:
        """Freeze the trace on its full history, or jump back to live.

        Frames that arrive while paused are skipped, not shown on resume.
        """
        if paused == self._paused:
            return
        self._paused = paused
        if paused:
            self.stop()
            older = self._shown(self._rows)
            del older[len(older) - len(self._view) :]
            if older:
                # Below the live rows, so scroll position and selection stay.
                n = len(self._view)
                self.beginInsertRows(QModelIndex(), n, n + len(older) - 1)
                self._view[:0] = older
                self._view_offset -= len(older)
                for i, f in enumerate(older):
                    self._view_pos[id(f)] = self._view_offset + i
                self.endInsertRows()
            return
        self._remove_oldest(len(self._view) - self._live_rows)
        for name, ring in self._rings.items():
            self._cursors[name] = ring.total_seq - 1
        # Else each ID's next delta spans the pause and the frames it skipped.
        self._last_ts.clear()
        self.start()

    def set_text_filter(self, text: str, case_sensitive: bool) -> None:
        """Show only the rows whose ID, Name or Bus cell contains ``text``.

        A literal substring; signal rows follow their frame. Stored frames
        stay, so clearing the filter brings every row back.
        """
        needle = text if case_sensitive else text.lower()
        changed = needle != self._needle or (
            bool(needle) and case_sensitive != self._case_sensitive
        )
        self._needle = needle
        self._case_sensitive = case_sensitive
        if changed:
            self._rebuild_shown()

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
        self._view.clear()
        self._view_pos.clear()
        self._view_offset = 0
        self._rows_by_key.clear()
        self._sorted_keys.clear()
        self._last_ts.clear()
        self._delta_for_key.clear()
        self._signal_keys.clear()
        self._decoded_keys.clear()
        self._key_ids.clear()
        self._keys_by_id.clear()
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
            kept = deque(
                f
                for f in self._rows
                if admit(f.bus_name, f.arbitration_id, is_error=f.is_error_frame)
            )
            if len(kept) == len(self._rows):
                return
            self._rows = kept
            self._rebuild_shown()
            survivors = {id(f) for f in kept}
            for old_id in list(self._signal_keys):
                if old_id not in survivors:
                    self._signal_keys.pop(old_id, None)
            self._rx_count = len(kept)
            self._err_count = sum(f.coalesced for f in kept if f.is_error_frame)
            self.counters_changed.emit(self._rx_count, self._err_count, self._lost_count)
            return
        # Overwrite mode: drop the (bus, aid) entries, shown or filtered out.
        stale = [
            key
            for key, f in self._rows_by_key.items()
            if not admit(key[0], key[1], is_error=f.is_error_frame)
        ]
        if stale:
            self.beginResetModel()
            for key in stale:
                self._signal_keys.pop(id(self._rows_by_key.pop(key)), None)
                self._last_ts.pop(key, None)
                self._delta_for_key.pop(key, None)
                self._arrival.pop(key, None)
                self._stale_keys.discard(key)
                self._decoded_keys.discard(key)
            gone = {(aid, bus) for bus, aid in stale}
            self._sorted_keys = [k for k in self._sorted_keys if k not in gone]
            self.endResetModel()

    def set_decoders(self, decoders: Mapping[str, Decoder | None]) -> None:
        """Replace the per-bus decoder map; invalidates lookup caches."""
        self._decoders = dict(decoders)
        self._signal_units.clear()
        self._name_cache.clear()
        self._expandable_by_key.clear()
        if self._needle:
            # Message names, which the filter matches, may have changed.
            self._rebuild_shown()

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
        self._lost_count = 0
        self.counters_changed.emit(0, 0, 0)

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
        lost = self._lost_count
        new_frames: list[FrameRecord] = []
        for name, ring in self._rings.items():
            cursor = self._cursors[name]
            frames, dropped = ring.read_since(cursor)
            # A fresh cursor on a ring that has already wrapped missed nothing.
            if cursor >= 0:
                self._lost_count += dropped
            if not frames:
                continue
            self._cursors[name] = frames[-1].seq
            for f in frames:
                if not admit(f.bus_name, f.arbitration_id, is_error=f.is_error_frame):
                    continue
                new_frames.append(f)
        if new_frames:
            new_frames.sort(key=lambda f: f.timestamp)
            if self._mode == "append":
                self._drain_append(new_frames)
            else:
                self._drain_overwrite(new_frames)
            self._rx_count += len(new_frames)
            self._err_count += sum(f.coalesced for f in new_frames if f.is_error_frame)
        elif self._lost_count == lost:
            return
        self.counters_changed.emit(self._rx_count, self._err_count, self._lost_count)

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

        # Costs O(new frames + live_rows) however deep the history is.
        rows, view = self._rows, self._view
        new_frames = new_frames[-self._max_rows :]
        evict = max(0, len(rows) + len(new_frames) - self._max_rows)
        gone = 0
        for f in itertools.islice(rows, evict):
            if gone < len(view) and view[gone] is f:
                gone += 1
        shown = self._shown(new_frames)[-self._live_rows :]
        self._remove_oldest(max(gone, len(view) + len(shown) - self._live_rows))
        for _ in range(evict):
            self._signal_keys.pop(id(rows.popleft()), None)
        rows.extend(new_frames)
        if not shown:
            return
        self.beginInsertRows(QModelIndex(), 0, len(shown) - 1)
        base = self._view_offset + len(view)
        for i, f in enumerate(shown):
            self._view_pos[id(f)] = base + i
        view.extend(shown)
        self.endInsertRows()

    def _remove_oldest(self, n: int) -> None:
        """Drop the ``n`` oldest shown frames, the bottom rows."""
        if n <= 0:
            return
        view = self._view
        self.beginRemoveRows(QModelIndex(), len(view) - n, len(view) - 1)
        for f in view[:n]:
            del self._view_pos[id(f)]
        del view[:n]
        self._view_offset += n
        self.endRemoveRows()

    def _drain_overwrite(self, new_frames: list[FrameRecord]) -> None:
        now = time.monotonic()
        latest: dict[tuple[str, int], FrameRecord] = {}
        for f in new_frames:
            key = (f.bus_name, f.arbitration_id)
            prev_ts = self._last_ts.get(key)
            self._delta_for_key[key] = None if prev_ts is None else f.timestamp - prev_ts
            self._last_ts[key] = f.timestamp
            self._arrival[key] = now
            latest[key] = f
        self._stale_keys.difference_update(latest)
        root = QModelIndex()
        for key, f in latest.items():
            old = self._rows_by_key.get(key)
            sort_key = (f.arbitration_id, f.bus_name)
            pos = bisect.bisect_left(self._sorted_keys, sort_key)
            shown = pos < len(self._sorted_keys) and self._sorted_keys[pos] == sort_key
            matches = self._matches(f)
            if shown and matches:
                self._replace_shown(pos, key, f)
            elif shown:
                # Its new Name cell no longer passes the text filter.
                self.beginRemoveRows(root, pos, pos)
                del self._sorted_keys[pos]
                self._rows_by_key[key] = f
                self.endRemoveRows()
            elif matches:
                self.beginInsertRows(root, pos, pos)
                self._sorted_keys.insert(pos, sort_key)
                self._rows_by_key[key] = f
                self.endInsertRows()
            else:
                self._rows_by_key[key] = f
            if old is not None:
                self._signal_keys.pop(id(old), None)

    def _replace_shown(self, pos: int, key: tuple[str, int], f: FrameRecord) -> None:
        top = self.createIndex(pos, 0)
        old_n = new_n = 0
        if key in self._decoded_keys:
            # The view has seen this row's signal rows: decode the new frame
            # and announce a changed count (an error frame has none, a
            # multiplexed message varies).
            old_n = len(self._ensure_decoded(self._rows_by_key[key]) or ())
            new_n = len(self._ensure_decoded(f) or ())
        if new_n < old_n:
            self.beginRemoveRows(top, new_n, old_n - 1)
        elif new_n > old_n:
            self.beginInsertRows(top, old_n, new_n - 1)
        self._rows_by_key[key] = f
        if new_n < old_n:
            self.endRemoveRows()
        elif new_n > old_n:
            self.endInsertRows()
        self.dataChanged.emit(top, self.createIndex(pos, self.columnCount() - 1))
        if new_n:
            child_id = self._child_id(f)
            self.dataChanged.emit(
                self.createIndex(0, self.COL_TIME, child_id),
                self.createIndex(new_n - 1, self.COL_TIME, child_id),
            )

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

    # ---- Text filter ----

    def _matches(self, f: FrameRecord) -> bool:
        needle = self._needle
        if not needle:
            return True
        cells = (_id_text(f), self._name_for(f), f.bus_name)
        if self._case_sensitive:
            return any(needle in cell for cell in cells)
        return any(needle in cell.lower() for cell in cells)

    def _shown(self, frames: Iterable[FrameRecord]) -> list[FrameRecord]:
        if not self._needle:
            return list(frames)
        return [f for f in frames if self._matches(f)]

    def _rebuild_shown(self) -> None:
        """Rebuild the rows from the stored frames, e.g. for a new filter.

        A layout change, not a reset, so the rows that stay keep their
        expansion and selection.
        """
        self.layoutAboutToBeChanged.emit()
        before = self.persistentIndexList()
        # Each index's frame row, named as its signal rows name it, so that
        # _parent_row finds the row again after the rebuild.
        owners: list[int] = []
        for index in before:
            frame = self.frame_at(index)
            owners.append(index.internalId() if frame is None else self._child_id(frame))
        if self._mode == "overwrite":
            self._sorted_keys = sorted(
                (aid, bus) for (bus, aid), f in self._rows_by_key.items() if self._matches(f)
            )
        else:
            if self._paused:
                view = self._shown(self._rows)
            else:
                newest = filter(self._matches, reversed(self._rows))
                view = list(itertools.islice(newest, self._live_rows))
                view.reverse()
            self._view = view
            self._view_offset = 0
            self._view_pos = {id(f): i for i, f in enumerate(view)}
        after: list[QModelIndex] = []
        for index, owner in zip(before, owners, strict=True):
            row = self._parent_row(owner)
            if row is None:
                after.append(QModelIndex())
            elif index.internalId():
                after.append(self.createIndex(index.row(), index.column(), owner))
            else:
                after.append(self.createIndex(row, index.column()))
        self.changePersistentIndexList(before, after)
        self.layoutChanged.emit()

    # ---- QAbstractItemModel ----

    def _frame_at_visible_row(self, row: int) -> FrameRecord | None:
        if self._mode == "overwrite":
            if not (0 <= row < len(self._sorted_keys)):
                return None
            aid, bus = self._sorted_keys[row]
            return self._rows_by_key.get((bus, aid))
        if not (0 <= row < len(self._view)):
            return None
        return self._view[-1 - row]

    def _top_row_count(self) -> int:
        return len(self._sorted_keys) if self._mode == "overwrite" else len(self._view)

    def _child_id(self, frame: FrameRecord) -> int:
        if self._mode == "append":
            return id(frame)
        key = (frame.bus_name, frame.arbitration_id)
        child_id = self._key_ids.get(key)
        if child_id is None:
            child_id = next(self._next_key_id)
            self._key_ids[key] = child_id
            self._keys_by_id[child_id] = key
        return child_id

    def _parent_row(self, child_id: int) -> int | None:
        """Visible row of the frame whose signal rows carry ``child_id``."""
        if self._mode == "overwrite":
            key = self._keys_by_id.get(child_id)
            if key is None:
                return None
            sort_key = (key[1], key[0])
            pos = bisect.bisect_left(self._sorted_keys, sort_key)
            if pos < len(self._sorted_keys) and self._sorted_keys[pos] == sort_key:
                return pos
            return None
        nominal = self._view_pos.get(child_id)
        if nominal is None:
            return None
        return len(self._view) - 1 - (nominal - self._view_offset)

    def index(
        self,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> QModelIndex:
        if not 0 <= column < len(self.COLUMNS):
            return QModelIndex()
        if not parent.isValid():
            if 0 <= row < self._top_row_count():
                return self.createIndex(row, column)
            return QModelIndex()
        if parent.internalId():
            return QModelIndex()
        parent_frame = self._frame_at_visible_row(parent.row())
        if parent_frame is None:
            return QModelIndex()
        decoded = self._ensure_decoded(parent_frame)
        if not decoded or not (0 <= row < len(decoded)):
            return QModelIndex()
        return self.createIndex(row, column, self._child_id(parent_frame))

    def parent(  # type: ignore[override]
        self,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        child_id = index.internalId()
        if not child_id:
            return QModelIndex()
        row = self._parent_row(child_id)
        return QModelIndex() if row is None else self.createIndex(row, 0)

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        if not parent.isValid():
            return self._top_row_count()
        if parent.internalId():
            return 0
        # Qt asks only for an expanded row's count: the one place to decode.
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
        if parent.internalId():
            return False
        frame = self._frame_at_visible_row(parent.row())
        # A frame decoded under a DBC since unbound keeps its signal rows.
        return frame is not None and (bool(frame.decoded) or self._expandable(frame))

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
        child_id = index.internalId()
        if child_id:
            if role != Qt.ItemDataRole.DisplayRole or col != self.COL_TIME:
                return None
            parent_row = self._parent_row(child_id)
            if parent_row is None:
                return None
            parent_frame = self._frame_at_visible_row(parent_row)
            if parent_frame is None:
                return None
            decoded = self._ensure_decoded(parent_frame)
            if not decoded:
                return None
            keys = self._signal_keys[id(parent_frame)]
            if not (0 <= index.row() < len(keys)):
                return None
            sig_name = keys[index.row()]
            return self._format_signal(
                parent_frame.bus_name,
                parent_frame.arbitration_id,
                sig_name,
                decoded[sig_name],
            )
        if role == Qt.ItemDataRole.ForegroundRole:
            # Top-level frame rows only: dim when their bus is closed or their
            # Latest ID went quiet; otherwise only the direction cell and an
            # error frame's name are tinted.
            frame = self._frame_at_visible_row(index.row())
            if frame is None:
                return None
            if (
                frame.bus_name not in self._rings
                or (frame.bus_name, frame.arbitration_id) in self._stale_keys
            ):
                return self._dim_color
            if col == self.COL_NAME and frame.is_error_frame:
                return self._err_color
            if col != self.COL_DIR:
                return None
            return self._rx_color if frame.is_rx else self._tx_color
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        frame = self._frame_at_visible_row(index.row())
        if frame is None:
            return None
        return self._frame_cell(frame, col)

    # ---- Helpers ----

    def _refresh_colors(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        self._rx_color = QColor(tokens["rx"])
        self._tx_color = QColor(tokens["tx"])
        self._err_color = QColor(tokens["err"])
        self._dim_color = QColor(tokens["fg_3"])

    def _expandable(self, f: FrameRecord) -> bool:
        """Whether a row gets an expander, from the DBC and without decoding."""
        if f.is_error_frame:
            return False
        key = (f.bus_name, f.arbitration_id)
        known = self._expandable_by_key.get(key)
        if known is None:
            decoder = self._decoders.get(f.bus_name)
            known = decoder is not None and decoder.has_message(f.arbitration_id)
            self._expandable_by_key[key] = known
        return known

    def _ensure_decoded(self, f: FrameRecord) -> dict[str, Any] | None:
        if f.is_error_frame:
            return None
        decoded = f.decoded
        if decoded is None:
            decoder = self._decoders.get(f.bus_name)
            if decoder is None:
                return None
            decoded = decoder.decode(f.arbitration_id, f.data)
            f.decoded = decoded
        self._decoded_keys.add((f.bus_name, f.arbitration_id))
        # Also when another consumer decoded the frame first.
        if decoded and id(f) not in self._signal_keys:
            self._signal_keys[id(f)] = tuple(decoded)
        return decoded

    def _name_for(self, f: FrameRecord) -> str:
        """The Name cell, which the text filter also matches."""
        if f.is_error_frame:
            return "Error frame" if f.coalesced == 1 else f"Error frame ×{f.coalesced}"
        key = (f.bus_name, f.arbitration_id)
        cached = self._name_cache.get(key)
        if cached is None:
            decoder = self._decoders.get(f.bus_name)
            cached = "" if decoder is None else (decoder.message_name(f.arbitration_id) or "")
            self._name_cache[key] = cached
        return cached

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
            return _id_text(f)
        if col == self.COL_DLC:
            return str(len(f.data))
        if col == self.COL_DATA:
            return f.data.hex(" ").upper()
        if col == self.COL_NAME:
            return self._name_for(f)
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
