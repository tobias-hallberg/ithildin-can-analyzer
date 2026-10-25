# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Transmit (TX) window — see ``design_handoff_tx_window/`` for the canonical design.

Two-tier control:

* **Master switch** (top toolbar) — single gate on every transmit. OFF
  means no frame leaves the bus regardless of per-row state. Toggling
  OFF then ON returns to the prior set of active transmits.
* **Per-row active flag** — each row has its own start/stop button.
  Effective transmission = ``master_enabled && tx.active``.

The window owns a :class:`TxScheduler` (``core.transmit``) and shares
its :class:`TxSetup` with it. Closing the window calls
:meth:`TxScheduler.shutdown` — equivalent to master OFF plus stopping
every cyclic task. When the scheduler disarms (a bus closed, lost or
newly in error, a periodic task failed) the window shows why in an amber banner and in the
Report dock; the user re-arms by hand.

Visuals follow ``design_handoff_tx_window/TX window.html``: large pill
master switch, custom list-row delegate (tri-state run button + period
badge + signal summary), per-signal controls dispatched by DBC kind,
live payload preview at the bottom of the editor, and a status bar
that paints accent-green when master is ON.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.transmit import (
    TxMessage,
    TxScheduler,
    TxSetup,
    TxSignalSpec,
    build_signal_spec,
    inactive_signals,
    resolve_kind,
)
from ithildin.core.types import BusHealth
from ithildin.ui._child_base import ChildView
from ithildin.ui._drag_payload import MessagePayload, SignalPayload
from ithildin.ui._signal_drop import SignalDropArea
from ithildin.ui.banner import Banner
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import themed_icon
from ithildin.ui.transmit.controls import control_for_signal
from ithildin.ui.transmit.editor_header import ActionButton, ModeSegmented, PeriodField
from ithildin.ui.transmit.list_delegate import TX_ROW_ROLE, TxListDelegate
from ithildin.ui.transmit.master_switch import TxMasterPill
from ithildin.ui.transmit.payload_preview import TxPayloadPreview
from ithildin.ui.view_header import ViewHeaderBar

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext


_ERROR_TOAST_MS = 4000
_BLINK_INTERVAL_MS = 500


