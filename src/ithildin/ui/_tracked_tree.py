# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tracked subscription tree for the merged Trace view.

Three node kinds: ``bus``, ``dbc``, ``msg``. The tree mirrors the natural
data model — buses contain DBCs, DBCs contain messages. Each tracked
root admits frames at its level:

- ``bus`` admits every frame on that bus (decoded or not).
- ``dbc`` admits frames whose arbitration ID is defined by the DBC.
- ``msg`` admits frames whose arbitration ID equals that message's.

After every tree mutation an ``_Effective`` snapshot is recomputed so
the model's hot-path ``_admit`` is a couple of set lookups.

Overlap rules:

- Redundant drop (msg under tracked dbc, dbc under tracked bus): no-op,
  the existing row is highlighted briefly.
- Superseding drop (bus dropped while dbc/msg children are tracked under
  it; dbc dropped while individual messages of it are tracked): the
  narrower nodes are removed, replaced by the broader one; the owning
  window surfaces a banner.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QMenu,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ithildin.ui._drag_payload import (
    BusPayload,
    DbcPayload,
    MessagePayload,
    Payload,
    SignalPayload,
)
from ithildin.ui._trace_drop import TraceDropArea
from ithildin.ui.fonts import tabular_font

if TYPE_CHECKING:
    from ithildin.app_state.database import Database


NodeKind = Literal["bus", "dbc", "msg"]

_NODE_KIND_ROLE = Qt.ItemDataRole.UserRole + 1
_BUS_NAME_ROLE = Qt.ItemDataRole.UserRole + 2
_DBC_PATH_ROLE = Qt.ItemDataRole.UserRole + 3
_MSG_ID_ROLE = Qt.ItemDataRole.UserRole + 4
_MSG_NAME_ROLE = Qt.ItemDataRole.UserRole + 5
# True only for nodes the user explicitly subscribed to. A bus row may
# exist as a synthetic parent for a tracked DBC; that doesn't admit
# every frame on the bus — only an explicit bus subscription does.
_EXPLICIT_ROLE = Qt.ItemDataRole.UserRole + 6

_HIGHLIGHT_FLASH_MS = 600

# Sentinel keys for the dedup dict. Each row has exactly one key.
_NodeKey = tuple[str, str, str, int]


def _bus_key(bus_name: str) -> _NodeKey:
    return ("bus", bus_name, "", 0)


def _dbc_key(bus_name: str, path: Path) -> _NodeKey:
    return ("dbc", bus_name, str(path), 0)


def _msg_key(bus_name: str, path: Path, arbitration_id: int) -> _NodeKey:
    return ("msg", bus_name, str(path), int(arbitration_id))


@dataclass(frozen=True, slots=True)
class _Effective:
    """O(1)-checkable subscription snapshot.

    Each per-bus dict only contains entries for buses with subscriptions
    of that flavour; absence is the same as the empty set.
    """

    bus_all: frozenset[str] = frozenset()
    bus_dbc_ids: dict[str, frozenset[int]] = field(default_factory=dict)
    bus_msg_ids: dict[str, frozenset[int]] = field(default_factory=dict)

    def admits(self, bus_name: str, arbitration_id: int) -> bool:
        if bus_name in self.bus_all:
            return True
        ids = self.bus_dbc_ids.get(bus_name)
        if ids is not None and arbitration_id in ids:
            return True
        ids = self.bus_msg_ids.get(bus_name)
        if ids is not None and arbitration_id in ids:
            return True
        return False


@dataclass(frozen=True, slots=True)
class _MsgEntry:
    bus_name: str
    dbc_path: Path
    message_id: int
    message_name: str


@dataclass(frozen=True, slots=True)
class _DbcEntry:
    bus_name: str
    dbc_path: Path
    explicit: bool = True


@dataclass(frozen=True, slots=True)
class _BusEntry:
    bus_name: str
    explicit: bool = True


_Entry = _BusEntry | _DbcEntry | _MsgEntry


