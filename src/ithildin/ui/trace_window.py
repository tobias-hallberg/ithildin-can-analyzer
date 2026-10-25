# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Merged Trace window — bus, DBC, and message-level subscriptions.

Replaces ``MessageTraceWindow`` and ``BusTraceWindow``. The left pane is
a hierarchical subscription tree (BUS → DBC → MESSAGE) that the user
builds by dragging buses, DBCs, signals, or messages onto it. The right
pane is the same frame tree + filter strip + Append/Overwrite toggle +
pause + counters as before, now driven by ``TraceTreeModel``.

Legacy ``message_trace`` and ``bus_trace`` state shapes round-trip
through this window's ``restore_state`` so old ``.itp`` projects keep
opening without a schema bump.

The model is built once in ``bind``. Buses opening and closing only swap
its rings, so rows survive a Disconnect, a failure and a Reconnect; a new
capture session (``session_t0_changed(None)``) clears them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QModelIndex, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.types import HEALTH_RANK, BusHealth, FrameRecord
from ithildin.ui._child_base import ChildView
from ithildin.ui._tracked_tree import TrackedTreePane
from ithildin.ui.banner import Banner
from ithildin.ui.fonts import tabular_font
from ithildin.ui.frame_inspector_view import FrameInspectorView
from ithildin.ui.icons import icon, themed_callback, themed_icon
from ithildin.ui.trace_view import TraceTreeModel
from ithildin.ui.view_header import ViewHeaderBar

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.core.decoder import Decoder

_FILTER_DEBOUNCE_MS = 100
_DEFAULT_LEFT_PANE_WIDTH = 220
_DEFAULT_DETAIL_HEIGHT = 240
_BANNER_HEALTH = (BusHealth.ERRORS, BusHealth.BUS_OFF)