class TransmitWindow(ChildView):
    """The Transmit MDI child view."""

    KIND = "transmit"
    DEFAULT_SIZE = (920, 560)
    TITLE = "Transmit"

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, params=params)
        self._ctx: AppContext | None = None
        self._scheduler: TxScheduler | None = None
        self._setup = TxSetup()
        self._selected: int = -1
        self._signal_widgets: dict[str, QWidget] = {}
        self._signal_rows: dict[str, QWidget] = {}
        # The editor's message, kept so a selector change can re-filter rows.
        self._editor_message: Any = None
        self._selector_names: set[str] = set()
        # Block re-entrancy while we programmatically populate signal
        # widgets — value-changed signals fire while restoring and would
        # otherwise stomp the values we just loaded.
        self._suspend_signal_capture = False
        # Slider/spinner drags fire value_changed every tick; debounce the
        # scheduler reconcile so a hammered slider doesn't re-encode every
        # active periodic TX on every event-loop turn.
        self._apply_debounce = QTimer(self)
        self._apply_debounce.setSingleShot(True)
        self._apply_debounce.setInterval(30)
        self._apply_debounce.timeout.connect(self._apply)
        self._build_body()

    # ---- ChildView API ----

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        from ithildin.app_state.bus_reader import BusReader

        bus_reader: BusReader = ctx.bus_reader
        self._scheduler = TxScheduler(bus_reader, ctx.database, parent=self)
        self._scheduler.apply(self._setup)
        self._scheduler.error.connect(self._on_scheduler_error)
        self._scheduler.disarmed.connect(self._on_disarmed)
        self._list_delegate.set_transmitting_predicate(self._scheduler.is_transmitting)
        self._payload_preview.set_database(ctx.database)
        # Bound methods, not lambdas: BusReader outlives this window, and a
        # lambda would keep calling into it after it is deleted.
        bus_reader.bus_started.connect(self._on_bus_topology_changed)
        bus_reader.bus_stopped.connect(self._on_bus_topology_changed)
        bus_reader.bus_health_changed.connect(self._on_bus_health_changed)
        self._refresh_listen_only_gate()
        self._refresh_status_bar()

    def save_state(self) -> dict[str, object]:
        state = super().save_state()
        state["tx_setup"] = {
            "master_enabled": bool(self._setup.master_enabled),
            "transmits": [
                {
                    "bus_name": tx.bus_name,
                    "message_id": int(tx.message_id),
                    "message_name": tx.message_name,
                    "mode": tx.mode,
                    "period_ms": int(tx.period_ms),
                    "active": bool(tx.active),
                    "signal_values": dict(tx.signal_values),
                    "kind_overrides": dict(tx.kind_overrides),
                }
                for tx in self._setup.transmits
            ],
        }
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        super().restore_state(state)
        raw = state.get("tx_setup")
        if not isinstance(raw, dict):
            return
        setup = TxSetup()
        # master always starts OFF on reload for safety — the user
        # explicitly re-arms after a project open.
        setup.master_enabled = False
        for entry in raw.get("transmits", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                tx = TxMessage(
                    bus_name=str(entry["bus_name"]),
                    message_id=int(entry["message_id"]),
                    message_name=str(entry["message_name"]),
                    mode=cast(Any, entry.get("mode", "one-shot")),
                    period_ms=int(entry.get("period_ms", 100) or 100),
                    active=bool(entry.get("active", False)),
                    signal_values=dict(entry.get("signal_values") or {}),
                    kind_overrides=dict(entry.get("kind_overrides") or {}),
                )
            except (KeyError, TypeError, ValueError):
                continue
            setup.transmits.append(tx)
        self._setup = setup
        self._rebuild_list()
        self._select(0 if self._setup.transmits else -1)
        self._sync_master_button()
        self._apply()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # A trailing edit must not reconcile after shutdown; the window can
        # outlive close() until its deferred delete runs.
        self._apply_debounce.stop()
        if self._scheduler is not None:
            self._scheduler.shutdown()
        super().closeEvent(event)

    # ---- Build ----

    def _build_body(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Hidden until maximized, when the native title bar collapses and
        # this becomes the only place to restore/close the window.
        self.header = ViewHeaderBar(self, hide_when_idle=True)
        outer.addWidget(self.header)

        # Build the status label first — ``_sync_master_button`` (called
        # from the toolbar build) flips its object name, so it has to
        # exist before the toolbar wires its toggle.
        status_widget = self._build_status_bar()
        outer.addWidget(self._build_toolbar())
        # The disarm is global, so its banner sits under the toolbar rather
        # than in the row editor.
        self._disarm_banner = Banner(kind="warn", parent=self)
        self._disarm_banner.dismissed.connect(self._refresh_master_off_banner)
        outer.addWidget(self._disarm_banner)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_list_pane())
        splitter.addWidget(self._build_editor_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 660])
        outer.addWidget(splitter, 1)

        outer.addWidget(status_widget)
        # Apply the empty-state visibility once at construction so the
        # mode picker / period field / action button start hidden when
        # there's nothing selected yet.
        self._populate_editor()

    def _build_toolbar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("TxToolbar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(48)
        bar.setFrameShape(QFrame.Shape.NoFrame)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)

        self._master_btn = TxMasterPill(bar)
        self._master_btn.toggled.connect(self._on_master_toggled)
        self._sync_master_button()
        row.addWidget(self._master_btn)

        self._count_label = QLabel("0 / 0", bar)
        self._count_label.setObjectName("TxCountNumber")
        self._count_label.setFont(tabular_font())
        self._count_caption = QLabel("TRANSMITTING", bar)
        self._count_caption.setObjectName("TxCountCaption")
        self._count_caption.setFont(tabular_font())
        count_box = QVBoxLayout()
        count_box.setContentsMargins(0, 0, 0, 0)
        count_box.setSpacing(0)
        count_box.addWidget(self._count_label)
        count_box.addWidget(self._count_caption)
        count_wrap = QFrame(bar)
        count_wrap.setLayout(count_box)
        row.addWidget(count_wrap)

        sep = QFrame(bar)
        sep.setObjectName("TxToolbarSep")
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setFixedHeight(24)
        row.addWidget(sep)

        self._start_all_btn = QToolButton(bar)
        self._start_all_btn.setObjectName("TxBulkBtn")
        self._start_all_btn.setText("Start all")
        themed_icon("play", self._start_all_btn.setIcon)
        self._start_all_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._start_all_btn.clicked.connect(self._on_start_all)
        row.addWidget(self._start_all_btn)

        self._stop_all_btn = QToolButton(bar)
        self._stop_all_btn.setObjectName("TxBulkBtn")
        self._stop_all_btn.setText("Stop all")
        themed_icon("stop", self._stop_all_btn.setIcon)
        self._stop_all_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._stop_all_btn.clicked.connect(self._on_stop_all)
        row.addWidget(self._stop_all_btn)

        row.addStretch(1)
        return bar

    def _build_list_pane(self) -> QWidget:
        wrap = QFrame(self)
        wrap.setObjectName("TxListPane")
        wrap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        wrap.setMinimumWidth(220)
        vbox = QVBoxLayout(wrap)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        header = QFrame(wrap)
        header.setObjectName("TxListHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header.setFixedHeight(28)
        hrow = QHBoxLayout(header)
        hrow.setContentsMargins(10, 0, 6, 0)
        hrow.setSpacing(6)
        title = QLabel("TRANSMITS", header)
        title.setObjectName("TxListTitle")
        title.setFont(tabular_font())
        hrow.addWidget(title)
        self._count_in_header = QLabel("0", header)
        self._count_in_header.setObjectName("TxListCount")
        self._count_in_header.setFont(tabular_font())
        hrow.addWidget(self._count_in_header)
        hrow.addStretch(1)
        vbox.addWidget(header)

        self._list_model = QStandardItemModel(0, 1, self)
        self._list_view = QListView(wrap)
        self._list_view.setObjectName("TxListView")
        self._list_view.setModel(self._list_model)
        self._list_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._list_view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._list_view.setFont(tabular_font())
        self._list_view.setSpacing(0)
        self._list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list_view.customContextMenuRequested.connect(self._on_list_context_menu)
        # Delete/Backspace removes the focused row. We watch keys via an
        # event filter rather than subclassing QListView.
        self._list_view.installEventFilter(self)
        self._list_delegate = TxListDelegate(self._list_view)
        self._list_delegate.set_value_formatter(self._summary_value)
        self._list_view.setItemDelegate(self._list_delegate)
        self._list_delegate.active_toggled.connect(self._on_delegate_toggle)
        self._list_view.selectionModel().currentChanged.connect(self._on_row_changed)

        # Blink at ~2Hz so the period-badge dot pulses while transmitting.
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(_BLINK_INTERVAL_MS)
        self._blink_timer.timeout.connect(self._on_blink)
        self._blink_timer.start()

        drop = SignalDropArea(wrap)
        drop.payload_dropped.connect(self._on_payload_dropped)
        drop.set_inner(self._list_view)
        vbox.addWidget(drop, 1)
        return wrap

    def _build_editor_pane(self) -> QWidget:
        wrap = QFrame(self)
        wrap.setObjectName("TxEditorPane")
        wrap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        vbox = QVBoxLayout(wrap)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self._editor_header = QFrame(wrap)
        self._editor_header.setObjectName("TxEditorHeader")
        self._editor_header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._editor_header.setFixedHeight(48)
        hrow = QHBoxLayout(self._editor_header)
        hrow.setContentsMargins(16, 8, 16, 8)
        hrow.setSpacing(10)

        self._editor_title = QLabel("No transmit selected", self._editor_header)
        self._editor_title.setObjectName("TxEditorTitle")
        hrow.addWidget(self._editor_title, 1)

        self._mode_seg = ModeSegmented(self._editor_header)
        self._mode_seg.changed.connect(self._on_mode_changed)
        hrow.addWidget(self._mode_seg)

        self._period_field = PeriodField(self._editor_header)
        self._period_field.changed.connect(self._on_period_changed)
        hrow.addWidget(self._period_field)

        self._action_btn = ActionButton(self._editor_header)
        self._action_btn.clicked.connect(self._on_action_clicked)
        hrow.addWidget(self._action_btn)
        vbox.addWidget(self._editor_header)

        self._master_off_banner = Banner(kind="warn", parent=wrap)
        vbox.addWidget(self._master_off_banner)

        self._editor_scroll = QScrollArea(wrap)
        self._editor_scroll.setWidgetResizable(True)
        self._editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._editor_body = QWidget(self._editor_scroll)
        self._editor_layout = QVBoxLayout(self._editor_body)
        self._editor_layout.setContentsMargins(0, 0, 0, 0)
        self._editor_layout.setSpacing(0)
        self._editor_layout.addStretch(1)
        self._editor_scroll.setWidget(self._editor_body)
        vbox.addWidget(self._editor_scroll, 1)

        # Live payload preview at the bottom of the editor; database is
        # wired in ``bind`` (the editor pane is constructed before ``bind``).
        self._payload_preview = TxPayloadPreview(wrap)
        vbox.addWidget(self._payload_preview)
        return wrap

    def _build_status_bar(self) -> QWidget:
        self._status_label = QLabel("", self)
        self._status_label.setObjectName("TxStatusBarOff")
        self._status_label.setFixedHeight(22)
        self._status_label.setFont(tabular_font())
        self._status_label.setContentsMargins(12, 0, 12, 0)
        return self._status_label

    # ---- State mutators ----

    def _apply(self) -> None:
        # Gate first: an activation that targets a listen-only bus forces
        # master OFF, so reconciling afterwards won't try (and fail) to
        # start a periodic task on a bus that can't transmit.
        self._refresh_listen_only_gate()
        if self._scheduler is not None:
            self._scheduler.apply(self._setup)
        self._rebuild_list_glyphs()
        self._refresh_counts()
        self._refresh_bulk_buttons()
        self._refresh_status_bar()
        self._refresh_master_off_banner()
        self._refresh_action_button()

    def _select(self, idx: int) -> None:
        self._selected = idx
        if 0 <= idx < self._list_model.rowCount():
            self._list_view.setCurrentIndex(self._list_model.index(idx, 0))
        self._populate_editor()
        self._refresh_status_bar()
        self._refresh_master_off_banner()

    def _rebuild_list(self) -> None:
        self._list_model.clear()
        for tx in self._setup.transmits:
            item = QStandardItem()
            item.setData(tx, TX_ROW_ROLE)
            # The delegate elides long names to keep clear of the badge.
            item.setToolTip(f"{tx.message_name}  0x{tx.message_id:X}  ·  {tx.bus_name}")
            item.setEditable(False)
            self._list_model.appendRow(item)
        self._count_in_header.setText(str(len(self._setup.transmits)))

    def _populate_editor(self) -> None:
        # Clear any previous signal rows.
        while self._editor_layout.count() > 1:  # keep the trailing stretch
            item = self._editor_layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._signal_widgets.clear()
        self._signal_rows.clear()
        self._editor_message = None
        self._selector_names = set()

        if not (0 <= self._selected < len(self._setup.transmits)):
            # Empty state: hide every header widget (rather than leave them
            # disabled-but-visible, which reads as "broken"), surface a
            # centred dim hint in the body, clear the payload preview.
            self._editor_title.setText("No transmit selected")
            self._mode_seg.setVisible(False)
            self._period_field.setVisible(False)
            self._action_btn.setVisible(False)
            hint = QLabel(
                "Drop a message here to add a transmit",
                self._editor_body,
            )
            hint.setObjectName("TxEmptyHint")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._editor_layout.insertWidget(0, hint)
            self._payload_preview.clear()
            return

        # Restore header widgets after an empty state.
        self._mode_seg.setVisible(True)
        self._period_field.setVisible(True)
        self._action_btn.setVisible(True)

        tx = self._setup.transmits[self._selected]
        self._editor_title.setText(f"{tx.message_name}  ·  0x{tx.message_id:X}")

        self._suspend_signal_capture = True
        try:
            self._mode_seg.setEnabled(True)
            self._mode_seg.set_mode(tx.mode)
            self._period_field.setVisible(True)
            self._period_field.setEnabled(tx.mode == "periodic")
            self._period_field.set_value(int(tx.period_ms))
        finally:
            self._suspend_signal_capture = False

        self._action_btn.setEnabled(True)
        self._action_btn.set_state(tx.mode, tx.active, master_on=self._setup.master_enabled)

        decoder = self._ctx.database.decoder_for_bus(tx.bus_name) if self._ctx else None
        if decoder is None:
            self._editor_layout.insertWidget(
                0,
                QLabel(f"No DBC bound to bus {tx.bus_name!r} — cannot edit signals.", self),
            )
            self._payload_preview.clear()
            return
        try:
            msg = decoder.database.get_message_by_frame_id(tx.message_id)
        except KeyError:
            self._editor_layout.insertWidget(
                0,
                QLabel(
                    f"Message 0x{tx.message_id:X} no longer in DBC — remove the row.",
                    self,
                ),
            )
            self._payload_preview.clear()
            return

        self._editor_message = msg
        self._selector_names = {sig.name for sig in msg.signals if sig.is_multiplexer}
        self._suspend_signal_capture = True
        try:
            for sig in msg.signals:
                kind = resolve_kind(tx, sig, msg)
                spec = build_signal_spec(sig, msg, kind=kind, values=tx.signal_values)
                row = self._build_signal_row(spec, tx)
                self._signal_rows[spec.name] = row
                self._editor_layout.insertWidget(self._editor_layout.count() - 1, row)
        finally:
            self._suspend_signal_capture = False
        self._refresh_branch_rows(tx)

        # Live payload preview reflecting current overrides.
        self._payload_preview.update_for(tx)

    def _build_signal_row(self, spec, tx: TxMessage) -> QWidget:  # type: ignore[no-untyped-def]
        row = QFrame(self._editor_body)
        row.setObjectName("TxSignalRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(16, 6, 16, 6)
        rl.setSpacing(12)

        name_box = QVBoxLayout()
        name_box.setContentsMargins(0, 0, 0, 0)
        name_box.setSpacing(0)
        name_lbl = QLabel(spec.name, row)
        name_lbl.setObjectName("TxSignalName")
        name_lbl.setFont(tabular_font())
        name_box.addWidget(name_lbl)
        name_wrap = QFrame(row)
        name_wrap.setLayout(name_box)
        name_wrap.setMinimumWidth(140)
        name_wrap.setMaximumWidth(180)
        rl.addWidget(name_wrap)

        def _emit(v: Any, name: str = spec.name) -> None:
            self._on_signal_changed(name, v)

        control = control_for_signal(spec, on_change=_emit, parent=self._editor_body)
        self._signal_widgets[spec.name] = control
        rl.addWidget(control, 1)

        kind_label = QLabel(_type_badge(spec), row)
        kind_label.setObjectName("TxKindBadge")
        kind_label.setFont(tabular_font())
        rl.addWidget(kind_label)
        return row

    def _summary_value(self, tx: TxMessage, name: str, value: object) -> str | None:
        """List-summary text for hex-kind values: the raw bits, as the editor's grid shows them."""
        decoder = self._ctx.database.decoder_for_bus(tx.bus_name) if self._ctx else None
        if decoder is None or not isinstance(value, int):
            return None
        try:
            msg = decoder.database.get_message_by_frame_id(tx.message_id)
            sig = msg.get_signal_by_name(name)
        except KeyError:
            return None
        if resolve_kind(tx, sig, msg) != "hex":
            return None
        raw = (value - int(sig.offset)) // int(sig.scale)
        length = int(sig.length)
        return f"0x{raw & ((1 << length) - 1):0{(length + 7) // 8 * 2}X}"

    def _refresh_branch_rows(self, tx: TxMessage) -> None:
        """Show only the rows the current multiplexer selection will send."""
        if not self._selector_names:
            return
        inactive = inactive_signals(self._editor_message, tx.signal_values)
        for name, row in self._signal_rows.items():
            row.setVisible(name not in inactive)

    def _on_signal_changed(self, signal_name: str, value: Any) -> None:
        if self._suspend_signal_capture:
            return
        if not (0 <= self._selected < len(self._setup.transmits)):
            return
        tx = self._setup.transmits[self._selected]
        tx.signal_values[signal_name] = value
        if signal_name in self._selector_names:
            self._refresh_branch_rows(tx)
        self._payload_preview.update_for(tx)
        # Debounce: re-encoding every active periodic TX is cheap but not
        # free, and a slider drag fires hundreds of value_changed events
        # per second. Trail the reconcile by ~30ms; mode/active/period
        # changes still take the immediate ``_apply`` path.
        self._apply_debounce.start()

    # ---- Slots ----

    def _on_master_toggled(self, checked: bool) -> None:
        if self._scheduler is None:
            self._setup.master_enabled = checked
        else:
            self._scheduler.set_master(checked)
            if self._setup.master_enabled:
                self._disarm_banner.hide()
        self._sync_master_button()
        self._refresh_counts()
        self._refresh_status_bar()
        self._refresh_master_off_banner()
        # Every row's glyph and the selected row's action button are both
        # gated on master; neither repaints itself.
        self._rebuild_list_glyphs()
        self._refresh_action_button()

    def _on_start_all(self) -> None:
        for tx in self._setup.transmits:
            tx.active = True
        self._apply()

    def _on_stop_all(self) -> None:
        for tx in self._setup.transmits:
            tx.active = False
        self._apply()

    def _on_payload_dropped(self, payload: object) -> None:
        if isinstance(payload, (SignalPayload, MessagePayload)):
            self._add_from_payload(payload.message, int(payload.message_id))

    def _add_from_payload(self, message_name: str, message_id: int) -> None:
        bus_name = self._resolve_bus(message_id)
        if bus_name is None:
            self._toast("No bus has a DBC with this message — bind a DBC first.")
            return
        self._on_transmit_chosen(bus_name, message_id, message_name)

    def _resolve_bus(self, message_id: int) -> str | None:
        """First bus whose bound DBC defines ``message_id`` (single-bus in v0.1).

        Dropped payloads carry no bus; mirror ``Database.decoder_for_message``'s
        first-match rule, but key on the frame id the TX row will encode with.
        """
        if self._ctx is None:
            return None
        for cfg in self._ctx.project.workspace.bus:
            decoder = self._ctx.database.decoder_for_bus(cfg.name)
            if decoder is None:
                continue
            try:
                decoder.database.get_message_by_frame_id(message_id)
            except KeyError:
                continue
            return cfg.name
        return None

    def _on_transmit_chosen(self, bus_name: str, message_id: int, message_name: str) -> None:
        # If the user already added this (bus, message_id) pair, jump to
        # the existing row instead of growing the list with a duplicate.
        for i, existing in enumerate(self._setup.transmits):
            if existing.bus_name == bus_name and existing.message_id == int(message_id):
                self._select(i)
                self._toast(f"Already in list: {message_name}")
                return
        tx = TxMessage(
            bus_name=bus_name,
            message_id=int(message_id),
            message_name=message_name,
            mode="one-shot",
            period_ms=100,
            active=False,  # safety default — handoff §15 Q4
        )
        # Capture the prior selection BEFORE rebuilding: _rebuild_list()
        # calls model.clear(), which fires currentChanged(invalid) and
        # clobbers self._selected to -1. We must restore from this local.
        prior = self._selected
        self._setup.transmits.append(tx)
        self._rebuild_list()
        # First-ever transmit auto-selects; subsequent adds leave the
        # user's focus alone so they don't lose their editing context.
        self._select(len(self._setup.transmits) - 1 if prior < 0 else prior)
        self._refresh_counts()
        self._refresh_bulk_buttons()

    def _on_row_changed(self, current, _previous) -> None:  # type: ignore[no-untyped-def]
        idx = current.row() if current.isValid() else -1
        self._selected = idx
        self._populate_editor()
        self._refresh_status_bar()
        self._refresh_master_off_banner()

    def _on_mode_changed(self, mode: str) -> None:
        if self._suspend_signal_capture:
            return
        if not (0 <= self._selected < len(self._setup.transmits)):
            return
        tx = self._setup.transmits[self._selected]
        tx.mode = "periodic" if mode == "periodic" else "one-shot"
        self._period_field.setEnabled(tx.mode == "periodic")
        self._apply()

    def _on_period_changed(self, value: int) -> None:
        if self._suspend_signal_capture:
            return
        if not (0 <= self._selected < len(self._setup.transmits)):
            return
        tx = self._setup.transmits[self._selected]
        tx.period_ms = int(value)
        self._apply()

    def _on_action_clicked(self) -> None:
        if not (0 <= self._selected < len(self._setup.transmits)):
            return
        tx = self._setup.transmits[self._selected]
        if self._scheduler is None:
            return
        if tx.mode == "one-shot":
            self._scheduler.send_one_shot(self._selected)
            return
        # periodic: toggle row active
        tx.active = not tx.active
        self._apply()

    def _on_delegate_toggle(self, row: int) -> None:
        if not (0 <= row < len(self._setup.transmits)):
            return
        tx = self._setup.transmits[row]
        tx.active = not tx.active
        # Clicking the run-state button on a non-selected row should bring
        # focus to that row so the editor follows the toggle. Otherwise the
        # user toggles row 3 but keeps editing row 1, which is confusing.
        if row != self._selected:
            self._select(row)
        self._apply()

    def _on_blink(self) -> None:
        # Toggle the badge dot's phase; force a viewport repaint so the
        # delegate redraws every visible row with the new blink state.
        self._list_delegate.blink_tick()
        self._list_view.viewport().update()

    def _on_list_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QMenu

        index = self._list_view.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        menu = QMenu(self._list_view)
        remove_action = menu.addAction("Remove transmit")
        chosen = menu.exec(self._list_view.viewport().mapToGlobal(pos))
        if chosen is remove_action:
            self._remove_at(row)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[no-untyped-def]
        from PySide6.QtCore import QEvent

        if (
            obj is self._list_view
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
        ):
            idx = self._list_view.currentIndex()
            if idx.isValid():
                self._remove_at(idx.row())
                return True
        return super().eventFilter(obj, event)

    def _remove_at(self, row: int) -> None:
        if not (0 <= row < len(self._setup.transmits)):
            return
        self._setup.transmits.pop(row)
        self._rebuild_list()
        new_count = len(self._setup.transmits)
        next_sel = min(row, new_count - 1) if new_count else -1
        self._select(next_sel)
        self._refresh_counts()
        self._refresh_bulk_buttons()
        self._apply()

    def _on_scheduler_error(self, _mid: int, message: str) -> None:
        self._toast(message)

    def _on_disarmed(self, reason: str) -> None:
        self._sync_master_button()
        self._disarm_banner.show_message(
            f"{reason} — TX disarmed. Switch TX Master on to transmit again."
        )
        if self._ctx is not None:
            self._ctx.events.report_requested.emit(f"TX disarmed: {reason}", "warn")
        self._refresh_counts()
        self._refresh_status_bar()
        self._refresh_master_off_banner()
        self._rebuild_list_glyphs()
        self._refresh_action_button()

    def _on_bus_topology_changed(self, _name: str) -> None:
        # A reopened bus may now be listen-only; a closed one shows as such.
        self._refresh_listen_only_gate()
        self._refresh_counts()
        self._refresh_status_bar()
        self._rebuild_list_glyphs()

    def _on_bus_health_changed(self, _name: str, _state: str, _detail: str) -> None:
        self._refresh_status_bar()

    # ---- Refresh helpers ----

    def _sync_master_button(self) -> None:
        on = self._setup.master_enabled
        # Drive the pill animation programmatically when state changed from
        # outside (restore / listen-only revert). Skip when the pill is
        # already in sync to avoid re-triggering the animation on idle calls.
        self._master_btn.set_enabled_state(on)
        self._status_label.setObjectName("TxStatusBarOn" if on else "TxStatusBarOff")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _transmitting_count(self) -> int:
        if self._scheduler is None:
            return 0
        return sum(1 for tx in self._setup.transmits if self._scheduler.is_transmitting(tx))

    def _refresh_counts(self) -> None:
        self._count_label.setText(f"{self._transmitting_count()} / {len(self._setup.transmits)}")

    def _refresh_bulk_buttons(self) -> None:
        total = len(self._setup.transmits)
        active = sum(1 for tx in self._setup.transmits if tx.active)
        self._start_all_btn.setEnabled(total > 0 and active < total)
        self._stop_all_btn.setEnabled(total > 0 and active > 0)

    def _refresh_status_bar(self) -> None:
        on = self._setup.master_enabled
        if 0 <= self._selected < len(self._setup.transmits):
            tx = self._setup.transmits[self._selected]
            sel = f"0x{tx.message_id:X} · {tx.message_name}"
            bus_info = self._bus_info_text(tx.bus_name)
        else:
            sel = "(no selection)"
            bus_info = self._bus_info_text(None)
        transmitting = self._transmitting_count()
        prefix = "● BUS TX ENABLED" if on else "○ BUS TX DISABLED"
        sep = "      "
        # Handoff §8 lists a trailing ``bus load N%`` field; ithildin has
        # no bus-load metric yet (handoff §15 Q2 flags it as deferred), so
        # we leave it off rather than fake a value.
        self._status_label.setText(
            f"{prefix}{sep}"
            f"{transmitting}/{len(self._setup.transmits)} transmitting{sep}"
            f"{sel}{sep}"
            f"{bus_info}"
        )

    def _refresh_master_off_banner(self) -> None:
        show = False
        # The disarm banner already says why master is OFF; don't stack two.
        if self._disarm_banner.isHidden() and 0 <= self._selected < len(self._setup.transmits):
            tx = self._setup.transmits[self._selected]
            show = tx.active and not self._setup.master_enabled
        if show:
            self._master_off_banner.show_message(
                "TX Master is OFF — this message is queued but not transmitting."
            )
        else:
            self._master_off_banner.hide()

    def _refresh_listen_only_gate(self) -> None:
        if self._ctx is None:
            return
        bus_reader = self._ctx.bus_reader
        listen_only_buses = {
            tx.bus_name
            for tx in self._setup.transmits
            if tx.active and bus_reader.is_listen_only(tx.bus_name)
        }
        if listen_only_buses:
            self._master_btn.setEnabled(False)
            self._master_btn.setToolTip("Active bus is listen-only — cannot transmit.")
            if self._setup.master_enabled and self._scheduler is not None:
                self._scheduler.set_master(False)
                self._sync_master_button()
        else:
            self._master_btn.setEnabled(True)
            self._master_btn.setToolTip("")

    def _refresh_action_button(self) -> None:
        if not (0 <= self._selected < len(self._setup.transmits)):
            return
        tx = self._setup.transmits[self._selected]
        self._action_btn.set_state(tx.mode, tx.active, master_on=self._setup.master_enabled)

    def _rebuild_list_glyphs(self) -> None:
        # The delegate reads live from the mutable TxMessage stashed on
        # each item via TX_ROW_ROLE, so a model rebuild is never needed
        # when only mode / period / active flips on existing rows. A
        # viewport repaint is enough. Doing a model.clear() here would
        # wipe the selection and trigger _on_row_changed(-1), which is
        # what made the editor flash blank on every mode-picker click.
        self._list_view.viewport().update()

    def _bus_info_text(self, bus_name: str | None) -> str:
        if not bus_name or self._ctx is None:
            return ""
        reader = self._ctx.bus_reader
        suffix = "" if bus_name in reader.bus_names() else " (closed)"
        health = reader.health(bus_name)
        if health is not None and health[0] is not BusHealth.OK:
            suffix += f" · {health[0].describe(health[1])}"
        cfg = reader.last_config_for(bus_name)
        if cfg is None:
            # Never opened this session: the project's declaration.
            cfg = next((c for c in self._ctx.project.workspace.bus if c.name == bus_name), None)
        if cfg is None:
            return f"{bus_name}{suffix}"
        return f"{cfg.name} · {cfg.bitrate // 1000} kbps{suffix}"

    def _toast(self, message: str) -> None:
        """Brief status-bar toast — restored to the live status after _ERROR_TOAST_MS."""
        self._status_label.setText(f"⚠ {message}")
        QTimer.singleShot(_ERROR_TOAST_MS, self._refresh_status_bar)


def _type_badge(spec: TxSignalSpec) -> str:
    """Row tag: the control family, or for numbers the signal's real encoding."""
    if spec.kind == "bool":
        return "bool"
    if spec.kind in ("enum-seg", "enum-dd"):
        return "enum"
    if spec.kind == "auto":
        return "auto"
    if spec.is_float:
        return f"f{spec.length_bits}"
    return f"{'i' if spec.is_signed else 'u'}{spec.length_bits}"