class TrackedTreeModel(QStandardItemModel):
    """BUS → DBC → MESSAGE subscription tree.

    The model is the source of truth for what the Trace view records.
    ``effective_set`` is recomputed on every mutation and exposed via
    ``effective()``; consumers connect to ``effective_changed`` to know
    when to re-query.

    Drop resolution is partial — the model only knows the structural
    rules (where a payload type fits in the hierarchy). Resolving a
    ``MessagePayload`` to a specific ``(bus, dbc)`` pair requires the
    project's ``Database``, so that lookup lives on the pane.
    """

    effective_changed = Signal()
    superseded = Signal(str)  # human-readable description for the window banner

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._effective = _Effective()
        # Maps the dedup key to its top-level / child item for O(1) presence checks.
        self._items: dict[_NodeKey, QStandardItem] = {}

    # ---- Public API ----

    def effective(self) -> _Effective:
        return self._effective

    def entries(self) -> list[_Entry]:
        """Flat, deterministic list of every node in the tree.

        Order is depth-first: bus, then each of its DBC children, then
        each of those DBC's MSG children. Used for save_state and to
        let the window iterate over what's currently tracked.
        """
        out: list[_Entry] = []
        for r in range(self.rowCount()):
            bus_item = self.item(r)
            if bus_item is None:
                continue
            bus_name = _bus_name_of(bus_item)
            if not bus_name:
                continue
            out.append(_BusEntry(bus_name=bus_name, explicit=_is_explicit(bus_item)))
            for dr in range(bus_item.rowCount()):
                dbc_item = bus_item.child(dr)
                if dbc_item is None:
                    continue
                kind = _kind_of(dbc_item)
                if kind == "dbc":
                    path = _dbc_path_of(dbc_item)
                    if path is None:
                        continue
                    out.append(
                        _DbcEntry(
                            bus_name=bus_name,
                            dbc_path=path,
                            explicit=_is_explicit(dbc_item),
                        )
                    )
                    for mr in range(dbc_item.rowCount()):
                        msg_item = dbc_item.child(mr)
                        if msg_item is None:
                            continue
                        aid = _msg_id_of(msg_item)
                        if aid is None:
                            continue
                        out.append(
                            _MsgEntry(
                                bus_name=bus_name,
                                dbc_path=path,
                                message_id=aid,
                                message_name=_msg_name_of(msg_item) or "",
                            )
                        )
                elif kind == "msg":
                    aid = _msg_id_of(dbc_item)
                    if aid is None:
                        continue
                    out.append(
                        _MsgEntry(
                            bus_name=bus_name,
                            dbc_path=Path(""),
                            message_id=aid,
                            message_name=_msg_name_of(dbc_item) or "",
                        )
                    )
        return out

    def clear_tree(self) -> None:
        self.removeRows(0, self.rowCount())
        self._items.clear()
        self._recompute()

    def add_bus(self, bus_name: str) -> tuple[QStandardItem | None, str | None]:
        """Add an explicit top-level bus subscription. Returns (item, supersede_msg).

        If a synthetic bus row exists as a parent for a dbc/msg subscription,
        promoting it to explicit triggers the supersedence cleanup (its
        narrower children are removed because the bus now admits everything).
        """
        if not bus_name:
            return None, None
        key = _bus_key(bus_name)
        existing = self._items.get(key)
        if existing is not None and _is_explicit(existing):
            return existing, None
        removed = self._remove_narrower_than_bus(bus_name)
        if existing is not None:
            existing.setData(True, _EXPLICIT_ROLE)
            item = existing
        else:
            item = _make_bus_item(bus_name, explicit=True)
            self.appendRow(item)
            self._items[key] = item
        msg = (
            f"Promoted to bus subscription · {bus_name} "
            f"(replaced {removed} narrower subscription{'s' if removed != 1 else ''})"
            if removed
            else None
        )
        self._recompute()
        if msg:
            self.superseded.emit(msg)
        return item, msg

    def add_dbc(
        self,
        bus_name: str,
        dbc_path: Path,
    ) -> tuple[QStandardItem | None, str | None]:
        """Add an explicit DBC subscription under ``bus_name``.

        If a synthetic dbc row already exists (auto-created as a parent
        for a per-message subscription), promote it to explicit and
        supersede the message children.
        """
        if not bus_name or not dbc_path:
            return None, None
        if self._is_bus_explicitly_tracked(bus_name):
            return None, None  # bus already covers this
        key = _dbc_key(bus_name, dbc_path)
        existing = self._items.get(key)
        if existing is not None and _is_explicit(existing):
            return existing, None
        bus_item = self._items.get(_bus_key(bus_name))
        if bus_item is None:
            bus_item = _make_bus_item(bus_name, explicit=False)
            self.appendRow(bus_item)
            self._items[_bus_key(bus_name)] = bus_item
        removed = self._remove_msgs_in_dbc(bus_name, dbc_path)
        if existing is not None:
            existing.setData(True, _EXPLICIT_ROLE)
            item = existing
        else:
            item = _make_dbc_item(bus_name, dbc_path, explicit=True)
            bus_item.appendRow(item)
            self._items[key] = item
        msg = (
            f"Promoted to DBC subscription · {dbc_path.name} "
            f"(replaced {removed} message{'s' if removed != 1 else ''})"
            if removed
            else None
        )
        self._recompute()
        if msg:
            self.superseded.emit(msg)
        return item, msg

    def add_message(
        self,
        bus_name: str,
        dbc_path: Path,
        message_id: int,
        message_name: str,
    ) -> tuple[QStandardItem | None, str | None]:
        """Add an explicit message subscription. Returns (item, supersede_msg=None)."""
        if not bus_name or message_id < 0:
            return None, None
        if self._is_bus_explicitly_tracked(bus_name):
            return None, None
        if dbc_path and self._is_dbc_explicitly_tracked(bus_name, dbc_path):
            return None, None
        key = _msg_key(bus_name, dbc_path, message_id)
        existing = self._items.get(key)
        if existing is not None:
            return existing, None
        bus_item = self._items.get(_bus_key(bus_name))
        if bus_item is None:
            bus_item = _make_bus_item(bus_name, explicit=False)
            self.appendRow(bus_item)
            self._items[_bus_key(bus_name)] = bus_item
        parent_item = bus_item
        if dbc_path:
            dbc_item = self._items.get(_dbc_key(bus_name, dbc_path))
            if dbc_item is None:
                dbc_item = _make_dbc_item(bus_name, dbc_path, explicit=False)
                bus_item.appendRow(dbc_item)
                self._items[_dbc_key(bus_name, dbc_path)] = dbc_item
            parent_item = dbc_item
        item = _make_msg_item(bus_name, dbc_path, message_id, message_name)
        parent_item.appendRow(item)
        self._items[key] = item
        self._recompute()
        return item, None

    def remove_at(self, index: QModelIndex | QPersistentModelIndex) -> None:
        if not index.isValid():
            return
        # itemFromIndex requires a QModelIndex; QPersistentModelIndex needs
        # conversion via the model's index() lookup.
        if isinstance(index, QPersistentModelIndex):
            model_index = self.index(index.row(), index.column(), index.parent())
        else:
            model_index = index
        item = self.itemFromIndex(model_index)
        if item is None:
            return
        # Collect the keys of this item and every descendant before removal.
        keys_to_drop: list[_NodeKey] = []
        stack: list[QStandardItem] = [item]
        while stack:
            node = stack.pop()
            key = _key_of(node)
            if key is not None:
                keys_to_drop.append(key)
            for r in range(node.rowCount()):
                child = node.child(r)
                if child is not None:
                    stack.append(child)
        parent = item.parent()
        row = item.row()
        if parent is None:
            self.removeRow(row)
        else:
            parent.removeRow(row)
            # Prune now-empty parent rows so the tree doesn't grow
            # phantom branches that no longer admit anything.
            self._prune_empty_ancestors(parent)
        for k in keys_to_drop:
            self._items.pop(k, None)
        self._recompute()

    # ---- Recompute ----

    def recompute(self, decoders_by_path: dict[Path, frozenset[int]]) -> None:
        """Recompute the effective set against the provided DBC id-sets.

        Called by the pane after construction and whenever the project's
        ``database_changed`` fires (DBC content may have shifted under a
        tracked dbc node).
        """
        self._decoders_by_path = dict(decoders_by_path)
        self._recompute()

    def _recompute(self) -> None:
        ids_by_path: dict[Path, frozenset[int]] = getattr(self, "_decoders_by_path", {})
        bus_all: set[str] = set()
        bus_dbc_ids: dict[str, set[int]] = {}
        bus_msg_ids: dict[str, set[int]] = {}
        all_entries = self.entries()
        # Only explicit dbc subscriptions shadow per-message subs on the same bus.
        explicit_dbcs_per_bus: dict[str, set[Path]] = {}
        for entry in all_entries:
            if isinstance(entry, _DbcEntry) and entry.explicit:
                explicit_dbcs_per_bus.setdefault(entry.bus_name, set()).add(entry.dbc_path)
        for entry in all_entries:
            if isinstance(entry, _BusEntry):
                if entry.explicit:
                    bus_all.add(entry.bus_name)
            elif isinstance(entry, _DbcEntry):
                if not entry.explicit:
                    continue
                if entry.bus_name in bus_all:
                    continue
                ids = ids_by_path.get(entry.dbc_path, frozenset())
                bucket = bus_dbc_ids.setdefault(entry.bus_name, set())
                bucket.update(ids)
            elif isinstance(entry, _MsgEntry):
                if entry.bus_name in bus_all:
                    continue
                if entry.dbc_path and entry.dbc_path in explicit_dbcs_per_bus.get(
                    entry.bus_name, set()
                ):
                    continue
                bucket = bus_msg_ids.setdefault(entry.bus_name, set())
                bucket.add(entry.message_id)
        new_eff = _Effective(
            bus_all=frozenset(bus_all),
            bus_dbc_ids={k: frozenset(v) for k, v in bus_dbc_ids.items()},
            bus_msg_ids={k: frozenset(v) for k, v in bus_msg_ids.items()},
        )
        if new_eff != self._effective:
            self._effective = new_eff
            self.effective_changed.emit()

    # ---- Supersede helpers ----

    def _remove_narrower_than_bus(self, bus_name: str) -> int:
        """Drop every dbc/msg row under ``bus_name``. Returns count removed."""
        bus_item = self._items.get(_bus_key(bus_name))
        if bus_item is None:
            return 0
        # Count and drop everything below.
        removed = 0
        for r in range(bus_item.rowCount() - 1, -1, -1):
            child = bus_item.child(r)
            if child is None:
                continue
            removed += _count_subtree(child)
            self._drop_subtree_keys(child)
            bus_item.removeRow(r)
        return removed

    def _remove_msgs_in_dbc(self, bus_name: str, dbc_path: Path) -> int:
        """Drop every per-message subscription under ``bus_name`` whose DBC matches.

        Operates on two places: direct msg children of the bus row (orphan
        msgs without a dbc parent) and msg children of the matching dbc
        row (which become redundant once the dbc itself is tracked).
        """
        bus_item = self._items.get(_bus_key(bus_name))
        if bus_item is None:
            return 0
        removed = 0
        for r in range(bus_item.rowCount() - 1, -1, -1):
            child = bus_item.child(r)
            if child is None:
                continue
            kind = _kind_of(child)
            if kind == "msg":
                msg_dbc = _dbc_path_of(child)
                if msg_dbc == dbc_path or msg_dbc is None or str(msg_dbc) == "":
                    aid = _msg_id_of(child)
                    if aid is None:
                        continue
                    self._items.pop(_msg_key(bus_name, msg_dbc or Path(""), aid), None)
                    bus_item.removeRow(r)
                    removed += 1
            elif kind == "dbc" and _dbc_path_of(child) == dbc_path:
                # Walk into this dbc and strip its msg children — they're
                # subsumed once the dbc subscription itself is explicit.
                for mr in range(child.rowCount() - 1, -1, -1):
                    msg_child = child.child(mr)
                    if msg_child is None:
                        continue
                    if _kind_of(msg_child) != "msg":
                        continue
                    aid = _msg_id_of(msg_child)
                    if aid is None:
                        continue
                    self._items.pop(_msg_key(bus_name, dbc_path, aid), None)
                    child.removeRow(mr)
                    removed += 1
        return removed

    def _drop_subtree_keys(self, item: QStandardItem) -> None:
        stack: list[QStandardItem] = [item]
        while stack:
            node = stack.pop()
            key = _key_of(node)
            if key is not None:
                self._items.pop(key, None)
            for r in range(node.rowCount()):
                child = node.child(r)
                if child is not None:
                    stack.append(child)

    def _is_bus_explicitly_tracked(self, bus_name: str) -> bool:
        item = self._items.get(_bus_key(bus_name))
        return item is not None and _is_explicit(item)

    def _is_dbc_explicitly_tracked(self, bus_name: str, dbc_path: Path) -> bool:
        item = self._items.get(_dbc_key(bus_name, dbc_path))
        return item is not None and _is_explicit(item)

    def _prune_empty_ancestors(self, item: QStandardItem) -> None:
        """Walk up from ``item`` removing now-empty intermediate rows.

        Prunes synthetic bus parents (created to host a dbc/msg child) and
        empty dbc rows. Stops at explicit bus rows — those are kept as
        tracked subscriptions even when they have no children.
        """
        cur = item
        while cur is not None and cur.rowCount() == 0:
            key = _key_of(cur)
            if key is None:
                return
            if key[0] == "bus" and _is_explicit(cur):
                return
            parent = cur.parent()
            row = cur.row()
            self._items.pop(key, None)
            if parent is None:
                self.removeRow(row)
                return
            parent.removeRow(row)
            cur = parent


