# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Record child window — Trace-shaped shell wrapping ``core.logger.TeeLogger``.

Shares Trace's structural chrome: a horizontal splitter with the
``TrackedTreePane`` subscription tree on the left and a right pane
carrying the top control strip + the recording-specific main area.
The subscription tree filters what gets written — an empty tree
records everything; any explicit subscription narrows the recorder to
that subset, mutable live while a recording is in flight.

The main area is intentionally minimal for v0.1 (file picker, status,
start/stop). The eventual richer recording UI replaces this region;
the surrounding shell stays.

A recording keeps going through a bus loss or a Disconnect and follows
the buses as they reopen, so one file spans the outage; stopping it on
its own would let a restart to the same path overwrite what was captured.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.logger import TeeLogger
from ithildin.ui._child_base import ChildView
from ithildin.ui._tracked_tree import TrackedTreePane
from ithildin.ui.banner import Banner
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import icon, themed_callback
from ithildin.ui.view_header import ViewHeaderBar

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.core.types import FrameRecord


_FILE_FILTER = (
    "ASAM MF4 (*.mf4);;Vector BLF (*.blf);;ASCII (*.asc);;CSV (*.csv);;"
    "All log formats (*.mf4 *.blf *.asc *.csv *.log *.trc)"
)

_DEFAULT_LEFT_PANE_WIDTH = 220
_STAT_INTERVAL_S = 1.0


