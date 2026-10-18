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
    Buses       — declared buses with bound DBCs as child rows; live
                  ones get a filled dot. Two header actions:
                    ``bus_plus`` (Add bus…)
                    ``database_plus`` (Add database to selected bus…,
                    disabled until a bus is selected).
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
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
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

from ithildin.core.decoder import Decoder
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
from ithildin.workspace.schema import DbcRef

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


LiveState = Literal["live", "stopped", "available", "unavailable"]
"""Per-row liveness:

- ``live`` — this is the bus the ``BusReader`` is currently capturing.
- ``stopped`` — this bus was opened earlier in this session but its
  worker has exited (user disconnect, or hardware unplug / error).
- ``available`` — a detected adapter matches this row's
  ``(interface, channel)``; never opened in this session.
- ``unavailable`` — declared bus has no matching detected adapter.
"""

_DOT_FOR_STATE: dict[LiveState, str] = {
    "live": "●",
    "stopped": "◌",
    "available": "○",
    "unavailable": "⊘",
}


_BUS_ROW_ROLE = Qt.ItemDataRole.UserRole + 1
_DBC_PATH_ROLE = Qt.ItemDataRole.UserRole + 2


class _BusRow:
    """Lightweight container for a bus row's metadata.

    Stored on each top-level ``QStandardItem`` via ``setData`` so the
    drop handler and context-menu builder can recover the bus name,
    matching adapter info, and droppability without text parsing.
    """

    __slots__ = (
        "accepts_drops",
        "bus_name",
        "channel",
        "interface",
        "listen_only",
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
    ) -> None:
        self.bus_name = bus_name
        self.interface = interface
        self.channel = channel
        self.state = state
        self.accepts_drops = accepts_drops
        self.listen_only = listen_only


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

    Per-bus expand state is tracked by name across model rebuilds so
    loading another DBC (which triggers a refresh) doesn't snap
    collapsed buses back open.
    """

    bind_requested = Signal(str, str)  # (bus_name, dbc_path_str)
    unbind_requested = Signal(object, str)  # (Path, bus_name)
    remove_bus_requested = Signal(str)  # bus_name
    signal_centric_toggled = Signal(object, bool)  # (Path, new_value)

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

        # Names of buses the user has explicitly collapsed. Survives
        # model rebuilds — see ``set_rows``.
        self._collapsed_buses: set[str] = set()
        self.expanded.connect(self._on_index_expanded)
        self.collapsed.connect(self._on_index_collapsed)

        self._signal_centric_getter: Callable[[Path], bool] | None = None

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
        """Repopulate the tree from a flat list of (bus, dbcs) pairs."""
        self._tree_model.removeRows(0, self._tree_model.rowCount())
        for bus, dbc_paths in rows:
            parent_item = QStandardItem(_format_bus_label(bus))
            parent_item.setData(bus, _BUS_ROW_ROLE)
            parent_item.setEditable(False)
            if bus.listen_only:
                parent_item.setToolTip("Listen only — this bus cannot transmit.")
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
            remove_action = menu.addAction(f"Remove {bus.bus_name}")
            bus_name = bus.bus_name
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
        self._adapter_rows: list[AdapterChoice] = []
        self._failed_backends: list[tuple[str, str]] = []
        self._probe_finished = False
        self._prober: AdapterProber | None = None
        self._prober_thread: QThread | None = None
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
        ctx.bus_reader.bus_started.connect(self._on_bus_state_changed)
        ctx.bus_reader.bus_stopped.connect(self._on_bus_state_changed)
        if self._bus_tree is not None:
            self._bus_tree.set_signal_centric_getter(ctx.database.is_signal_centric)
        self._refresh_dbc_lists()
        self._refresh_buses()

    def start_adapter_probe(self) -> None:
        """Kick off background adapter detection.

        Separate from ``bind`` so that tests which only care about the
        DBC-driven models don't spawn a worker thread that has to be
        torn down. ``MainWindow`` calls this once after ``show()``.
        """
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
        detected: dict[tuple[str, object], AdapterChoice] = {
            (a.interface, a.channel): a for a in self._adapter_rows
        }
        # Group bound DBCs by bus name so we can render them as child rows.
        dbcs_per_bus: dict[str, list[Path]] = {}
        for path in self._ctx.database.loaded_paths():
            for bus in self._ctx.database.buses_for_path(path):
                dbcs_per_bus.setdefault(bus, []).append(path)

        rows: list[tuple[_BusRow, list[Path]]] = []
        for cfg in project.workspace.bus:
            state: LiveState
            if cfg.name in live_names:
                state = "live"
            elif cfg.name in ever_opened:
                state = "stopped"
            elif (cfg.interface, cfg.channel) in detected:
                state = "available"
            else:
                state = "unavailable"
            rows.append(
                (
                    _BusRow(
                        bus_name=cfg.name,
                        interface=cfg.interface,
                        channel=cfg.channel,
                        state=state,
                        accepts_drops=True,
                        listen_only=cfg.listen_only,
                    ),
                    sorted(dbcs_per_bus.get(cfg.name, []), key=lambda p: p.name),
                )
            )

        tree.set_rows(rows)
        tree.setVisible(bool(rows))
        self._buses_hint.setVisible(not rows)
        # Tree rebuild wipes selection; re-sync the cached bus name and
        # the Add-DBC button's enabled state.
        self._recompute_selected_bus()

        # Failed-backends warning footer.
        if self._failed_backends:
            text = "  ".join(f"⚠ {name}: {reason}" for name, reason in self._failed_backends)
            self._failed_backends_label.setText(text)
            self._failed_backends_label.setVisible(True)
        else:
            self._failed_backends_label.setVisible(False)

    def _on_add_bus(self) -> None:
        if self._ctx is None:
            return
        from ithildin.ui.create_bus_dialog import CreateCanBusDialog

        existing = {b.name for b in self._ctx.project.workspace.bus}
        existing_channels = {(b.interface, b.channel) for b in self._ctx.project.workspace.bus}
        dlg = CreateCanBusDialog(
            existing_names=existing,
            existing_channels=existing_channels,
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
        from ithildin.workspace.schema import BusConfig

        if self._ctx is None or not isinstance(cfg, BusConfig):
            return False
        project = self._ctx.project
        if any(b.name == cfg.name for b in project.workspace.bus):
            self._ctx.database.error.emit(f"Bus name {cfg.name!r} already declared; pick another.")
            return False
        if any(
            b.interface == cfg.interface and b.channel == cfg.channel for b in project.workspace.bus
        ):
            self._ctx.database.error.emit(
                f"{cfg.interface} channel {cfg.channel!r} is already used by "
                "another bus; pick a different channel."
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

    def _on_bus_declared_and_connect(self, cfg: object, dbcs: object = ()) -> None:
        from ithildin.workspace.schema import BusConfig

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
        project = self._ctx.project
        project.workspace.bus = [b for b in project.workspace.bus if b.name != bus_name]
        # Cascade: drop any DBC bindings that referenced the removed bus.
        for path in list(self._ctx.database.loaded_paths()):
            if bus_name in self._ctx.database.buses_for_path(path):
                self._ctx.database.unbind_dbc(path, bus_name)
        self._refresh_buses()
        self._save_project_silently()
        self._ctx.events.buses_changed.emit()

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

    def _start_prober(self) -> None:
        if self._prober is not None:
            return
        self._adapter_rows = []
        self._failed_backends = []
        self._probe_finished = False
        self._refresh_buses()
        self._prober = AdapterProber()
        # No QObject parent: lifetime is managed via signal-driven
        # deleteLater. Parenting to the dock would let qtbot dispose
        # the widget while the worker thread is still mid-probe, and
        # destroying a running QThread is undefined behaviour.
        self._prober_thread = QThread()
        self._prober.moveToThread(self._prober_thread)
        self._prober_thread.started.connect(self._prober.run)
        self._prober.adapter_found.connect(self._on_adapter_found)
        self._prober.backend_failed.connect(self._on_backend_failed)
        self._prober.finished.connect(self._on_prober_finished)
        # DirectConnection so the worker thread itself triggers quit;
        # otherwise the queued slot would target the main thread, which
        # may already be tearing down by the time it lands.
        self._prober.finished.connect(self._prober_thread.quit, Qt.ConnectionType.DirectConnection)
        self._prober.finished.connect(self._prober.deleteLater)
        self._prober_thread.finished.connect(self._prober_thread.deleteLater)
        self._prober_thread.start()
        # Cancel + join this probe if the app quits mid-enumeration.
        register_prober(self._prober, self._prober_thread)

    def _on_adapter_found(self, adapter: AdapterChoice) -> None:
        self._adapter_rows.append(adapter)
        self._refresh_buses()

    def _on_backend_failed(self, backend: str, reason: str) -> None:
        self._failed_backends.append((backend, reason))
        self._refresh_buses()

    def _on_prober_finished(self) -> None:
        self._probe_finished = True
        self._refresh_buses()

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