# --------------------------------------------------------------------------
# Item helpers
# --------------------------------------------------------------------------


def _is_explicit(item: QStandardItem) -> bool:
    val = item.data(_EXPLICIT_ROLE)
    return bool(val)


def _make_bus_item(bus_name: str, *, explicit: bool) -> QStandardItem:
    item = QStandardItem(bus_name)
    item.setEditable(False)
    item.setData("bus", _NODE_KIND_ROLE)
    item.setData(bus_name, _BUS_NAME_ROLE)
    item.setData(bool(explicit), _EXPLICIT_ROLE)
    return item


def _make_dbc_item(bus_name: str, path: Path, *, explicit: bool) -> QStandardItem:
    item = QStandardItem(path.name or str(path))
    item.setEditable(False)
    item.setData("dbc", _NODE_KIND_ROLE)
    item.setData(bus_name, _BUS_NAME_ROLE)
    item.setData(path, _DBC_PATH_ROLE)
    item.setData(bool(explicit), _EXPLICIT_ROLE)
    return item


def _make_msg_item(
    bus_name: str,
    dbc_path: Path,
    message_id: int,
    message_name: str,
) -> QStandardItem:
    label = f"0x{message_id:X}  {message_name}" if message_name else f"0x{message_id:X}"
    item = QStandardItem(label)
    item.setEditable(False)
    item.setData("msg", _NODE_KIND_ROLE)
    item.setData(bus_name, _BUS_NAME_ROLE)
    item.setData(dbc_path, _DBC_PATH_ROLE)
    item.setData(int(message_id), _MSG_ID_ROLE)
    item.setData(message_name, _MSG_NAME_ROLE)
    return item


