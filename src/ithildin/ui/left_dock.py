# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Left dock — search + three collapsible accordion sections.

Spec: handoff §4.

- Filter input pinned at top, narrows Signals & Messages by case-
  insensitive substring on signal name, message name, or hex ID.
- Sections, top to bottom:
    Signals     — flat list `(message · signal · unit)` across DBCs.
                  Drag source for ``SignalPayload``.
    Messages    — list of `(0xID · name · DLC · period)`.
                  Drag source for ``MessagePayload``.
    Buses       — declared buses with bound DBCs as child rows; each
                  row's status dot shows its state and connects or
                  disconnects it on a click. Three header actions:
                    ``+ Bus`` (Add bus…)
                    ``+ DBC`` (Add database to selected bus…,
                    disabled until a bus is selected)
                    ``↻`` (Rescan adapters).
- Footer hint: "Drag onto a view button or open tab".

Implemented as a single ``QWidget`` containing a ``QVBoxLayout`` of
``CollapsibleSection`` instances. The dock holds this widget.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import (
    QAbstractListModel,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QThread,
    Signal,
    SignalInstance,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QMouseEvent,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QProgressBar,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.bus import ADAPTER_NOT_ATTACHED
from ithildin.core.decoder import Decoder
from ithildin.core.types import BusHealth
from ithildin.ui._drag_payload import (
    BUS_MIME,
    DBC_MIME,
    BusPayload,
    DbcPayload,
    MessagePayload,
    SignalPayload,
    pack_into_mime,
    parse_from_mime,
)
from ithildin.ui.adapter_prober import AdapterChoice, AdapterProber, register_prober
from ithildin.ui.collapsible_section import HEADER_BAND_HEIGHT, CollapsibleSection
from ithildin.ui.dbc_loader import DbcLoader
from ithildin.ui.icons import icon, themed_callback, themed_icon
from ithildin.ui.theme import ThemeManager
from ithildin.workspace.schema import BusConfig, DbcRef, channel_key, channel_keys

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_compact(view: QListView) -> None:
    """Tighten a QListView for the dock: smaller font, slim row padding."""
    f: QFont = view.font()
    f.setPointSize(9)
    view.setFont(f)
    # Scoped item padding overrides the global QListView::item rule
    # (4px 6px) for these dock lists only.
    view.setStyleSheet("QListView::item { padding: 1px 6px; }")


def _as_bool(value: object) -> bool:
    """Coerce a ``QSettings.value`` return to ``bool``.

    Qt's INI backend on some platforms (and the default backend when a
    config file from another platform is imported) returns the strings
    ``"true"`` / ``"false"`` instead of real bools. Treat both forms.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


_DEFAULT_SECTION_SIZES = (600, 160, 120)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class SignalsModel(QAbstractListModel):
    """Flat list of every (message · signal) pair from loaded DBCs.

    Each row carries its drag payload plus pre-rendered display and
    tooltip text. The display text honours per-DBC signal-centric mode
    (built by the caller during ``set_rows``); the tooltip names the
    origin bus + DBC file so the user can trace a signal back without
    reading the synthetic message name.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[SignalPayload, str, str]] = []

    def set_rows(self, rows: Sequence[tuple[SignalPayload, str, str]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        payload, display, tooltip = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return display
        if role == Qt.ItemDataRole.ToolTipRole:
            return tooltip
        if role == Qt.ItemDataRole.UserRole:
            return payload
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return base | Qt.ItemFlag.ItemIsDragEnabled

    def mimeTypes(self) -> list[str]:
        from ithildin.ui._drag_payload import SIGNAL_MIME

        return [SIGNAL_MIME]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        payloads = [self._rows[index.row()][0] for index in indexes if index.isValid()]
        if not payloads:
            return QMimeData()
        return pack_into_mime(payloads)


class MessagesModel(QAbstractListModel):
    """Flat list of (CAN ID, name, DLC, period) per message in loaded DBCs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[MessagePayload, int, int | None]] = []
        # tuple = (payload, dlc, cycle_ms)

    def set_rows(
        self,
        rows: Sequence[tuple[MessagePayload, int, int | None]],
    ) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),  # noqa: B008
    ) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        payload, dlc, cycle_ms = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            cycle = f"  · {cycle_ms} ms" if cycle_ms else ""
            return f"0x{payload.message_id:X}  {payload.message}  · {dlc}B{cycle}"
        if role == Qt.ItemDataRole.UserRole:
            return payload
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return base | Qt.ItemFlag.ItemIsDragEnabled

    def mimeTypes(self) -> list[str]:
        from ithildin.ui._drag_payload import SIGNAL_MIME

        return [SIGNAL_MIME]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        payloads = [self._rows[index.row()][0] for index in indexes if index.isValid()]
        if not payloads:
            return QMimeData()
        return pack_into_mime(payloads)


LiveState = Literal[
    "live",
    "opening",
    "errors",
    "bus_off",
    "lost",
    "failed",
    "closing",
    "stopped",
    "available",
    "unavailable",
]
"""Per-row liveness:

- ``live`` — the ``BusReader`` is capturing this bus and it is healthy.
- ``opening`` — opened, but the adapter isn't up yet (or the open hangs).
- ``errors`` / ``bus_off`` — capturing, but the bus is degraded.
- ``lost`` — the adapter went away; holds until the bus is opened or
  closed again.
- ``failed`` — the worker died on its own (``BusReader.failures()``), the
  reason in the tooltip; holds until the bus is opened again.
- ``closing`` — Disconnect timed out: the driver has not released the
  channel, and connecting is refused until it does.
- ``stopped`` — this bus was opened earlier in this session and the user
  closed it.
- ``available`` — a detected adapter matches this row
  (``schema.channel_key``); never opened in this session.
- ``unavailable`` — declared bus has no matching detected adapter.
"""

_DOT_FOR_STATE: dict[LiveState, str] = {
    "live": "●",
    "opening": "◔",
    "errors": "▲",
    "bus_off": "■",
    "lost": "✕",
    "failed": "⊗",
    "closing": "◐",
    "stopped": "◌",
    "available": "○",
    "unavailable": "⊘",
}

_RUNNING_STATES: frozenset[LiveState] = frozenset({"live", "opening", "errors", "bus_off"})
_WAITING_STATES: frozenset[LiveState] = frozenset({"lost", "failed"})

_STATE_FOR_HEALTH: dict[BusHealth, LiveState] = {
    BusHealth.OK: "live",
    BusHealth.ERRORS: "errors",
    BusHealth.BUS_OFF: "bus_off",
    BusHealth.LOST: "lost",
}

# Tooltip and theme tint for the states that need explaining. No health
# detail: the row only rebuilds when the state changes; the status bar
# carries the detail.
_STATE_HINTS: dict[LiveState, tuple[str, str | None]] = {
    "opening": ("Opening — waiting for the adapter.", None),
    "errors": (
        "Bus errors. Check the cable, termination and that another node is powered.",
        "warn",
    ),
    "bus_off": (
        "Bus-off — the controller stopped transmitting. Reconnect the bus to recover.",
        "err",
    ),
    "lost": ("Adapter lost — plug it back in, then connect the bus again.", "err"),
    # The failure itself says what failed.
    "failed": ("", "err"),
    "closing": (
        "Closing — the adapter has not released the channel yet. "
        "Connecting is refused until it does.",
        "warn",
    ),
}


_BUS_ROW_ROLE = Qt.ItemDataRole.UserRole + 1
_DBC_PATH_ROLE = Qt.ItemDataRole.UserRole + 2

_GLYPH_HIT_PX = 24
"""Width of the status dot's click zone, from the row's left edge."""


class _BusRow:
    """Lightweight container for a bus row's metadata.

    Stored on each top-level ``QStandardItem`` via ``setData`` so the
    drop handler and context-menu builder can recover the bus name,
    matching adapter info, and droppability without text parsing.
    ``detail`` adds tooltip lines (a failure's reason, an adapter back);
    ``pin_label`` is the context menu's "Pin to …" text, when one is offered.
    """

    __slots__ = (
        "accepts_drops",
        "bus_name",
        "channel",
        "detail",
        "interface",
        "listen_only",
        "pin_label",
        "state",
    )

    def __init__(
        self,
        bus_name: str,
        interface: str,
        channel: str | int,
        state: LiveState,
        accepts_drops: bool,
        listen_only: bool = False,
        detail: str = "",
        pin_label: str | None = None,
    ) -> None:
        self.bus_name = bus_name
        self.interface = interface
        self.channel = channel
        self.state = state
        self.accepts_drops = accepts_drops
        self.listen_only = listen_only
        self.detail = detail
        self.pin_label = pin_label


def _pin_label(adapter: AdapterChoice) -> str:
    """The "Pin to …" menu text, in the device's own words."""
    device = adapter.device
    if device is None:
        return f"Pin to {adapter.interface} {adapter.channel}"
    channel = next(
        (ch.label for ch in device.channels if ch.channel == adapter.channel),
        str(adapter.channel),
    )
    return f"Pin to {device.display_label} {channel}"


def _format_bus_label(row: _BusRow) -> str:
    """One-line render: ``[dot] BusName · iface·channel [RO]``."""
    parts = [str(p) for p in (row.interface, row.channel) if p != ""]
    suffix = f" · {'·'.join(parts)}" if parts else ""
    badge = "  [RO]" if row.listen_only else ""
    return f"{_DOT_FOR_STATE[row.state]}  {row.bus_name}{suffix}{badge}"


class _BusItemModel(QStandardItemModel):
    """Bus tree model.

    Top-level rows are a drag source for ``BusPayload``. Child rows
    (bound DBCs) are a drag source for ``DbcPayload`` — dragging a DBC
    child onto another bus additively binds it there.
    """

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if not index.isValid():
            return base
        return base | Qt.ItemFlag.ItemIsDragEnabled

    def mimeTypes(self) -> list[str]:
        return [BUS_MIME, DBC_MIME]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:
        for idx in indexes:
            if not idx.isValid():
                continue
            item = self.itemFromIndex(idx)
            if item is None:
                continue
            if not idx.parent().isValid():
                row = item.data(_BUS_ROW_ROLE)
                if isinstance(row, _BusRow):
                    return pack_into_mime([BusPayload(bus_name=row.bus_name)])
                continue
            path = item.data(_DBC_PATH_ROLE)
            if isinstance(path, Path):
                return pack_into_mime([DbcPayload(path=str(path))])
        return QMimeData()


class BusTreeView(QTreeView):
    """Two-level tree of buses (top-level) and their bound DBCs (children).

    Inspired by ``mellon.gui.panels.outline_pane`` — a real ``QTreeView``
    over a ``QStandardItemModel`` so we get native expand/collapse
    triangles, keyboard navigation, and standard Qt selection for free.
    Replaces an earlier hand-rolled stack of ``QWidget`` rows.

    Drag and drop:

    - ``DbcPayload`` drops on a top-level row (or a child row) emit
      ``bind_requested(bus_name, dbc_path_str)`` for the parent bus.
      Drops on rows whose ``_BusRow.accepts_drops`` is False are
      silently rejected (no project to persist to).
    - Top-level rows are a **drag source** for ``BusPayload`` so they
      can be dropped onto the Bus-trace view. Child rows (bound DBCs)
      stay non-draggable.

    Right-click on a child DBC row exposes "Unbind from <bus>" which
    emits ``unbind_requested(path, bus_name)``.

    A bus row's status dot is its Connect / Disconnect control: a click
    within ``_GLYPH_HIT_PX`` of the row's left edge emits
    ``connect_requested`` or ``disconnect_requested`` by state. The right-
    click menu offers the same plus Reconnect and Pin.

    Per-bus expand state is tracked by name across model rebuilds so
    loading another DBC (which triggers a refresh) doesn't snap
    collapsed buses back open.
    """

    bind_requested = Signal(str, str)  # (bus_name, dbc_path_str)
    unbind_requested = Signal(object, str)  # (Path, bus_name)
    remove_bus_requested = Signal(str)  # bus_name
    signal_centric_toggled = Signal(object, bool)  # (Path, new_value)
    # Open the bus, or reopen it when it is running (a degraded bus).
    connect_requested = Signal(str)  # bus_name
    disconnect_requested = Signal(str)  # bus_name
    pin_requested = Signal(str)  # bus_name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tree_model = _BusItemModel(self)
        self.setModel(self._tree_model)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setIndentation(14)
        self.setRootIsDecorated(True)
        self.setExpandsOnDoubleClick(True)
        self.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        # For the hand cursor over a status dot.
        self.setMouseTracking(True)

        # Names of buses the user has explicitly collapsed. Survives
        # model rebuilds — see ``set_rows``.
        self._collapsed_buses: set[str] = set()
        self.expanded.connect(self._on_index_expanded)
        self.collapsed.connect(self._on_index_collapsed)

        self._signal_centric_getter: Callable[[Path], bool] | None = None
        # Bus whose status dot the left button went down on.
        self._glyph_pressed: str | None = None

    def set_signal_centric_getter(self, getter: Callable[[Path], bool] | None) -> None:
        """Provide a callback the context menu uses to show current state.

        Without a getter the "Treat as signal-centric" menu item is
        omitted, so the tree degrades gracefully when wired before any
        database is bound (e.g. in tests that exercise the view in
        isolation).
        """
        self._signal_centric_getter = getter

    # ---- Public API ----

    def set_rows(self, rows: list[tuple[_BusRow, list[Path]]]) -> None:
        """Repopulate the tree from a flat list of (bus, dbcs) pairs.

        Keeps the selected bus or DBC row selected: every adapter rescan
        rebuilds the tree, and must not take the "+ DBC" target away.
        """
        selected = self._selected_row()
        self._tree_model.removeRows(0, self._tree_model.rowCount())
        tokens = ThemeManager.instance().current_tokens()
        for bus, dbc_paths in rows:
            parent_item = QStandardItem(_format_bus_label(bus))
            parent_item.setData(bus, _BUS_ROW_ROLE)
            parent_item.setEditable(False)
            tips: list[str] = []
            hint = _STATE_HINTS.get(bus.state)
            if hint is not None:
                tip, tint = hint
                if tip:
                    tips.append(tip)
                if tint is not None:
                    parent_item.setForeground(QColor(tokens[tint]))
            if bus.detail:
                tips.append(bus.detail)
            if bus.listen_only:
                tips.append("Listen only — this bus cannot transmit.")
            if bus.state in _RUNNING_STATES:
                tips.append("Click the dot to disconnect.")
            elif bus.state != "closing":
                tips.append("Click the dot to connect.")
            parent_item.setToolTip("\n".join(tips))
            for path in dbc_paths:
                child = QStandardItem(path.name)
                child.setData(path, _DBC_PATH_ROLE)
                child.setEditable(False)
                parent_item.appendRow(child)
            self._tree_model.appendRow(parent_item)

        # Apply per-bus expand state.
        for r in range(self._tree_model.rowCount()):
            idx = self._tree_model.index(r, 0)
            bus = idx.data(_BUS_ROW_ROLE)
            if not isinstance(bus, _BusRow):
                continue
            if bus.bus_name in self._collapsed_buses:
                self.collapse(idx)
            else:
                self.expand(idx)

        if selected is not None:
            index = self._index_for_row(*selected)
            if index is not None:
                # Not setCurrentIndex: that scrolls to the row on every rescan.
                self.selectionModel().select(
                    index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                )

    def _selected_row(self) -> tuple[str, Path | None] | None:
        """The selected row as (bus name, DBC path or ``None`` for the bus row)."""
        selection = self.selectionModel().selectedIndexes()
        if not selection:
            return None
        index = selection[0]
        if index.parent().isValid():
            bus = index.parent().data(_BUS_ROW_ROLE)
            path = index.data(_DBC_PATH_ROLE)
        else:
            bus = index.data(_BUS_ROW_ROLE)
            path = None
        return (bus.bus_name, path) if isinstance(bus, _BusRow) else None

    def _index_for_row(self, bus_name: str, path: Path | None) -> QModelIndex | None:
        index = self._index_for_bus(bus_name)
        if index is None or path is None:
            return index
        for r in range(self._tree_model.rowCount(index)):
            child = self._tree_model.index(r, 0, index)
            if child.data(_DBC_PATH_ROLE) == path:
                return child
        return None

    def bus_names(self) -> list[str]:
        out: list[str] = []
        for r in range(self._tree_model.rowCount()):
            bus = self._tree_model.index(r, 0).data(_BUS_ROW_ROLE)
            if isinstance(bus, _BusRow):
                out.append(bus.bus_name)
        return out

    def bus_row(self, bus_name: str) -> _BusRow | None:
        idx = self._index_for_bus(bus_name)
        if idx is None:
            return None
        data = idx.data(_BUS_ROW_ROLE)
        return data if isinstance(data, _BusRow) else None

    def dbcs_for_bus(self, bus_name: str) -> list[Path]:
        idx = self._index_for_bus(bus_name)
        if idx is None:
            return []
        out: list[Path] = []
        for r in range(self._tree_model.rowCount(idx)):
            child = self._tree_model.index(r, 0, idx)
            data = child.data(_DBC_PATH_ROLE)
            if isinstance(data, Path):
                out.append(data)
        return out

    def is_expanded_bus(self, bus_name: str) -> bool:
        idx = self._index_for_bus(bus_name)
        return False if idx is None else self.isExpanded(idx)

    def set_expanded_bus(self, bus_name: str, expanded: bool) -> None:
        idx = self._index_for_bus(bus_name)
        if idx is None:
            return
        if expanded:
            self.expand(idx)
        else:
            self.collapse(idx)

    # ---- Drag & drop ----

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(DBC_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasFormat(DBC_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            event.ignore()
            return
        # Drops on a child DBC bind to the parent bus — convenient when
        # you aim for the row text rather than the bus header.
        bus_index = index if not index.parent().isValid() else index.parent()
        bus = bus_index.data(_BUS_ROW_ROLE)
        if not isinstance(bus, _BusRow) or not bus.accepts_drops:
            event.ignore()
            return
        bound = False
        for payload in parse_from_mime(event.mimeData()):
            if isinstance(payload, DbcPayload) and payload.path:
                self.bind_requested.emit(bus.bus_name, payload.path)
                bound = True
        if bound:
            event.acceptProposedAction()
            return
        event.ignore()

    # ---- Status dot ----

    def _glyph_bus(self, pos: QPoint) -> _BusRow | None:
        """The bus whose status dot is under ``pos``, if any."""
        index = self.indexAt(pos)
        if not index.isValid() or index.parent().isValid():
            return None
        # Left of the item rect is the expand arrow, which indexAt also maps
        # to the row.
        if not 0 <= pos.x() - self.visualRect(index).left() < _GLYPH_HIT_PX:
            return None
        bus = index.data(_BUS_ROW_ROLE)
        return bus if isinstance(bus, _BusRow) else None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        bus = None
        if event.button() == Qt.MouseButton.LeftButton:
            bus = self._glyph_bus(event.position().toPoint())
        self._glyph_pressed = bus.bus_name if bus is not None else None
        # A press on the dot neither selects the row nor starts a drag.
        if bus is None:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        pressed, self._glyph_pressed = self._glyph_pressed, None
        if pressed is None:
            super().mouseReleaseEvent(event)
            return
        bus = self._glyph_bus(event.position().toPoint())
        if bus is None or bus.bus_name != pressed:
            return
        if bus.state in _RUNNING_STATES:
            self.disconnect_requested.emit(bus.bus_name)
        elif bus.state != "closing":
            self.connect_requested.emit(bus.bus_name)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        # A double-click's second press arrives here instead: swallowing it
        # on the dot leaves its release with no press, so the bus toggles once.
        if self._glyph_bus(event.position().toPoint()) is None:
            super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._glyph_bus(event.position().toPoint()) is None:
            self.viewport().unsetCursor()
        else:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        # The view never saw a press swallowed on the dot: nothing to drag.
        if self._glyph_pressed is None:
            super().mouseMoveEvent(event)

    # ---- Context menu ----

    def _on_context_menu(self, pos: QPoint) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return
        # Top-level rows are buses; child rows are bound DBCs.
        if not index.parent().isValid():
            bus = index.data(_BUS_ROW_ROLE)
            if not isinstance(bus, _BusRow):
                return
            menu = QMenu(self)
            bus_name = bus.bus_name
            actions: list[tuple[str, SignalInstance]] = []
            if bus.state in ("errors", "bus_off"):
                actions.append(("Reconnect", self.connect_requested))
            if bus.state in _RUNNING_STATES:
                actions.append(("Disconnect", self.disconnect_requested))
            elif bus.state != "closing":
                actions.append(("Connect", self.connect_requested))
            if bus.pin_label is not None:
                actions.append((bus.pin_label, self.pin_requested))
            for text, signal in actions:
                action = menu.addAction(text)
                action.triggered.connect(lambda _checked=False, s=signal: s.emit(bus_name))
            if actions:
                menu.addSeparator()
            remove_action = menu.addAction(f"Remove {bus.bus_name}")
            remove_action.triggered.connect(
                lambda _checked=False: self.remove_bus_requested.emit(bus_name)
            )
            menu.exec(self.viewport().mapToGlobal(pos))
            return

        path = index.data(_DBC_PATH_ROLE)
        if not isinstance(path, Path):
            return
        parent = index.parent()
        bus = parent.data(_BUS_ROW_ROLE) if parent.isValid() else None
        if not isinstance(bus, _BusRow):
            return
        menu = QMenu(self)
        if self._signal_centric_getter is not None:
            current = self._signal_centric_getter(path)
            toggle = menu.addAction("Treat as signal-centric")
            toggle.setCheckable(True)
            toggle.setChecked(current)
            dbc_path = path
            toggle.triggered.connect(
                lambda checked, p=dbc_path: self.signal_centric_toggled.emit(p, bool(checked))
            )
            menu.addSeparator()
        action = menu.addAction(f"Unbind from {bus.bus_name}")
        bus_name = bus.bus_name
        action.triggered.connect(lambda _checked=False: self.unbind_requested.emit(path, bus_name))
        menu.exec(self.viewport().mapToGlobal(pos))

    # ---- Expand-state tracking ----

    def _on_index_expanded(self, index: QModelIndex) -> None:
        bus = index.data(_BUS_ROW_ROLE)
        if isinstance(bus, _BusRow):
            self._collapsed_buses.discard(bus.bus_name)

    def _on_index_collapsed(self, index: QModelIndex) -> None:
        bus = index.data(_BUS_ROW_ROLE)
        if isinstance(bus, _BusRow):
            self._collapsed_buses.add(bus.bus_name)

    def _index_for_bus(self, bus_name: str) -> QModelIndex | None:
        for r in range(self._tree_model.rowCount()):
            idx = self._tree_model.index(r, 0)
            data = idx.data(_BUS_ROW_ROLE)
            if isinstance(data, _BusRow) and data.bus_name == bus_name:
                return idx
        return None


# --------------------------------------------------------------------------
# Left dock widget
# --------------------------------------------------------------------------


class LeftDockWidget(QWidget):
    """Dock content. Subscribe to ``Database`` and ``BusReader`` via ``bind``."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LeftDockBody")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._ctx: AppContext | None = None
        self._signals_model = SignalsModel(self)
        self._messages_model = MessagesModel(self)
        self._signals_proxy = QSortFilterProxyModel(self)
        self._signals_proxy.setSourceModel(self._signals_model)
        self._signals_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._messages_proxy = QSortFilterProxyModel(self)
        self._messages_proxy.setSourceModel(self._messages_model)
        self._messages_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # Detected adapters, as of each backend's latest probe.
        self._adapter_rows: list[AdapterChoice] = []
        # Backend failures of the latest probes, timeouts aside.
        self._failed_backends: list[tuple[str, str]] = []
        # Backends a probe timed out on, skipped by every later probe. Shared
        # with the Create dialog's prober.
        self._hung_backends: set[str] = set()
        self._prober: AdapterProber | None = None
        self._prober_thread: QThread | None = None
        # The probe in flight's results, swapped in one backend at a time,
        # so a rescan never flashes a bus unavailable.
        self._probe_rows: list[AdapterChoice] = []
        self._probe_failures: list[tuple[str, str]] = []
        self._probe_gen = 0
        self._rescan_pending = False
        # Lost or refused buses waiting for their adapter to come back, with
        # the first probe generation allowed to see it: one started before
        # the loss may list the adapter from before.
        self._waiting: dict[str, int] = {}
        # Buses whose adapter came back; the bus stays down until clicked.
        self._adapter_back: set[str] = set()
        self._loader: DbcLoader | None = None
        self._loader_thread: QThread | None = None
        # Bus name captured at "Add DBC" click time; carried through the
        # async DbcLoader so the bind target survives selection changes
        # made while the worker is parsing.
        self._loader_target_bus: str | None = None
        # The currently selected bus in the tree (None if the user has
        # not selected any row, or a child row whose parent we can't
        # resolve). Drives the "Add DBC" button's enabled state.
        self._selected_bus_name: str | None = None
        # The tree-driven Buses section. ``_build`` populates this; the
        # tree itself owns its model and per-bus expand state.
        self._bus_tree: BusTreeView | None = None
        self._buses_hint: QLabel | None = None
        self._failed_backends_label: QLabel | None = None
        self._build()

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        ctx.database.database_changed.connect(self._refresh_dbc_lists)
        ctx.bus_reader.bus_opening.connect(self._on_bus_state_changed)
        ctx.bus_reader.bus_started.connect(self._on_bus_state_changed)
        ctx.bus_reader.bus_stopped.connect(self._on_bus_state_changed)
        ctx.bus_reader.bus_released.connect(self._on_bus_state_changed)
        ctx.bus_reader.bus_health_changed.connect(self._on_bus_health_changed)
        ctx.bus_reader.bus_failed.connect(self._on_bus_failed)
        if self._bus_tree is not None:
            self._bus_tree.set_signal_centric_getter(ctx.database.is_signal_centric)
        self._refresh_dbc_lists()
        # Rebuilt on a theme switch too: degraded rows carry a theme tint.
        themed_callback(self._refresh_buses)

    def start_adapter_probe(self) -> None:
        """Kick off background adapter detection.

        Separate from ``bind`` so that tests which only care about the
        DBC-driven models don't spawn a worker thread that has to be
        torn down. ``MainWindow`` calls this once after ``show()``; later
        probes come from :meth:`rescan_adapters`.
        """
        self.rescan_adapters()

    def rescan_adapters(self) -> None:
        """Probe the adapters again: a device came or went, or the user asked.

        Requests while a probe runs merge into one more probe after it.
        Never opens a bus: an adapter coming back only offers its bus a
        reconnect.
        """
        if self._prober is not None:
            self._rescan_pending = True
        else:
            self._start_prober()

    # ---- Persistence ----
    #
    # Layout state — splitter sizes plus per-section expanded/collapsed
    # state — lives in ``QSettings`` (user-scoped), not in the project
    # ``.itp`` file. It's a per-user UI preference, identical for every
    # project. Same SETTINGS_ORG / SETTINGS_APP that ``ThemeManager``
    # uses, so everything user-scoped lands in one place.

    _SETTINGS_ORG = "Ithildin"
    _SETTINGS_APP = "Ithildin"
    _SETTINGS_PREFIX = "LeftDock"

    def save_state(self) -> None:
        """Persist remembered sizes + collapsed state. Called from MainWindow.closeEvent."""
        settings = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)
        settings.setValue(
            f"{self._SETTINGS_PREFIX}/size_signals",
            self._remembered_expanded[self._sig_section],
        )
        settings.setValue(
            f"{self._SETTINGS_PREFIX}/size_messages",
            self._remembered_expanded[self._msg_section],
        )
        settings.setValue(
            f"{self._SETTINGS_PREFIX}/size_buses",
            self._remembered_expanded[self._buses_section],
        )
        settings.setValue(
            f"{self._SETTINGS_PREFIX}/expanded_signals",
            self._sig_section.is_expanded(),
        )
        settings.setValue(
            f"{self._SETTINGS_PREFIX}/expanded_messages",
            self._msg_section.is_expanded(),
        )
        settings.setValue(
            f"{self._SETTINGS_PREFIX}/expanded_buses",
            self._buses_section.is_expanded(),
        )

    def _restore_dock_state(self) -> None:
        """Apply saved remembered sizes + collapsed state over the defaults set in ``_build``."""
        settings = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)

        # Remembered expanded sizes — load first so any toggled signal
        # fired by the expanded-state loop below uses fresh values.
        # Partial / corrupted state falls through to the defaults from
        # ``_build``.
        try:
            sig = int(settings.value(f"{self._SETTINGS_PREFIX}/size_signals"))
            msg = int(settings.value(f"{self._SETTINGS_PREFIX}/size_messages"))
            buses = int(settings.value(f"{self._SETTINGS_PREFIX}/size_buses"))
        except (TypeError, ValueError):
            sig = msg = buses = 0
        if sig > 0 and msg > 0 and buses > 0:
            self._remembered_expanded[self._sig_section] = sig
            self._remembered_expanded[self._msg_section] = msg
            self._remembered_expanded[self._buses_section] = buses

        for section, key in (
            (self._sig_section, "expanded_signals"),
            (self._msg_section, "expanded_messages"),
            (self._buses_section, "expanded_buses"),
        ):
            raw = settings.value(f"{self._SETTINGS_PREFIX}/{key}")
            if raw is not None:
                section.set_expanded(_as_bool(raw))

        self._rebalance()

    # ---- Section sizing ----

    def _rebalance(self) -> None:
        """Push remembered/collapsed weights into the splitter.

        Collapsed sections contribute the header-band height; expanded
        ones contribute their remembered preferred size. Qt scales the
        ratios to fit the available viewport.
        """
        weights = [
            self._remembered_expanded[section] if section.is_expanded() else HEADER_BAND_HEIGHT
            for section in (self._sig_section, self._msg_section, self._buses_section)
        ]
        self._rebalancing = True
        try:
            self._sections_splitter.setSizes(weights)
        finally:
            self._rebalancing = False

    def _on_section_toggled(self, _expanded: bool) -> None:
        self._rebalance()

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        if self._rebalancing:
            return
        sizes = self._sections_splitter.sizes()
        for section, size in zip(
            (self._sig_section, self._msg_section, self._buses_section),
            sizes,
            strict=True,
        ):
            if section.is_expanded():
                self._remembered_expanded[section] = size

    # ---- Build ----

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(8)

        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Filter signals & messages")
        self._filter_input.textChanged.connect(self._apply_filter)

        self._case_action = QAction(self)
        self._case_action.setCheckable(True)
        self._case_action.setToolTip("Match case")
        self._case_action.toggled.connect(self._on_case_toggled)
        themed_callback(self._refresh_case_icon)
        self._filter_input.addAction(self._case_action, QLineEdit.ActionPosition.TrailingPosition)

        self._clear_action = QAction(self)
        self._clear_action.setToolTip("Clear")
        self._clear_action.triggered.connect(self._filter_input.clear)
        themed_icon("x", self._clear_action.setIcon)
        self._filter_input.addAction(self._clear_action, QLineEdit.ActionPosition.TrailingPosition)
        # Must follow addAction: QLineEdit builds the side-widget button
        # there and shows it regardless of the action's current state, so
        # hiding beforehand has no effect.
        self._clear_action.setVisible(False)

        layout.addWidget(self._filter_input)

        # Signals section.
        self._signals_view = QListView(self)
        self._signals_view.setModel(self._signals_proxy)
        self._signals_view.setUniformItemSizes(True)
        self._signals_view.setDragEnabled(True)
        self._signals_view.setDragDropMode(QListView.DragDropMode.DragOnly)
        self._signals_view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        _make_compact(self._signals_view)
        self._sig_section = CollapsibleSection("Signals", self)
        self._sig_section.set_inner(self._signals_view)

        # Messages section.
        self._messages_view = QListView(self)
        self._messages_view.setModel(self._messages_proxy)
        self._messages_view.setUniformItemSizes(True)
        self._messages_view.setDragEnabled(True)
        self._messages_view.setDragDropMode(QListView.DragDropMode.DragOnly)
        self._messages_view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        _make_compact(self._messages_view)
        self._msg_section = CollapsibleSection("Messages", self)
        self._msg_section.set_inner(self._messages_view)

        # Buses section: project-declared buses shown in a real QTreeView
        # with native expand/collapse and bound-DBC child rows. Pattern
        # mirrors mellon's outline pane — a QTreeView over a
        # QStandardItemModel so we get keyboard nav and tree gestures
        # for free instead of rolling our own stacked widgets.
        buses_inner = QWidget(self)
        buses_inner_layout = QVBoxLayout(buses_inner)
        buses_inner_layout.setContentsMargins(0, 0, 0, 0)
        buses_inner_layout.setSpacing(2)
        self._bus_tree = BusTreeView(buses_inner)
        self._bus_tree.bind_requested.connect(self._on_bind_requested)
        self._bus_tree.unbind_requested.connect(self._on_unbind)
        self._bus_tree.remove_bus_requested.connect(self._on_remove_bus)
        self._bus_tree.signal_centric_toggled.connect(self._on_signal_centric_toggled)
        self._bus_tree.connect_requested.connect(self._on_connect_requested)
        self._bus_tree.disconnect_requested.connect(self._on_disconnect_requested)
        self._bus_tree.pin_requested.connect(self._on_pin_requested)
        sel_model = self._bus_tree.selectionModel()
        sel_model.selectionChanged.connect(self._on_bus_selection_changed)
        buses_inner_layout.addWidget(self._bus_tree)
        # Indeterminate busy indicator while a DBC parses on the worker
        # thread. cantools exposes no progress callback, so range 0,0
        # (marquee) is the honest representation. Hidden until used.
        self._dbc_loading_label = QLabel("", buses_inner)
        self._dbc_loading_label.setStyleSheet("color: palette(mid); font-size: 9pt;")
        self._dbc_loading_label.setVisible(False)
        buses_inner_layout.addWidget(self._dbc_loading_label)
        self._dbc_loading_bar = QProgressBar(buses_inner)
        self._dbc_loading_bar.setRange(0, 0)
        self._dbc_loading_bar.setTextVisible(False)
        self._dbc_loading_bar.setFixedHeight(6)
        self._dbc_loading_bar.setVisible(False)
        buses_inner_layout.addWidget(self._dbc_loading_bar)
        # Empty-state stand-in. Reuses the DbcEmptyHint object name so
        # it picks up the existing QSS framing.
        self._buses_hint = QLabel("Click + Bus to add a bus", buses_inner)
        self._buses_hint.setObjectName("DbcEmptyHint")
        self._buses_hint.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._buses_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._buses_hint.setMinimumHeight(80)
        buses_inner_layout.addWidget(self._buses_hint)
        self._failed_backends_label = QLabel("", buses_inner)
        self._failed_backends_label.setWordWrap(True)
        self._failed_backends_label.setStyleSheet("color: palette(mid); font-size: 9pt;")
        self._failed_backends_label.setVisible(False)
        buses_inner_layout.addWidget(self._failed_backends_label)
        self._buses_section = CollapsibleSection("Buses", self)
        self._buses_section.set_inner(buses_inner)
        self._add_bus_btn = self._buses_section.add_header_action(
            text="+ Bus",
            tooltip="Add bus…",
            callback=self._on_add_bus,
        )
        self._dbc_load_btn = self._buses_section.add_header_action(
            text="+ DBC",
            tooltip="Add database to selected bus…",
            callback=self._on_load_dbc,
        )
        self._dbc_load_btn.setEnabled(False)
        # Linux has no device-change hook, and Windows drops the event
        # while the GUI thread is blocked.
        self._rescan_btn = self._buses_section.add_header_action(
            text="↻",
            tooltip="Rescan adapters",
            callback=self.rescan_adapters,
        )

        # User-resizable stack: the three sections live in a vertical
        # splitter so the user can drag handles to allocate space
        # between them (Messages will usually want most of it).
        self._sections_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._sections_splitter.setChildrenCollapsible(False)
        self._sections_splitter.setHandleWidth(4)
        self._sections_splitter.addWidget(self._sig_section)
        self._sections_splitter.addWidget(self._msg_section)
        self._sections_splitter.addWidget(self._buses_section)
        # Signals dominates by default — one row per *signal* in the
        # loaded DBCs, easily dozens — while Messages and Buses each
        # have one row per top-level item and rarely need more than a
        # handful of rows of space.
        self._sections_splitter.setSizes(list(_DEFAULT_SECTION_SIZES))
        # Only Signals absorbs the extra room when the dock is resized
        # taller; Messages and Buses stay at their natural heights.
        self._sections_splitter.setStretchFactor(0, 1)
        self._sections_splitter.setStretchFactor(1, 0)
        self._sections_splitter.setStretchFactor(2, 0)
        layout.addWidget(self._sections_splitter, 1)
        # Per-section "preferred expanded height". Drives auto-reclaim
        # of freed space on collapse and restore-to-prior-size on
        # expand. Kept fresh by ``_on_splitter_moved`` whenever the
        # user drags a handle.
        self._remembered_expanded: dict[CollapsibleSection, int] = {
            self._sig_section: _DEFAULT_SECTION_SIZES[0],
            self._msg_section: _DEFAULT_SECTION_SIZES[1],
            self._buses_section: _DEFAULT_SECTION_SIZES[2],
        }
        # Reentrancy guard: Qt emits ``splitterMoved`` from inside
        # ``setSizes``, and without this flag ``_on_splitter_moved``
        # would clobber the just-set remembered values with the
        # ratio-scaled live sizes.
        self._rebalancing = False
        self._sig_section.toggled.connect(self._on_section_toggled)
        self._msg_section.toggled.connect(self._on_section_toggled)
        self._buses_section.toggled.connect(self._on_section_toggled)
        self._sections_splitter.splitterMoved.connect(self._on_splitter_moved)
        # Apply persisted sizes / collapsed state on top of the defaults
        # so a fresh install gets the layout above, and returning users
        # see whatever they last set.
        self._restore_dock_state()

        # Footer hint.
        hint = QLabel("Drag onto a view button or open tab", self)
        f: QFont = hint.font()
        f.setItalic(True)
        hint.setFont(f)
        hint.setStyleSheet("color: palette(mid); padding: 4px 0;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

    # ---- Slots ----

    def _apply_filter(self, text: str) -> None:
        self._clear_action.setVisible(bool(text))
        self._signals_proxy.setFilterFixedString(text)
        self._messages_proxy.setFilterFixedString(text)

    def _on_case_toggled(self, checked: bool) -> None:
        cs = Qt.CaseSensitivity.CaseSensitive if checked else Qt.CaseSensitivity.CaseInsensitive
        self._signals_proxy.setFilterCaseSensitivity(cs)
        self._messages_proxy.setFilterCaseSensitivity(cs)
        self._refresh_case_icon()
        self._apply_filter(self._filter_input.text())

    def _refresh_case_icon(self) -> None:
        name = "match_case_on" if self._case_action.isChecked() else "match_case"
        self._case_action.setIcon(icon(name))

    def _refresh_dbc_lists(self) -> None:
        if self._ctx is None:
            return
        signals: list[tuple[SignalPayload, str, str]] = []
        messages: list[tuple[MessagePayload, int, int | None]] = []
        for path in sorted(self._ctx.database.loaded_paths(), key=lambda p: p.name):
            decoder = self._ctx.database.decoder_for_path(path)
            if decoder is None:
                continue
            db = decoder.database
            signal_centric = self._ctx.database.is_signal_centric(path)
            buses = self._ctx.database.buses_for_path(path)
            bus_label = ", ".join(buses) if buses else "—"
            tooltip = f"Bus: {bus_label} · DBC: {path.name}"
            for msg in sorted(db.messages, key=lambda m: m.frame_id):
                cycle = getattr(msg, "cycle_time", None)
                length = int(getattr(msg, "length", 0))
                messages.append(
                    (
                        MessagePayload(
                            message=msg.name,
                            message_id=int(msg.frame_id),
                        ),
                        length,
                        int(cycle) if cycle else None,
                    )
                )
                for sig in msg.signals:
                    unit = getattr(sig, "unit", "") or ""
                    unit_suffix = f"  [{unit}]" if unit else ""
                    if signal_centric:
                        display = f"{sig.name}{unit_suffix}"
                    else:
                        display = f"0x{int(msg.frame_id):X}  {msg.name}.{sig.name}{unit_suffix}"
                    signals.append(
                        (
                            SignalPayload(
                                message=msg.name,
                                message_id=int(msg.frame_id),
                                signal=sig.name,
                                unit=unit,
                            ),
                            display,
                            tooltip,
                        )
                    )
        self._signals_model.set_rows(signals)
        self._messages_model.set_rows(messages)
        # Bus rows show their bound DBCs as child rows; rebuild them
        # whenever the DBC set changes.
        self._refresh_buses()

    def refresh_buses(self) -> None:
        """Public alias used by ``MainWindow`` after mutating workspace.bus."""
        self._refresh_buses()

    def _refresh_buses(self) -> None:
        """Rebuild the Buses section from ``project.workspace.bus``.

        ``project`` is always set (an unsaved project is still a project).
        Each row's live state comes from the prober's detected adapters
        plus the bus reader's open state. Detected adapters that don't
        match any declared bus are deliberately *not* shown — buses are
        user-curated via the section's ``+`` action.
        """
        if self._bus_tree is None or self._ctx is None:
            return
        # Narrow the optional types once for the rest of this method.
        assert self._buses_hint is not None
        assert self._failed_backends_label is not None
        tree = self._bus_tree

        project = self._ctx.project
        bus_reader = self._ctx.bus_reader
        live_names = set(bus_reader.bus_names())
        ever_opened = {cfg.name for cfg in bus_reader.last_configs()}
        failures = bus_reader.failures()
        # Group bound DBCs by bus name so we can render them as child rows.
        dbcs_per_bus: dict[str, list[Path]] = {}
        for path in self._ctx.database.loaded_paths():
            for bus in self._ctx.database.buses_for_path(path):
                dbcs_per_bus.setdefault(bus, []).append(path)

        rows: list[tuple[_BusRow, list[Path]]] = []
        lost_or_failed: set[str] = set()
        for cfg in project.workspace.bus:
            state: LiveState
            health = bus_reader.health(cfg.name)
            detected = self._detected_for(cfg)
            details: list[str] = []
            if cfg.name in live_names:
                state = "opening" if health is None else _STATE_FOR_HEALTH[health[0]]
            elif bus_reader.is_closing(cfg.name):
                state = "closing"
            elif health is not None and health[0] is BusHealth.LOST:
                state = "lost"
            elif cfg.name in failures:
                state = "failed"
                details.append(failures[cfg.name])
            elif cfg.name in ever_opened:
                state = "stopped"
            elif detected is not None:
                state = "available"
            else:
                state = "unavailable"
            if state in _WAITING_STATES:
                lost_or_failed.add(cfg.name)
                if cfg.name in self._adapter_back:
                    details.append("Adapter is back — click the dot to reconnect.")
            pin = self._pin_candidate(cfg) if state not in _RUNNING_STATES else None
            rows.append(
                (
                    _BusRow(
                        bus_name=cfg.name,
                        interface=cfg.interface,
                        # Where the adapter is now: a pinned one may have moved.
                        channel=detected.channel if detected is not None else cfg.channel,
                        state=state,
                        accepts_drops=True,
                        listen_only=cfg.listen_only,
                        detail="\n".join(details),
                        pin_label=_pin_label(pin) if pin is not None else None,
                    ),
                    sorted(dbcs_per_bus.get(cfg.name, []), key=lambda p: p.name),
                )
            )
        # Opened, closed, forgotten or gone from the project: nothing to wait for.
        self._waiting = {n: g for n, g in self._waiting.items() if n in lost_or_failed}
        self._adapter_back &= lost_or_failed

        tree.set_rows(rows)
        tree.setVisible(bool(rows))
        self._buses_hint.setVisible(not rows)
        # The selected bus may be gone; re-sync the cached bus name and the
        # Add-DBC button's enabled state.
        self._recompute_selected_bus()

        # Failed-backends warning footer.
        warnings = [f"⚠ {name}: {reason}" for name, reason in self._failed_backends]
        warnings += [
            f"⚠ {name}: timed out — restart to retry" for name in sorted(self._hung_backends)
        ]
        self._failed_backends_label.setText("  ".join(warnings))
        self._failed_backends_label.setVisible(bool(warnings))

    def _detected_for(self, cfg: BusConfig) -> AdapterChoice | None:
        """The detected adapter channel this bus opens, if any."""
        return next(iter(self._adapters_for(cfg)), None)

    def _adapters_for(self, cfg: BusConfig) -> list[AdapterChoice]:
        """Every detected adapter channel this bus would open."""
        key = channel_key(cfg.interface, cfg.channel, cfg.hw_id)
        # An unpinned bus matches by channel alone, whatever the adapter's id.
        return [
            a
            for a in self._adapter_rows
            if channel_key(a.interface, a.channel, a.hw_id if cfg.hw_id is not None else None)
            == key
        ]

    def _pin_candidate(self, cfg: BusConfig) -> AdapterChoice | None:
        """The adapter on this bus's channel to offer pinning it to.

        Offered for an unpinned bus, and for a pinned one whose adapter the
        latest probe didn't find (a different adapter took its place). Never
        when the pin is already right, nor when the adapter has no identity.
        """
        if cfg.hw_id is not None and self._detected_for(cfg) is not None:
            return None
        return next(
            (
                a
                for a in self._adapter_rows
                if a.interface == cfg.interface
                and str(a.channel) == str(cfg.channel)
                and a.hw_id is not None
                and a.hw_id != cfg.hw_id
            ),
            None,
        )

    def _on_add_bus(self) -> None:
        if self._ctx is None:
            return
        from ithildin.ui.create_bus_dialog import CreateCanBusDialog

        buses = self._ctx.project.workspace.bus
        dlg = CreateCanBusDialog(
            existing_names={b.name for b in buses},
            existing_keys=frozenset().union(
                *(channel_keys(b.interface, b.channel, b.hw_id) for b in buses)
            ),
            hung_backends=self._hung_backends,
            parent=self,
        )
        dlg.bus_declared.connect(self._on_bus_declared)
        dlg.bus_declared_and_connect.connect(self._on_bus_declared_and_connect)
        dlg.exec()

    def _on_bus_declared(self, cfg: object, dbcs: object = ()) -> bool:
        """Declare a bus and bind any databases picked in the create dialog.

        Returns ``True`` when the bus was newly declared; ``False`` when the
        duplicate-name guard rejected it (so DBCs aren't bound to a
        pre-existing bus's name).
        """
        if self._ctx is None or not isinstance(cfg, BusConfig):
            return False
        project = self._ctx.project
        if any(b.name == cfg.name for b in project.workspace.bus):
            self._ctx.database.error.emit(f"Bus name {cfg.name!r} already declared; pick another.")
            return False
        holder = self._channel_holder(cfg.name, cfg.interface, cfg.channel, cfg.hw_id)
        if holder is not None:
            self._ctx.database.error.emit(
                f"Bus {holder!r} already uses that {cfg.interface} channel or adapter; "
                "pick another."
            )
            return False
        project.workspace.bus = [*project.workspace.bus, cfg]
        bound_dbcs = self._bind_declared_dbcs(cfg.name, dbcs)
        self._refresh_buses()
        if bound_dbcs:
            # Mirrors the live Database state (incl. the new bindings) into
            # workspace.dbc and saves — supersedes the bare silent save.
            self._persist_dbc_bindings()
        else:
            self._save_project_silently()
        self._ctx.events.buses_changed.emit()
        return True

    def _bind_declared_dbcs(self, bus_name: str, dbcs: object) -> bool:
        """Attach the dialog's pre-parsed decoders and bind them to ``bus_name``.

        Reuses the same ``Database`` calls as the "+ DBC" flow
        (``_on_dbc_loaded``); the decoders were parsed off-thread in the
        dialog's :class:`DbcPicker`, so nothing parses on the GUI thread here.
        Returns ``True`` if at least one DBC was bound.
        """
        if self._ctx is None or not isinstance(dbcs, (list, tuple)):
            return False
        bound = False
        for item in dbcs:
            if not (isinstance(item, tuple) and len(item) == 2):
                continue
            path, decoder = item
            if not isinstance(path, Path) or not isinstance(decoder, Decoder):
                continue
            self._ctx.database.attach_loaded_decoder(path, decoder)
            self._ctx.database.bind_dbc(path, bus_name)
            bound = True
        return bound

    def _channel_holder(
        self, name: str, interface: str, channel: str | int, hw_id: str | None
    ) -> str | None:
        """Another declared bus on that channel or adapter (``schema.channel_keys``)."""
        assert self._ctx is not None
        keys = channel_keys(interface, channel, hw_id)
        return next(
            (
                b.name
                for b in self._ctx.project.workspace.bus
                if b.name != name
                and not keys.isdisjoint(channel_keys(b.interface, b.channel, b.hw_id))
            ),
            None,
        )

    def _on_bus_declared_and_connect(self, cfg: object, dbcs: object = ()) -> None:
        if self._ctx is None or not isinstance(cfg, BusConfig):
            return
        # Capture newness *before* declaring: a duplicate name is rejected
        # by _on_bus_declared, and we must not open the pre-existing bus
        # that happens to share the name.
        is_new = not any(b.name == cfg.name for b in self._ctx.project.workspace.bus)
        declared = self._on_bus_declared(cfg, dbcs)
        if is_new and declared:
            self._ctx.bus_reader.open(cfg)

    def _on_remove_bus(self, bus_name: str) -> None:
        if self._ctx is None:
            return
        # A removed bus must not keep running, nor be reopened by a
        # Reconnect from a remembered config.
        self._ctx.bus_reader.close(bus_name)
        self._ctx.bus_reader.forget(bus_name)
        project = self._ctx.project
        project.workspace.bus = [b for b in project.workspace.bus if b.name != bus_name]
        # Cascade: drop any DBC bindings that referenced the removed bus.
        for path in list(self._ctx.database.loaded_paths()):
            if bus_name in self._ctx.database.buses_for_path(path):
                self._ctx.database.unbind_dbc(path, bus_name)
        self._refresh_buses()
        self._save_project_silently()
        self._ctx.events.buses_changed.emit()

    def _workspace_config(self, bus_name: str) -> BusConfig | None:
        assert self._ctx is not None
        return next((b for b in self._ctx.project.workspace.bus if b.name == bus_name), None)

    def _on_connect_requested(self, bus_name: str) -> None:
        """Open the bus from its workspace config, pin included; reopen a running one."""
        if self._ctx is None:
            return
        reader = self._ctx.bus_reader
        cfg = self._workspace_config(bus_name)
        if cfg is None or reader.is_closing(bus_name):
            return
        if bus_name in reader.bus_names():
            reader.reopen(cfg)
        else:
            reader.open(cfg)

    def _on_disconnect_requested(self, bus_name: str) -> None:
        if self._ctx is not None:
            self._ctx.bus_reader.close(bus_name)

    def _on_pin_requested(self, bus_name: str) -> None:
        """Pin the bus to the adapter now on its channel, unless another bus has it."""
        if self._ctx is None:
            return
        cfg = self._workspace_config(bus_name)
        adapter = self._pin_candidate(cfg) if cfg is not None else None
        if cfg is None or adapter is None:
            return
        holder = self._channel_holder(bus_name, cfg.interface, cfg.channel, adapter.hw_id)
        if holder is not None:
            self._ctx.database.error.emit(
                f"Can't pin bus {bus_name!r}: bus {holder!r} is pinned to that adapter."
            )
            return
        project = self._ctx.project
        project.workspace.bus = [
            b.model_copy(update={"hw_id": adapter.hw_id}) if b.name == bus_name else b
            for b in project.workspace.bus
        ]
        self._refresh_buses()
        if not project.is_saved:
            return
        try:
            project.save_adapter_pins()
        except OSError as exc:
            self._ctx.database.error.emit(f"Failed to save adapter pins: {exc}")

    def _save_project_silently(self) -> None:
        """Persist the project iff it has a path; surface OS errors via Database.error."""
        if self._ctx is None:
            return
        project = self._ctx.project
        if not project.is_saved:
            return
        try:
            project.save()
        except OSError as exc:
            self._ctx.database.error.emit(f"Failed to save project: {exc}")

    def _on_bind_requested(self, bus_name: str, dbc_path_str: str) -> None:
        if self._ctx is None or not dbc_path_str:
            return
        path = Path(dbc_path_str)
        if path not in self._ctx.database.loaded_paths():
            return
        self._ctx.database.bind_dbc(path, bus_name)
        self._persist_dbc_bindings()

    def _on_bus_selection_changed(self, *_args: object) -> None:
        """Slot for ``selectionChanged`` — args ignored, state read from model."""
        self._recompute_selected_bus()

    def _recompute_selected_bus(self) -> None:
        """Re-derive ``_selected_bus_name`` from the tree's selection model.

        A top-level row resolves to its own bus name; a child row resolves
        to its parent bus. Drives the "Add database" button's enabled
        state. Skipped while a DBC parse is in flight — the button is
        already held disabled by ``_on_load_dbc`` until ``_on_dbc_load_finished``.
        """
        if self._bus_tree is None:
            return
        sel = self._bus_tree.selectionModel().selectedIndexes()
        bus_name: str | None = None
        if sel:
            idx = sel[0]
            if idx.parent().isValid():
                idx = idx.parent()
            data = idx.data(_BUS_ROW_ROLE)
            if isinstance(data, _BusRow):
                bus_name = data.bus_name
        self._selected_bus_name = bus_name
        if self._loader is None:
            self._dbc_load_btn.setEnabled(bus_name is not None)

    def _on_unbind(self, path: Path, bus: str) -> None:
        """Unbind a DBC from a bus; remove from the project if it was its
        last binding (every loaded DBC must live under at least one bus in
        the new model)."""
        if self._ctx is None:
            return
        self._ctx.database.unbind_dbc(path, bus)
        if not self._ctx.database.buses_for_path(path):
            self._ctx.database.remove_dbc(path)
        self._persist_dbc_bindings()

    def _on_signal_centric_toggled(self, path: Path, value: bool) -> None:
        if self._ctx is None:
            return
        self._ctx.database.set_signal_centric(path, value)
        self._persist_dbc_bindings()

    def _persist_dbc_bindings(self) -> None:
        """Mirror current Database state back into ``project.workspace.dbc``.

        Always updates the in-memory workspace; only writes to disk when
        the project has a path bound (i.e. has been saved at least once).
        Until the user runs Save-As the bindings sit in memory and get
        flushed by the eventual ``project.save()``.
        """
        if self._ctx is None:
            return
        project = self._ctx.project
        bus_names = {b.name for b in project.workspace.bus}
        new_refs: list[DbcRef] = []
        for path in self._ctx.database.loaded_paths():
            buses = [b for b in self._ctx.database.buses_for_path(path) if b in bus_names]
            new_refs.append(
                DbcRef(
                    path=path,
                    buses=buses,
                    signal_centric=self._ctx.database.is_signal_centric(path),
                )
            )
        project.workspace.dbc = new_refs
        if not project.is_saved:
            return
        try:
            project.save()
        except OSError as exc:
            self._ctx.database.error.emit(f"Failed to save project: {exc}")

    def _on_bus_state_changed(self, _name: str) -> None:
        self._refresh_dbc_lists()
        self._refresh_buses()

    def _on_bus_health_changed(self, name: str, state: str, _detail: str) -> None:
        # A detail-only change keeps the row: rebuilding the tree on every
        # one would churn it on a noisy bus.
        if self._bus_tree is None:
            return
        row = self._bus_tree.bus_row(name)
        if row is not None and row.state != _STATE_FOR_HEALTH[BusHealth(state)]:
            self._refresh_buses()

    def _on_bus_failed(self, bus_name: str, message: str) -> None:
        """Wait for a lost or missing adapter to come back, and look for it again."""
        if self._ctx is None:
            return
        health = self._ctx.bus_reader.health(bus_name)
        lost = health is not None and health[0] is BusHealth.LOST
        cfg = self._workspace_config(bus_name)
        if cfg is None or not (lost or ADAPTER_NOT_ATTACHED in message):
            return
        # A probe already running may still list the adapter: only the next counts.
        self._waiting[bus_name] = self._probe_gen + 1
        self._adapter_back.discard(bus_name)
        gone = self._adapters_for(cfg)
        self._adapter_rows = [a for a in self._adapter_rows if a not in gone]
        self.rescan_adapters()

    def _start_prober(self) -> None:
        self._rescan_pending = False
        self._probe_gen += 1
        self._probe_rows = []
        self._probe_failures = []
        self._prober = AdapterProber(skip_backends=frozenset(self._hung_backends))
        # No QObject parent: lifetime is managed via signal-driven
        # deleteLater. Parenting to the dock would let qtbot dispose
        # the widget while the worker thread is still mid-probe, and
        # destroying a running QThread is undefined behaviour.
        self._prober_thread = QThread()
        self._prober.moveToThread(self._prober_thread)
        self._prober_thread.started.connect(self._prober.run)
        self._prober.adapter_found.connect(self._on_adapter_found)
        self._prober.backend_failed.connect(self._on_backend_failed)
        self._prober.backend_done.connect(self._on_backend_done)
        self._prober.finished.connect(self._on_prober_finished)
        # DirectConnection so the worker thread itself triggers quit;
        # otherwise the queued slot would target the main thread, which
        # may already be tearing down by the time it lands.
        self._prober.finished.connect(self._prober_thread.quit, Qt.ConnectionType.DirectConnection)
        self._prober.finished.connect(self._prober.deleteLater)
        self._prober_thread.finished.connect(self._prober_thread.deleteLater)
        # Cancel + join this probe if the app quits mid-enumeration.
        register_prober(self._prober, self._prober_thread)
        self._prober_thread.start()

    def _on_adapter_found(self, adapter: AdapterChoice) -> None:
        self._probe_rows.append(adapter)

    def _on_backend_failed(self, backend: str, reason: str) -> None:
        if reason == "timeout":
            self._hung_backends.add(backend)
        else:
            self._probe_failures.append((backend, reason))

    def _on_backend_done(self, backend: str) -> None:
        self._take_probe_results(backend)

    def _on_prober_finished(self) -> None:
        # The dev-mode loopback comes last, with no backend_done of its own.
        self._take_probe_results("virtual")
        self._prober = None
        self._prober_thread = None
        if self._rescan_pending:
            self._start_prober()

    def _take_probe_results(self, backend: str) -> None:
        """Replace one backend's adapters and failures with this probe's.

        A backend that has since hung (in the Create dialog's probe, say)
        gets no more probes, so its rows would never be replaced: drop them.
        """
        self._adapter_rows = [
            a
            for a in self._adapter_rows
            if a.interface != backend and a.interface not in self._hung_backends
        ]
        self._adapter_rows += [a for a in self._probe_rows if a.interface == backend]
        self._failed_backends = [f for f in self._failed_backends if f[0] != backend]
        self._failed_backends += [f for f in self._probe_failures if f[0] == backend]
        self._report_adapters_back(backend)
        self._refresh_buses()

    def _report_adapters_back(self, backend: str) -> None:
        """Tell the user once when a waiting bus's adapter is listed again.

        Judged only on ``backend``'s rows just taken from a probe started
        after the loss: another backend's rows may still come from a probe
        that was running when the adapter went. The bus stays down:
        reopening it by itself would ACK on the wire and let MCP or Replay
        send with nobody having acted.
        """
        if self._ctx is None:
            return
        for bus_name, first_gen in list(self._waiting.items()):
            cfg = self._workspace_config(bus_name)
            if (
                self._probe_gen < first_gen
                or cfg is None
                or cfg.interface != backend
                or self._detected_for(cfg) is None
            ):
                continue
            del self._waiting[bus_name]
            self._adapter_back.add(bus_name)
            self._ctx.events.report_requested.emit(
                f"Adapter for bus {bus_name} is back — click its status dot in Buses to reconnect.",
                "info",
            )

    def _on_load_dbc(self) -> None:
        """Import a DBC and bind it to the currently selected bus."""
        if self._ctx is None or self._loader is not None:
            return
        # The button is disabled while no bus is selected, so reaching
        # this without a target is a defensive bail.
        target = self._selected_bus_name
        if target is None:
            return
        path_str, _filter = QFileDialog.getOpenFileName(
            self, "Load DBC", "", "DBC files (*.dbc);;All files (*)"
        )
        if not path_str:
            return
        path = Path(path_str)
        # Already-loaded: skip the worker and just additively bind.
        if self._ctx.database.decoder_for_path(path) is not None:
            self._ctx.database.bind_dbc(path, target)
            self._persist_dbc_bindings()
            return
        self._loader_target_bus = target
        self._dbc_load_btn.setEnabled(False)
        self._dbc_loading_label.setText(f"Loading {path.name}…")
        self._dbc_loading_label.setVisible(True)
        self._dbc_loading_bar.setVisible(True)
        self._loader = DbcLoader(path)
        # No QObject parent: lifetime is managed via signal-driven
        # deleteLater. Same reasoning as ``_start_prober`` — parenting
        # to the dock would let qtbot dispose the widget mid-parse.
        self._loader_thread = QThread()
        self._loader.moveToThread(self._loader_thread)
        self._loader_thread.started.connect(self._loader.run)
        self._loader.loaded.connect(self._on_dbc_loaded)
        self._loader.failed.connect(self._on_dbc_load_failed)
        self._loader.finished.connect(self._on_dbc_load_finished)
        # DirectConnection so the worker thread itself triggers quit;
        # otherwise the queued slot would target the main thread, which
        # may already be tearing down by the time it lands.
        self._loader.finished.connect(self._loader_thread.quit, Qt.ConnectionType.DirectConnection)
        self._loader.finished.connect(self._loader.deleteLater)
        self._loader_thread.finished.connect(self._loader_thread.deleteLater)
        self._loader_thread.start()

    def _on_dbc_loaded(self, path: Path, decoder: object) -> None:
        if self._ctx is None or not isinstance(decoder, Decoder):
            return
        if not self._ctx.database.attach_loaded_decoder(path, decoder):
            return
        target = self._loader_target_bus
        if target is not None:
            self._ctx.database.bind_dbc(path, target)
        self._persist_dbc_bindings()

    def _on_dbc_load_failed(self, path: Path, reason: str) -> None:
        if self._ctx is None:
            return
        self._ctx.database.error.emit(f"Failed to load DBC {path!s}: {reason}")

    def _on_dbc_load_finished(self) -> None:
        self._loader = None
        self._loader_thread = None
        self._loader_target_bus = None
        self._dbc_loading_bar.setVisible(False)
        self._dbc_loading_label.setVisible(False)
        self._dbc_loading_label.clear()
        # Selection may have changed while parsing — recompute the
        # button's enabled state from current state rather than blindly
        # re-enabling it.
        self._recompute_selected_bus()