class TraceWindow(ChildView):
    KIND = "trace"
    DEFAULT_SIZE = (920, 420)
    TITLE = "Trace"

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, params=params)
        self._ctx: AppContext | None = None
        self._model: TraceTreeModel | None = None
        self._tracked_pane: TrackedTreePane | None = None
        self._last_left_width = _DEFAULT_LEFT_PANE_WIDTH
        # Used by restore_state when the saved state arrived before
        # ``bind`` had a chance to build the tracked pane.
        self._pending_state: dict[str, object] | None = None
        self._detail_view: FrameInspectorView | None = None
        # Last visible height of the detail pane, captured before the
        # user hides it via the toolbar toggle so the next show restores
        # the same split.
        self._last_detail_height = _DEFAULT_DETAIL_HEIGHT
        # Frame Inspector pin: the (bus, arbitration_id) pair set by the
        # user's last row click. While pinned, the inspector follows the
        # latest known frame for that key as new arrivals drain in.
        # ``_last_pushed`` short-circuits redundant re-renders on drains
        # that didn't bring a new frame for the pinned key. Compared by
        # identity, not seq: a reopened bus's new ring restarts seq at 0.
        self._pinned_key: tuple[str, int] | None = None
        self._last_pushed: FrameRecord | None = None

        self._build_body()

    def _format_title(self, params: dict[str, object]) -> str:
        return "Trace"

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        # Build the tracked pane now that we have a Database to talk to.
        self._tracked_pane = TrackedTreePane(ctx.database, self._splitter)
        self._tracked_pane.model.effective_changed.connect(self._on_effective_changed)
        self._tracked_pane.model.superseded.connect(self._on_superseded)
        self._splitter.insertWidget(0, self._tracked_pane)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([_DEFAULT_LEFT_PANE_WIDTH, 700])

        if self._detail_view is not None:
            self._detail_view.bind(ctx)

        ctx.bus_reader.bus_started.connect(self._on_bus_topology_changed)
        ctx.bus_reader.bus_stopped.connect(self._on_bus_topology_changed)
        ctx.bus_reader.bus_started.connect(self._on_bus_started_clear_banner)
        ctx.bus_reader.bus_stopped.connect(self._on_bus_stopped_clear_health_banner)
        ctx.bus_reader.bus_failed.connect(self._on_bus_failed)
        ctx.bus_reader.bus_health_changed.connect(self._on_bus_health_changed)
        ctx.bus_reader.error.connect(self._on_bus_error)
        ctx.bus_reader.session_t0_changed.connect(self._on_session_t0_changed)
        ctx.database.database_changed.connect(self._on_database_changed)
        self._model = TraceTreeModel(
            ctx.bus_reader.rings,
            decoders=self._bound_decoders(),
            admit=self._tracked_pane.effective().admits,
            bus_reader=ctx.bus_reader,
            parent=self,
        )
        self._model.counters_changed.connect(self._on_counters_changed)
        self._model.counters_changed.connect(self._refresh_pinned_inspector)
        if self._latest_btn.isChecked():
            self._model.set_mode("overwrite")
        # Before setModel, so it runs ahead of the view's and the selection
        # model's own handlers, which would walk the current row along the
        # evicted edge, one row per drain.
        self._model.rowsAboutToBeRemoved.connect(self._on_rows_about_to_be_removed)
        # No proxy in between: see the trace_view module docstring.
        self._tree.setModel(self._model)
        self._tree.selectionModel().currentRowChanged.connect(self._on_row_changed)
        if not self._paused_btn.isChecked():
            self._model.start()

        if self._pending_state is not None:
            self._apply_pending_state()

    def add_payload(self, payload: object) -> None:
        """Subscribe to a dropped bus, DBC, message or signal payload."""
        if self._tracked_pane is not None:
            self._tracked_pane.add_payload(payload)

    # ---- Build ----

    def _build_body(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self._banner = Banner(kind="error", parent=self)
        self._banner.action_clicked.connect(self._on_banner_action)
        self._banner.dismissed.connect(self._on_banner_dismissed)
        # Bus name shown by the most recent bus_failed banner. ``None``
        # when the banner is dismissed or showing a non-failure message.
        self._failed_bus_name: str | None = None
        # Bus whose bus-errors or bus-off state the banner shows, if any.
        self._health_bus: str | None = None
        # Last health per running bus; absent means OK.
        self._health_seen: dict[str, tuple[BusHealth, str]] = {}
        layout.addWidget(self._banner)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        layout.addWidget(self._splitter, 1)

        # Placeholder right pane — tracked pane is inserted in bind().
        right = QWidget(self._splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        self.header = ViewHeaderBar(right)
        strip = self.header

        self._filter_input = QLineEdit(right)
        self._filter_input.setPlaceholderText("Filter ID / name / bus")
        self._filter_debounce = QTimer(self)
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(_FILTER_DEBOUNCE_MS)
        self._filter_debounce.timeout.connect(self._apply_filter)
        self._filter_input.textChanged.connect(self._on_filter_text_changed)

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
        # Must follow addAction — see the same note in left_dock.py.
        self._clear_action.setVisible(False)

        strip.add_widget(self._filter_input, 1)

        # Segmented mode selector: Stream (chronological log) and Latest
        # (per-ID, in-place updates). Both buttons live inside an
        # exclusive QButtonGroup so exactly one is checked at a time.
        # Persistence still writes "append" / "overwrite" so old `.itp`
        # files round-trip unchanged — see save_state / _apply_state.
        self._mode_segment = QFrame(right)
        self._mode_segment.setObjectName("ModeSegment")
        # Fixed vertical policy so the frame stays at its sizeHint
        # (≈ button sizeHint + 2 px border) instead of stretching to
        # match the strip's tallest widget. Strip vcenters it; the
        # surrounding QLineEdit / toggle buttons keep their natural
        # heights.
        self._mode_segment.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        mode_layout = QHBoxLayout(self._mode_segment)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(0)

        self._stream_btn = QToolButton(self._mode_segment)
        self._stream_btn.setText("Stream")
        self._stream_btn.setCheckable(True)
        self._stream_btn.setChecked(True)
        self._stream_btn.setAutoExclusive(False)  # QButtonGroup handles it
        # Expanding vertical policy so the button fills the frame's
        # interior. Without this Qt sizes the button to its sizeHint
        # and the QHBoxLayout vcenters it, leaving the frame's bg
        # showing above and below the :checked fill.
        self._stream_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._stream_btn.setToolTip(
            "Show every frame chronologically; newest at top. While running only the "
            "newest frames stay on screen; pause to scroll the full history."
        )

        self._latest_btn = QToolButton(self._mode_segment)
        self._latest_btn.setObjectName("SegmentRightBtn")
        self._latest_btn.setText("Latest")
        self._latest_btn.setCheckable(True)
        self._latest_btn.setAutoExclusive(False)
        self._latest_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._latest_btn.setToolTip("One row per ID; updates in place. Time column becomes Δt.")

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self._stream_btn)
        self._mode_group.addButton(self._latest_btn)
        self._stream_btn.clicked.connect(lambda: self._on_mode_selected("append"))
        self._latest_btn.clicked.connect(lambda: self._on_mode_selected("overwrite"))

        mode_layout.addWidget(self._stream_btn)
        mode_layout.addWidget(self._latest_btn)
        strip.add_widget(self._mode_segment)

        # Segmented Running / Paused selector — same chrome as the
        # Stream / Latest control above, same backing widgets. Wire
        # persistence still writes the boolean `state["paused"]` so
        # existing .itp files round-trip unchanged.
        self._pause_segment = QFrame(right)
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
        self._running_btn.setToolTip("Frames drain into the trace as they arrive.")

        self._paused_btn = QToolButton(self._pause_segment)
        self._paused_btn.setObjectName("SegmentRightBtn")
        self._paused_btn.setText("Paused")
        self._paused_btn.setCheckable(True)
        self._paused_btn.setAutoExclusive(False)
        self._paused_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._paused_btn.setToolTip(
            "Freeze the trace to scroll its full history; frames arriving while paused "
            "are not shown."
        )

        self._pause_group = QButtonGroup(self)
        self._pause_group.setExclusive(True)
        self._pause_group.addButton(self._running_btn)
        self._pause_group.addButton(self._paused_btn)
        self._running_btn.clicked.connect(lambda: self._on_pause_selected(False))
        self._paused_btn.clicked.connect(lambda: self._on_pause_selected(True))

        pause_layout.addWidget(self._running_btn)
        pause_layout.addWidget(self._paused_btn)
        strip.add_widget(self._pause_segment)

        self._counts_label = QLabel("RX 0   ERR 0", right)
        self._counts_label.setFont(tabular_font())
        self._counts_label.setStyleSheet("color: palette(mid);")
        self._counts_label.setToolTip(
            "RX: frames this trace admitted. ERR: error frames among them, counting "
            "each sampled one as every error frame it stands for. LOST: frames this "
            "trace fell too far behind to show."
        )
        strip.add_widget(self._counts_label)

        self._toggle_left_btn = strip.add_pane_toggle(
            "Show/hide tracked pane", self._on_toggle_left_pane
        )
        self._toggle_detail_btn = strip.add_pane_toggle(
            "Show/hide frame detail pane", self._on_toggle_detail_pane
        )

        right_layout.addWidget(strip)

        self._tree = QTreeView(right)
        self._tree.setFont(tabular_font())
        self._tree.setUniformRowHeights(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setAllColumnsShowFocus(True)
        self._tree.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self._tree.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        h = self._tree.header()
        if h is not None:
            h.setStretchLastSection(True)
            h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._tree.expanded.connect(self._on_expanded)

        # Tree and frame-detail pane stack vertically in a splitter so the
        # user can drag the boundary. The toolbar toggle button hides the
        # detail pane outright; the splitter hands the reclaimed space
        # back to the tree.
        self._detail_splitter = QSplitter(Qt.Orientation.Vertical, right)
        self._detail_splitter.setChildrenCollapsible(False)
        self._detail_splitter.addWidget(self._tree)

        self._detail_view = FrameInspectorView(right)
        # Hidden by default — most users are scanning the trace, not
        # inspecting individual frames. The toolbar toggle reveals it
        # on demand; `_on_toggle_detail_pane` restores the split using
        # `_last_detail_height`.
        self._detail_view.setVisible(False)
        self._detail_splitter.addWidget(self._detail_view)
        self._detail_splitter.setStretchFactor(0, 1)
        self._detail_splitter.setStretchFactor(1, 0)
        self._detail_splitter.setSizes([420, _DEFAULT_DETAIL_HEIGHT])
        right_layout.addWidget(self._detail_splitter, 1)

        # Registered after `_detail_view` exists so the initial fire of
        # the callback (themed_callback invokes immediately) reads the
        # real "hidden" state and paints `pane_bottom_off` — not the
        # `None`-branch fallback that mis-reported the pane as visible.
        themed_callback(self._refresh_toggle_icon)

        self._splitter.addWidget(right)

    # ---- Model lifecycle ----

    def _bound_decoders(self) -> dict[str, Decoder | None]:
        """Decoder per bus with a DBC bound, open or not: a closed bus's rows
        keep their names and stay expandable."""
        if self._ctx is None:
            return {}
        db = self._ctx.database
        return {b: db.decoder_for_bus(b) for p in db.loaded_paths() for b in db.buses_for_path(p)}

    def _on_bus_topology_changed(self, _bus_name: str) -> None:
        if self._model is None or self._ctx is None:
            return
        # A stopped bus's ring is final by now; take its tail before letting go.
        if not self._paused_btn.isChecked():
            self._model.drain_now()
        self._model.set_rings(self._ctx.bus_reader.rings)
        self._tree.viewport().update()

    def _on_session_t0_changed(self, t0: object) -> None:
        if t0 is not None or self._model is None:
            return
        self._model.clear()
        self._pinned_key = None
        self._last_pushed = None

    def _on_database_changed(self) -> None:
        if self._model is None:
            return
        self._model.set_decoders(self._bound_decoders())
        # Effective set may shift if a tracked DBC's message-IDs changed.
        self._on_effective_changed()

    def _on_bus_error(self, message: str) -> None:
        # Comes before a bus_failed, which takes the banner back.
        self._failed_bus_name = None
        self._health_bus = None
        self._banner.show_message(message, kind="error")

    def _on_bus_failed(self, bus_name: str, message: str) -> None:
        self._failed_bus_name = bus_name
        self._health_bus = None
        self._banner.show_message(
            f"Bus {bus_name} disconnected: {message}",
            kind="error",
            action_label="Reconnect",
        )

    def _on_bus_started_clear_banner(self, bus_name: str) -> None:
        if self._failed_bus_name == bus_name:
            self._failed_bus_name = None
            self._banner.hide()

    def _on_bus_stopped_clear_health_banner(self, bus_name: str) -> None:
        self._health_seen.pop(bus_name, None)
        if self._health_bus == bus_name:
            self._show_worst_health()

    def _on_bus_health_changed(self, bus_name: str, state: str, detail: str) -> None:
        health = BusHealth(state)
        previous = self._health_seen.get(bus_name, (BusHealth.OK, ""))[0]
        self._health_seen[bus_name] = (health, detail)
        # A failure banner carries a Reconnect for a dead bus; keep it.
        if self._failed_bus_name is not None:
            return
        # LOST: bus_failed follows with its own banner.
        if bus_name == self._health_bus and health is not BusHealth.LOST:
            self._show_worst_health()
            return
        # A new detail alone never brings a dismissed banner back, and a
        # bus-off banner, with its Reconnect, never gives way to bus errors.
        if health is previous or health not in _BANNER_HEALTH:
            return
        if self._health_bus is not None:
            shown = self._health_seen[self._health_bus][0]
            if HEALTH_RANK[shown] > HEALTH_RANK[health]:
                return
        self._show_health(bus_name)

    def _show_worst_health(self) -> None:
        """Show the worst bus-errors / bus-off state left, the shown bus winning a tie."""
        degraded = [name for name, (h, _d) in self._health_seen.items() if h in _BANNER_HEALTH]
        if not degraded:
            self._health_bus = None
            self._banner.hide()
            return
        self._show_health(
            max(
                degraded,
                key=lambda n: (HEALTH_RANK[self._health_seen[n][0]], n == self._health_bus),
            )
        )

    def _show_health(self, bus_name: str) -> None:
        health, detail = self._health_seen[bus_name]
        self._health_bus = bus_name
        if health is BusHealth.BUS_OFF:
            self._banner.show_message(
                f"Bus {bus_name}: bus-off. The controller stopped transmitting; "
                "reconnect to recover.",
                kind="error",
                action_label="Reconnect",
            )
        else:
            self._banner.show_message(
                f"Bus {bus_name}: {health.describe(detail)}. Error frames are sampled. "
                "Check the cable, termination and that another node is powered.",
                kind="warn",
            )

    def _on_banner_dismissed(self) -> None:
        self._failed_bus_name = None
        self._health_bus = None

    def _on_banner_action(self) -> None:
        if self._ctx is None:
            return
        bus_name = self._failed_bus_name or self._health_bus
        if bus_name is None:
            return
        reader = self._ctx.bus_reader
        if self._failed_bus_name is not None and bus_name in reader.bus_names():
            # Already opening, from an earlier click or the toolbar's Connect.
            return
        cfg = next(
            (c for c in self._ctx.project.workspace.bus if c.name == bus_name),
            reader.last_config_for(bus_name),
        )
        if cfg is None:
            return
        # Reopening also resets a bus-off controller, and keeps the session.
        reader.reopen(cfg)

    def _on_effective_changed(self) -> None:
        if self._model is None or self._tracked_pane is None:
            return
        self._model.set_admit(self._tracked_pane.effective().admits)

    def _on_superseded(self, message: str) -> None:
        self._failed_bus_name = None
        self._health_bus = None
        self._banner.show_message(message, kind="info")

    # ---- Slots ----

    def _on_mode_selected(self, mode: str) -> None:
        if self._model is None:
            return
        self._model.set_mode("overwrite" if mode == "overwrite" else "append")
        # Pin semantics are mode-specific (Latest follows, Stream
        # freezes); a pin set under the old mode would behave wrongly
        # after the switch, so drop it. The user has to click again
        # under the new mode to engage the new behaviour.
        self._pinned_key = None
        self._last_pushed = None

    def _on_pause_selected(self, paused: bool) -> None:
        if self._model is None:
            return
        self._model.set_paused(paused)

    def _on_counters_changed(self, rx: int, err: int, lost: int) -> None:
        text = f"RX {rx}   ERR {err}"
        self._counts_label.setText(f"{text}   LOST {lost}" if lost else text)

    def _on_filter_text_changed(self, text: str) -> None:
        # Clear affordance tracks the text immediately; the filter update
        # stays debounced.
        self._clear_action.setVisible(bool(text))
        self._filter_debounce.start()

    def _apply_filter(self) -> None:
        if self._model is None:
            return
        self._model.set_text_filter(
            self._filter_input.text().strip(), self._case_action.isChecked()
        )

    def _on_case_toggled(self, _checked: bool) -> None:
        self._refresh_case_icon()
        self._apply_filter()

    def _refresh_case_icon(self) -> None:
        name = "match_case_on" if self._case_action.isChecked() else "match_case"
        self._case_action.setIcon(icon(name))

    def _on_toggle_left_pane(self) -> None:
        if self._tracked_pane is None:
            return
        if self._tracked_pane.isVisible():
            sizes = self._splitter.sizes()
            if sizes and sizes[0] > 0:
                self._last_left_width = sizes[0]
        self._tracked_pane.setVisible(not self._tracked_pane.isVisible())
        self._refresh_toggle_icon()

    def _on_toggle_detail_pane(self) -> None:
        if self._detail_view is None:
            return
        if self._detail_view.isVisible():
            sizes = self._detail_splitter.sizes()
            if len(sizes) >= 2 and sizes[1] > 0:
                self._last_detail_height = sizes[1]
        self._detail_view.setVisible(not self._detail_view.isVisible())
        if self._detail_view.isVisible():
            # Restore the previous split — Qt would otherwise leave the
            # detail pane at its last "hidden" zero height even after
            # setVisible(True).
            total = sum(self._detail_splitter.sizes())
            top = max(total - self._last_detail_height, 0)
            self._detail_splitter.setSizes([top, self._last_detail_height])
        self._refresh_toggle_icon()

    def _refresh_toggle_icon(self) -> None:
        # Before ``bind`` builds the tracked pane the toggle reflects the
        # default-on state so the initial chrome matches the eventual
        # visible pane. ``isHidden`` captures intended visibility even
        # before the parent window is shown.
        left_visible = self._tracked_pane is None or not self._tracked_pane.isHidden()
        self._toggle_left_btn.setIcon(icon("pane_left_on" if left_visible else "pane_left_off"))
        detail_visible = self._detail_view is None or not self._detail_view.isHidden()
        self._toggle_detail_btn.setIcon(
            icon("pane_bottom_on" if detail_visible else "pane_bottom_off")
        )

    def _on_expanded(self, parent_idx: QModelIndex) -> None:
        for r in range(parent_idx.model().rowCount(parent_idx)):
            self._tree.setFirstColumnSpanned(r, parent_idx, True)

    def _on_rows_about_to_be_removed(self, parent: QModelIndex, first: int, last: int) -> None:
        # A selected Stream row leaving the live window: deselect it rather
        # than let the view move the selection on every drain. The inspector
        # keeps the clicked frame.
        sel = self._tree.selectionModel()
        current = sel.currentIndex()
        if (
            not parent.isValid()
            and current.isValid()
            and not current.parent().isValid()
            and first <= current.row() <= last
        ):
            sel.clear()

    def _on_row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._ctx is None or self._model is None:
            return
        frame = self._model.frame_at(current)
        if frame is None:
            return

        # Mode-specific behaviour for the inspector:
        #
        # - Latest (overwrite): the row IS a live view, with its
        #   underlying FrameRecord replaced in place on every new
        #   arrival. Pin to (bus, id) so the inspector tracks those
        #   in-place updates via `_refresh_pinned_inspector`.
        # - Stream (append): rows are individual historical frames.
        #   The click means "show me THIS frame", so we render the
        #   clicked record statically and clear any prior pin so the
        #   drain-driven refresh leaves the inspector alone.
        if self._model.mode() == "overwrite":
            self._pinned_key = (frame.bus_name, frame.arbitration_id)
            self._last_pushed = None
            self._refresh_pinned_inspector()
        else:
            self._pinned_key = None
            self._last_pushed = None
            if self._detail_view is not None:
                period = self._model.period_for(frame.bus_name, frame.arbitration_id)
                self._detail_view.set_frame(frame, observed_period_s=period)

        # Broadcast the user's click on the events bus so any future
        # cross-window listener sees the selection event. Drain-driven
        # pin refreshes do NOT re-broadcast — `frame_selected` means
        # "user selected this frame", not "the pinned latest changed".
        self._ctx.events.frame_selected.emit(frame)

    def _refresh_pinned_inspector(self) -> None:
        """Push the latest frame for ``_pinned_key`` into the inspector.

        Connected to ``TraceTreeModel.counters_changed`` so it runs
        once per drain that admitted at least one frame. Cheap — a
        single dict lookup plus an identity check; only actually
        re-renders the inspector when a new frame for the pinned key
        has arrived.
        """
        if self._pinned_key is None or self._model is None or self._detail_view is None:
            return
        bus, aid = self._pinned_key
        latest = self._model.latest_frame_for(bus, aid)
        if latest is None or latest is self._last_pushed:
            return
        self._last_pushed = latest
        period = self._model.period_for(bus, aid)
        self._detail_view.set_frame(latest, observed_period_s=period)

    # ---- Persistence ----

    def save_state(self) -> dict[str, object]:
        state = super().save_state()
        state["filter"] = self._filter_input.text()
        state["case_sensitive"] = self._case_action.isChecked()
        state["paused"] = self._paused_btn.isChecked()
        state["mode"] = "overwrite" if self._latest_btn.isChecked() else "append"
        if self._tracked_pane is not None:
            state["tracked"] = self._tracked_pane.serialize()
        sizes = self._splitter.sizes()
        if (
            self._tracked_pane is not None
            and self._tracked_pane.isVisible()
            and sizes
            and sizes[0] > 0
        ):
            self._last_left_width = sizes[0]
        state["splitter_sizes"] = list(sizes)
        if self._tracked_pane is not None:
            state["left_pane_visible"] = self._tracked_pane.isVisible()
        if self._detail_view is not None:
            state["detail_pane_visible"] = self._detail_view.isVisible()
            detail_sizes = self._detail_splitter.sizes()
            if self._detail_view.isVisible() and len(detail_sizes) >= 2 and detail_sizes[1] > 0:
                self._last_detail_height = detail_sizes[1]
            state["detail_splitter_sizes"] = list(detail_sizes)
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        super().restore_state(state)
        if self._tracked_pane is None:
            # bind() hasn't run yet — stash for later application.
            self._pending_state = dict(state)
            return
        self._apply_state(state)

    def _apply_pending_state(self) -> None:
        state = self._pending_state
        self._pending_state = None
        if state is not None:
            self._apply_state(state)

    def _apply_state(self, state: dict[str, object]) -> None:
        sizes = state.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                int_sizes = [int(s) for s in sizes]
            except (TypeError, ValueError):
                int_sizes = None
            if int_sizes is not None:
                self._splitter.setSizes(int_sizes)
                if int_sizes[0] > 0:
                    self._last_left_width = int_sizes[0]
        detail_sizes = state.get("detail_splitter_sizes")
        if isinstance(detail_sizes, list) and len(detail_sizes) == 2:
            try:
                int_detail = [int(s) for s in detail_sizes]
            except (TypeError, ValueError):
                int_detail = None
            if int_detail is not None:
                self._detail_splitter.setSizes(int_detail)
                if int_detail[1] > 0:
                    self._last_detail_height = int_detail[1]
        detail_visible = state.get("detail_pane_visible")
        if (
            isinstance(detail_visible, bool)
            and self._detail_view is not None
            and detail_visible != self._detail_view.isVisible()
        ):
            self._on_toggle_detail_pane()
        visible = state.get("left_pane_visible")
        if (
            isinstance(visible, bool)
            and self._tracked_pane is not None
            and visible != self._tracked_pane.isVisible()
        ):
            self._on_toggle_left_pane()

        # Legacy migration: old ``message_trace`` had ``tracked: [{id, name}, ...]``
        # with no bus binding; old ``bus_trace`` had ``tracked_buses: [name, ...]``.
        # New shape is a list of dicts with discriminator ``kind``.
        tracked = state.get("tracked")
        if isinstance(tracked, list) and self._tracked_pane is not None:
            if all(isinstance(t, dict) and "kind" in t for t in tracked):
                # New shape.
                self._tracked_pane.deserialize(tracked)
            else:
                # Legacy message_trace shape: tracked = [{"id": int, "name": str}, ...]
                channel = self._params.get("channel")
                bus_name = channel if isinstance(channel, str) and channel else ""
                migrated: list[dict[str, object]] = []
                for entry in tracked:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        mid = int(entry["id"])
                        name = str(entry["name"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    decoder = (
                        self._ctx.database.decoder_for_message(name)
                        if self._ctx is not None
                        else None
                    )
                    path_str = ""
                    resolved_bus = bus_name
                    if decoder is not None and self._ctx is not None:
                        for p in self._ctx.database.loaded_paths():
                            if self._ctx.database.decoder_for_path(p) is decoder:
                                path_str = str(p)
                                buses = self._ctx.database.buses_for_path(p)
                                if not resolved_bus and len(buses) == 1:
                                    resolved_bus = buses[0]
                                break
                    if not resolved_bus:
                        continue
                    migrated.append(
                        {
                            "kind": "msg",
                            "bus": resolved_bus,
                            "path": path_str,
                            "id": mid,
                            "name": name,
                        }
                    )
                self._tracked_pane.deserialize(migrated)

        legacy_buses = state.get("tracked_buses")
        if isinstance(legacy_buses, list) and self._tracked_pane is not None:
            migrated_buses: list[dict[str, object]] = []
            for name in legacy_buses:
                if isinstance(name, str) and name:
                    migrated_buses.append({"kind": "bus", "bus": name})
            if migrated_buses:
                self._tracked_pane.deserialize(migrated_buses)

        flt = state.get("filter")
        if isinstance(flt, str):
            self._filter_input.setText(flt)
        case_sensitive = state.get("case_sensitive")
        if isinstance(case_sensitive, bool):
            self._case_action.setChecked(case_sensitive)
        paused = state.get("paused")
        if isinstance(paused, bool):
            if paused:
                self._paused_btn.setChecked(True)
            else:
                self._running_btn.setChecked(True)
            self._on_pause_selected(paused)
        mode = state.get("mode")
        if mode == "overwrite":
            self._latest_btn.setChecked(True)
            self._on_mode_selected("overwrite")
        elif mode == "append":
            self._stream_btn.setChecked(True)
            self._on_mode_selected("append")