class RecordWindow(ChildView):
    KIND = "record"
    DEFAULT_SIZE = (920, 420)
    TITLE = "Record"

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, params=params)
        self._ctx: AppContext | None = None
        self._logger: TeeLogger | None = None
        self._tracked_pane: TrackedTreePane | None = None
        self._last_left_width = _DEFAULT_LEFT_PANE_WIDTH
        # Saved state may arrive before ``bind`` builds the tracked pane;
        # stash it and replay once the pane exists.
        self._pending_state: dict[str, object] | None = None
        self._written = 0
        self._dropped = 0
        self._lost = 0
        self._out_path: Path | None = None
        # The file size shown while recording, stat'ed at most once a second.
        self._size_kb: float | None = None
        self._stat_at = float("-inf")
        # Bus the loss banner names, so its reconnect can take it down.
        self._lost_bus: str | None = None
        self._build_body()

    def _format_title(self, params: dict[str, object]) -> str:
        return "Record"

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._tracked_pane = TrackedTreePane(ctx.database, self._splitter)
        self._tracked_pane.model.superseded.connect(self._on_superseded)
        self._splitter.insertWidget(0, self._tracked_pane)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([_DEFAULT_LEFT_PANE_WIDTH, 700])

        ctx.bus_reader.error.connect(self._on_bus_error)
        ctx.bus_reader.bus_started.connect(self._on_bus_started)
        ctx.bus_reader.bus_stopped.connect(self._on_bus_topology_changed)
        ctx.bus_reader.bus_failed.connect(self._on_bus_failed)
        # Child views get no closeEvent when the main window closes; stop
        # here so the file is finalised and the thread joined before exit.
        app = QCoreApplication.instance()
        assert app is not None
        app.aboutToQuit.connect(self._stop)

        if self._pending_state is not None:
            self._apply_pending_state()

    def add_payload(self, payload: object) -> None:
        """Narrow the recording to a dropped bus, DBC, message or signal."""
        if self._tracked_pane is not None:
            self._tracked_pane.add_payload(payload)

    # ---- Build ----

    def _build_body(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self._banner = Banner(kind="error", parent=self)
        layout.addWidget(self._banner)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        layout.addWidget(self._splitter, 1)

        right = QWidget(self._splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        self.header = ViewHeaderBar(right)
        strip = self.header

        self._state_segment = QFrame(right)
        self._state_segment.setObjectName("ModeSegment")
        self._state_segment.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        seg_layout = QHBoxLayout(self._state_segment)
        seg_layout.setContentsMargins(0, 0, 0, 0)
        seg_layout.setSpacing(0)

        self._idle_btn = QToolButton(self._state_segment)
        self._idle_btn.setText("Idle")
        self._idle_btn.setCheckable(True)
        self._idle_btn.setChecked(True)
        self._idle_btn.setAutoExclusive(False)
        self._idle_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._idle_btn.setToolTip("Stop writing frames to disk.")

        self._recording_btn = QToolButton(self._state_segment)
        self._recording_btn.setObjectName("SegmentRightBtn")
        self._recording_btn.setText("Recording")
        self._recording_btn.setCheckable(True)
        self._recording_btn.setAutoExclusive(False)
        self._recording_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._recording_btn.setToolTip("Begin writing matching frames to the output file.")

        self._state_group = QButtonGroup(self)
        self._state_group.setExclusive(True)
        self._state_group.addButton(self._idle_btn)
        self._state_group.addButton(self._recording_btn)
        self._idle_btn.clicked.connect(lambda: self._on_state_selected(False))
        self._recording_btn.clicked.connect(lambda: self._on_state_selected(True))

        seg_layout.addWidget(self._idle_btn)
        seg_layout.addWidget(self._recording_btn)
        strip.add_widget(self._state_segment)

        self._counts_label = QLabel("WRITTEN 0   DROPPED 0", right)
        self._counts_label.setFont(tabular_font())
        self._counts_label.setStyleSheet("color: palette(mid);")
        strip.add_widget(self._counts_label)

        strip.add_stretch(1)

        self._toggle_left_btn = strip.add_pane_toggle(
            "Show/hide tracked pane", self._on_toggle_left_pane
        )

        right_layout.addWidget(strip)

        # Main area — basic placeholder for v0.1; the richer recording UI
        # lands here in a follow-up. File row + status line + Start/Stop.
        main = QVBoxLayout()
        main.setContentsMargins(0, 8, 0, 0)
        main.setSpacing(10)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("Output", right))
        self._path_edit = QLineEdit(right)
        self._path_edit.setPlaceholderText("Pick a destination file…")
        file_row.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…", right)
        browse_btn.clicked.connect(self._on_browse_clicked)
        file_row.addWidget(browse_btn)
        main.addLayout(file_row)

        self._status_label = QLabel("Idle", right)
        main.addWidget(self._status_label)

        main.addStretch(1)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self._action_btn = QPushButton("Start recording", right)
        self._action_btn.setDefault(True)
        self._action_btn.clicked.connect(self._on_action_clicked)
        action_row.addWidget(self._action_btn)
        main.addLayout(action_row)

        right_layout.addLayout(main, 1)

        themed_callback(self._refresh_toggle_icon)
        themed_callback(self._refresh_action_icon)

        self._splitter.addWidget(right)

    # ---- Admit predicate ----

    def _admit(self, frame: FrameRecord) -> bool:
        """Return True if ``frame`` should be written.

        Re-reads the tracked pane's effective snapshot on every call so
        edits to the subscription tree take effect mid-recording. Empty
        tree (no explicit subscriptions of any kind) means record
        everything — ``_Effective.admits`` defaults to deny, which is
        right for the Trace view but wrong for a recorder with no
        explicit filter.
        """
        if self._tracked_pane is None:
            return True
        eff = self._tracked_pane.effective()
        if not eff.bus_all and not eff.bus_dbc_ids and not eff.bus_msg_ids:
            return True
        return eff.admits(frame.bus_name, frame.arbitration_id, is_error=frame.is_error_frame)

    # ---- Persistence ----

    def save_state(self) -> dict[str, object]:
        state = super().save_state()
        state["path"] = self._path_edit.text()
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
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        super().restore_state(state)
        path = state.get("path")
        if isinstance(path, str):
            self._path_edit.setText(path)
        if self._tracked_pane is None:
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
        visible = state.get("left_pane_visible")
        if (
            isinstance(visible, bool)
            and self._tracked_pane is not None
            and visible != self._tracked_pane.isVisible()
        ):
            self._on_toggle_left_pane()
        tracked = state.get("tracked")
        if (
            isinstance(tracked, list)
            and self._tracked_pane is not None
            and all(isinstance(t, dict) and "kind" in t for t in tracked)
        ):
            self._tracked_pane.deserialize(tracked)

    # ---- Slots ----

    def _on_browse_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Record to file", "capture.mf4", _FILE_FILTER)
        if path:
            self._path_edit.setText(path)

    def _on_action_clicked(self) -> None:
        if self._logger is not None:
            self._stop()
        else:
            self._start()

    def _on_state_selected(self, target_recording: bool) -> None:
        if target_recording:
            if self._logger is None:
                self._start()
        else:
            if self._logger is not None:
                self._stop()

    def _start(self) -> None:
        if self._ctx is None:
            self._snap_to_idle()
            return
        if not self._ctx.bus_reader.is_open:
            self._snap_to_idle("No bus open — connect first")
            return
        path_str = self._path_edit.text().strip()
        if not path_str:
            self._snap_to_idle("Pick an output file first")
            return
        rings = self._ctx.bus_reader.rings
        if not rings:
            self._snap_to_idle("No bus open — connect first")
            return
        self._banner.hide()
        self._written = 0
        self._dropped = 0
        self._lost = 0
        self._out_path = Path(path_str)
        self._stat_at = float("-inf")
        self._refresh_counts_label()
        self._logger = TeeLogger(self._out_path, rings, admit=self._admit)
        self._logger.started.connect(self._on_started)
        self._logger.frame_count.connect(self._on_frame_count)
        self._logger.dropped_count.connect(self._on_dropped_count)
        self._logger.lost_count.connect(self._on_lost_count)
        self._logger.error.connect(self._on_error)
        self._logger.stopped.connect(self._on_stopped)
        self._logger.start()

    def _stop(self) -> None:
        if self._logger is None:
            return
        self._logger.stop(wait_ms=5000)

    def _snap_to_idle(self, message: str | None = None) -> None:
        self._idle_btn.setChecked(True)
        self._action_btn.setText("Start recording")
        self._refresh_action_icon()
        if message is not None:
            self._banner.show_message(message, kind="error")
            self._status_label.setText(message)

    def _on_started(self) -> None:
        self._recording_btn.setChecked(True)
        self._action_btn.setText("Stop")
        self._refresh_action_icon()
        self._status_label.setText("Recording…")

    def _on_frame_count(self, count: int) -> None:
        self._written = count
        self._refresh_counts_label()
        self._refresh_status_running()

    def _on_dropped_count(self, count: int) -> None:
        self._dropped = count
        self._refresh_counts_label()

    def _on_lost_count(self, count: int) -> None:
        self._lost = count
        self._refresh_counts_label()

    def _refresh_counts_label(self) -> None:
        text = f"WRITTEN {self._written}   DROPPED {self._dropped}"
        if self._lost:
            text += f"   LOST {self._lost}"
            self._counts_label.setToolTip(
                "LOST: frames overwritten in the capture buffer before the recorder wrote them"
            )
        else:
            self._counts_label.setToolTip("")
        self._counts_label.setText(text)

    def _refresh_status_running(self) -> None:
        if self._ctx is not None and not self._ctx.bus_reader.is_open:
            self._status_label.setText("Recording · no bus open — waiting for a bus")
            return
        now = time.monotonic()
        if self._out_path is not None and now - self._stat_at >= _STAT_INTERVAL_S:
            self._stat_at = now
            try:
                self._size_kb = self._out_path.stat().st_size / 1024.0
            except OSError:
                self._size_kb = None
        if self._size_kb is None:
            self._status_label.setText(f"Recording · {self._written} frames written")
        else:
            self._status_label.setText(
                f"Recording · {self._written} frames written · {self._size_kb:.1f} kB on disk"
            )

    def _on_error(self, message: str) -> None:
        self._banner.show_message(f"Recorder error: {message}", kind="error")
        self._status_label.setText(f"Error: {message}")

    def _on_stopped(self) -> None:
        self._idle_btn.setChecked(True)
        self._action_btn.setText("Start recording")
        if self._banner.kind == "warn":
            self._banner.hide()
            self._lost_bus = None
        if self._logger is not None and not self._banner.isVisible():
            self._status_label.setText(
                f"Stopped · {self._written} frames written" if self._written else "Stopped"
            )
        if self._lost and self._ctx is not None:
            self._ctx.events.report_requested.emit(
                f"Recording to {self._out_path} finished with {self._lost} frames lost", "warn"
            )
        self._logger = None

    def _on_bus_started(self, bus_name: str) -> None:
        if bus_name == self._lost_bus and self._banner.kind == "warn":
            self._banner.hide()
        self._on_bus_topology_changed(bus_name)

    def _on_bus_topology_changed(self, _bus_name: str) -> None:
        if self._logger is None or self._ctx is None:
            return
        self._logger.set_rings(self._ctx.bus_reader.rings)
        self._refresh_status_running()

    def _on_bus_failed(self, bus_name: str, _message: str) -> None:
        if self._logger is None:
            return
        self._lost_bus = bus_name
        self._banner.show_message(
            f"Bus {bus_name} lost — recording continues and resumes when it reconnects",
            kind="warn",
        )

    def _on_bus_error(self, message: str) -> None:
        self._banner.show_message(message, kind="error")

    def _on_superseded(self, message: str) -> None:
        self._banner.show_message(message, kind="info")

    def _on_toggle_left_pane(self) -> None:
        if self._tracked_pane is None:
            return
        if self._tracked_pane.isVisible():
            sizes = self._splitter.sizes()
            if sizes and sizes[0] > 0:
                self._last_left_width = sizes[0]
        self._tracked_pane.setVisible(not self._tracked_pane.isVisible())
        self._refresh_toggle_icon()

    def _refresh_toggle_icon(self) -> None:
        # Before ``bind`` builds the tracked pane the toggle reflects the
        # default-on state so the initial chrome matches the eventual
        # visible pane.
        left_visible = self._tracked_pane is None or not self._tracked_pane.isHidden()
        self._toggle_left_btn.setIcon(icon("pane_left_on" if left_visible else "pane_left_off"))

    def _refresh_action_icon(self) -> None:
        # Record when idle, Stop while recording — re-tinted on theme switch.
        recording = self._recording_btn.isChecked()
        self._action_btn.setIcon(icon("stop" if recording else "record"))

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._logger is not None:
            self._stop()
        super().closeEvent(event)