def _kind_of(item: QStandardItem) -> NodeKind | None:
    val = item.data(_NODE_KIND_ROLE)
    if val == "bus":
        return "bus"
    if val == "dbc":
        return "dbc"
    if val == "msg":
        return "msg"
    return None


def _bus_name_of(item: QStandardItem) -> str:
    val = item.data(_BUS_NAME_ROLE)
    return val if isinstance(val, str) else ""


def _dbc_path_of(item: QStandardItem) -> Path | None:
    val = item.data(_DBC_PATH_ROLE)
    if isinstance(val, Path):
        return val
    if isinstance(val, str):
        return Path(val)
    return None


def _msg_id_of(item: QStandardItem) -> int | None:
    val = item.data(_MSG_ID_ROLE)
    if isinstance(val, int):
        return val
    return None


def _msg_name_of(item: QStandardItem) -> str | None:
    val = item.data(_MSG_NAME_ROLE)
    return val if isinstance(val, str) else None


def _key_of(item: QStandardItem) -> _NodeKey | None:
    kind = _kind_of(item)
    if kind == "bus":
        return _bus_key(_bus_name_of(item))
    if kind == "dbc":
        path = _dbc_path_of(item)
        if path is None:
            return None
        return _dbc_key(_bus_name_of(item), path)
    if kind == "msg":
        path = _dbc_path_of(item) or Path("")
        aid = _msg_id_of(item)
        if aid is None:
            return None
        return _msg_key(_bus_name_of(item), path, aid)
    return None


def _count_subtree(item: QStandardItem) -> int:
    n = 1
    for r in range(item.rowCount()):
        child = item.child(r)
        if child is not None:
            n += _count_subtree(child)
    return n


# --------------------------------------------------------------------------
# Pane widget
# --------------------------------------------------------------------------


class TrackedTreePane(QWidget):
    """Left-pane widget housing the subscription tree + drop area + empty hint.

    Translates incoming payload drops into the right ``TrackedTreeModel``
    mutation. The owning window listens to ``effective_changed`` (from
    the model) and ``superseded`` to update its own state and banner.
    """

    def __init__(
        self,
        database: Database,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._tree_model = TrackedTreeModel(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QLabel("TRACKED", self)
        header.setStyleSheet("font-weight: 600; padding: 6px 8px;")
        layout.addWidget(header)

        self._drop = TraceDropArea(self)
        self._drop.payload_dropped.connect(self._on_payload_dropped)

        self._stack = QStackedWidget(self._drop)

        self._tree = QTreeView(self._stack)
        self._tree.setModel(self._tree_model)
        self._tree.setHeaderHidden(True)
        self._tree.setFont(tabular_font())
        self._tree.setUniformRowHeights(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(14)
        self._tree.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        self._tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        h = self._tree.header()
        if h is not None:
            h.setStretchLastSection(True)
            h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._stack.addWidget(self._tree)

        self._empty_hint = QLabel(
            "Drop a bus, database, or message here",
            self._stack,
        )
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet("color: palette(mid); padding: 16px;")
        self._stack.addWidget(self._empty_hint)

        self._drop.set_inner(self._stack)
        layout.addWidget(self._drop, 1)

        self._tree_model.rowsInserted.connect(lambda *_: self._update_view_state())
        self._tree_model.rowsRemoved.connect(lambda *_: self._update_view_state())
        self._tree_model.modelReset.connect(self._update_view_state)
        self._update_view_state()

        self._refresh_dbc_ids()
        self._database.database_changed.connect(self._refresh_dbc_ids)

    # ---- Public API ----

    @property
    def model(self) -> TrackedTreeModel:
        return self._tree_model

    def effective(self) -> _Effective:
        return self._tree_model.effective()

    def expand_all(self) -> None:
        self._tree.expandAll()

    # ---- Slots ----

    def _update_view_state(self) -> None:
        empty = self._tree_model.rowCount() == 0
        self._stack.setCurrentWidget(self._empty_hint if empty else self._tree)

    def _refresh_dbc_ids(self) -> None:
        ids_by_path: dict[Path, frozenset[int]] = {}
        for path in self._database.loaded_paths():
            decoder = self._database.decoder_for_path(path)
            if decoder is None:
                continue
            ids_by_path[path] = frozenset(decoder.message_ids())
        self._tree_model.recompute(ids_by_path)

    def _on_payload_dropped(self, payload: object) -> None:
        if isinstance(payload, BusPayload):
            self._handle_bus_drop(payload)
            return
        if isinstance(payload, DbcPayload):
            self._handle_dbc_drop(payload)
            return
        if isinstance(payload, (SignalPayload, MessagePayload)):
            self._handle_message_drop(payload)
            return

    def _handle_bus_drop(self, payload: BusPayload) -> None:
        if not payload.bus_name:
            return
        item, _msg = self._tree_model.add_bus(payload.bus_name)
        if item is not None:
            self._flash(item)

    def _handle_dbc_drop(self, payload: DbcPayload) -> None:
        if not payload.path:
            return
        path = Path(payload.path)
        buses = self._database.buses_for_path(path)
        if len(buses) == 1:
            bus_name = buses[0]
        else:
            # Zero or ambiguous binding — surface via superseded signal so the
            # window can flash a banner. Drop is rejected.
            reason = (
                f"DBC {path.name} is not bound to a bus"
                if not buses
                else f"DBC {path.name} is bound to multiple buses; drop on a specific bus row"
            )
            self._tree_model.superseded.emit(reason)
            return
        item, _msg = self._tree_model.add_dbc(bus_name, path)
        if item is not None:
            self._flash(item)

    def _handle_message_drop(self, payload: Payload) -> None:
        message_id = getattr(payload, "message_id", 0)
        message_name = getattr(payload, "message", "")
        if not message_name:
            return
        decoder = self._database.decoder_for_message(message_name)
        if decoder is None:
            self._tree_model.superseded.emit(f"No loaded DBC defines message {message_name!r}")
            return
        # Find the path that owns this decoder.
        path: Path | None = None
        for p in self._database.loaded_paths():
            if self._database.decoder_for_path(p) is decoder:
                path = p
                break
        if path is None:
            return
        buses = self._database.buses_for_path(path)
        if len(buses) != 1:
            reason = (
                f"DBC {path.name} is not bound to a bus"
                if not buses
                else (f"DBC {path.name} is bound to multiple buses; drop on a specific bus row")
            )
            self._tree_model.superseded.emit(reason)
            return
        bus_name = buses[0]
        item, _msg = self._tree_model.add_message(
            bus_name=bus_name,
            dbc_path=path,
            message_id=int(message_id),
            message_name=message_name,
        )
        if item is not None:
            self._flash(item)

    def _on_context_menu(self, pos: QPoint) -> None:
        viewport = self._tree.viewport()
        if viewport is None:
            return
        index = self._tree.indexAt(pos)
        if not index.isValid():
            return
        menu = QMenu(self)
        act = menu.addAction("Remove")
        chosen = menu.exec(viewport.mapToGlobal(pos))
        if chosen is act:
            self._tree_model.remove_at(index)

    def _flash(self, item: QStandardItem) -> None:
        """Briefly highlight ``item``'s row so duplicate drops show feedback."""
        index = item.index()
        if not index.isValid():
            return
        # Scroll into view + flash the selection for a moment. Pure UI cue —
        # nothing else hangs off the selection state of this view.
        self._tree.scrollTo(index)
        self._tree.setCurrentIndex(index)
        sel = self._tree.selectionModel()
        if sel is None:
            return

        def _clear() -> None:
            try:
                sel.clearSelection()
            except RuntimeError:
                pass

        QTimer.singleShot(_HIGHLIGHT_FLASH_MS, _clear)

    # ---- State persistence ----

    def serialize(self) -> list[dict[str, object]]:
        """Return a flat list of subscription entries for ``.itp`` round-trip.

        Synthetic nodes (auto-created parents) are skipped — restoring the
        explicit subscriptions is enough; the parents get rebuilt on demand.
        """
        out: list[dict[str, object]] = []
        for entry in self._tree_model.entries():
            if isinstance(entry, _BusEntry):
                if entry.explicit:
                    out.append({"kind": "bus", "bus": entry.bus_name})
            elif isinstance(entry, _DbcEntry):
                if not entry.explicit:
                    continue
                out.append(
                    {
                        "kind": "dbc",
                        "bus": entry.bus_name,
                        "path": str(entry.dbc_path),
                    }
                )
            elif isinstance(entry, _MsgEntry):
                out.append(
                    {
                        "kind": "msg",
                        "bus": entry.bus_name,
                        "path": str(entry.dbc_path),
                        "id": int(entry.message_id),
                        "name": entry.message_name,
                    }
                )
        return out

    def deserialize(self, items: Iterable[object]) -> None:
        """Restore subscriptions from a previously-saved list.

        Order matters: add buses first, then DBCs, then messages — so the
        overlap-detection logic doesn't reject children of a not-yet-added
        parent.
        """
        bus_entries: list[dict[str, object]] = []
        dbc_entries: list[dict[str, object]] = []
        msg_entries: list[dict[str, object]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            if kind == "bus":
                bus_entries.append(raw)
            elif kind == "dbc":
                dbc_entries.append(raw)
            elif kind == "msg":
                msg_entries.append(raw)
        for raw in bus_entries:
            bus = raw.get("bus")
            if isinstance(bus, str):
                self._tree_model.add_bus(bus)
        for raw in dbc_entries:
            bus = raw.get("bus")
            path = raw.get("path")
            if isinstance(bus, str) and isinstance(path, str) and path:
                self._tree_model.add_dbc(bus, Path(path))
        for raw in msg_entries:
            bus = raw.get("bus")
            path = raw.get("path")
            aid = raw.get("id")
            name = raw.get("name")
            if not isinstance(bus, str) or not isinstance(aid, int):
                continue
            self._tree_model.add_message(
                bus_name=bus,
                dbc_path=Path(path) if isinstance(path, str) and path else Path(""),
                message_id=aid,
                message_name=name if isinstance(name, str) else "",
            )
        self._tree.expandAll()
