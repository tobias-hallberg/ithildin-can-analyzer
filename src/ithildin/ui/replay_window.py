# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Replay child window — wraps ``core.replay.ReplayController``.

Single-instance. Picks an input log file, picks a speed multiplier, and
plays the log onto whatever bus the application currently owns by
routing through ``BusReader.send_message``. Pause and scrub support are
deferred to Stage 1.E (the underlying engine doesn't seek today).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.replay import ReplayController
from ithildin.ui._child_base import ChildView
from ithildin.ui.icons import icon, themed_callback
from ithildin.ui.view_header import ViewHeaderBar

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext


_FILE_FILTER = "Log files (*.mf4 *.blf *.asc *.log *.csv *.trc);;All files (*)"
_SPEED_OPTIONS: tuple[tuple[str, float], ...] = (
    ("0.5x", 0.5),
    ("1x", 1.0),
    ("2x", 2.0),
    ("5x", 5.0),
    ("10x", 10.0),
    ("100x", 100.0),
)


class ReplayWindow(ChildView):
    KIND = "replay"
    DEFAULT_SIZE = (540, 200)
    TITLE = "Replay"

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, params=params)
        self._ctx: AppContext | None = None
        self._controller: ReplayController | None = None
        self._build_body()

    def bind(self, ctx: AppContext) -> None:
        self._ctx = ctx
        # Child views get no closeEvent when the main window closes; stop
        # here so the replay thread is joined before exit.
        app = QCoreApplication.instance()
        assert app is not None
        app.aboutToQuit.connect(self._stop)

    # ---- Build ----

    def _build_body(self) -> None:
        body = self
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = ViewHeaderBar(body, hide_when_idle=True)
        outer.addWidget(self.header)

        content = QWidget(body)
        outer.addWidget(content, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # File row.
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("File", body))
        self._path_edit = QLineEdit(body)
        self._path_edit.setPlaceholderText("Pick a log file to replay…")
        file_row.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…", body)
        browse_btn.clicked.connect(self._on_browse_clicked)
        file_row.addWidget(browse_btn)
        layout.addLayout(file_row)

        # Speed row.
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed", body))
        self._speed_combo = QComboBox(body)
        for label, factor in _SPEED_OPTIONS:
            self._speed_combo.addItem(label, factor)
        self._speed_combo.setCurrentIndex(1)  # 1x
        speed_row.addWidget(self._speed_combo)
        speed_row.addStretch(1)
        self._status_lbl = QLabel("Idle", body)
        speed_row.addWidget(self._status_lbl)
        layout.addLayout(speed_row)

        layout.addStretch(1)

        # v0.1 limit disclosure — replay engine has no seek/scrub yet.
        hint = QLabel("Pause and scrub are not available in v0.1.", body)
        hint.setStyleSheet("color: palette(mid); font-size: 9pt;")
        layout.addWidget(hint)

        # Footer: Play / Stop.
        footer = QHBoxLayout()
        footer.addStretch(1)
        self._action_btn = QPushButton("Play", body)
        self._action_btn.setDefault(True)
        self._action_btn.clicked.connect(self._on_action_clicked)
        footer.addWidget(self._action_btn)
        layout.addLayout(footer)
        themed_callback(self._refresh_action_icon)

    # ---- Persistence ----

    def save_state(self) -> dict[str, object]:
        state = super().save_state()
        state["path"] = self._path_edit.text()
        state["speed_idx"] = self._speed_combo.currentIndex()
        return state

    def restore_state(self, state: dict[str, object]) -> None:
        super().restore_state(state)
        path = state.get("path")
        if isinstance(path, str):
            self._path_edit.setText(path)
        idx = state.get("speed_idx")
        if isinstance(idx, int) and 0 <= idx < self._speed_combo.count():
            self._speed_combo.setCurrentIndex(idx)

    # ---- Slots ----

    def _on_browse_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open log", "", _FILE_FILTER)
        if path:
            self._path_edit.setText(path)

    def _on_action_clicked(self) -> None:
        if self._controller is not None:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self._ctx is None:
            return
        bus_names = self._ctx.bus_reader.bus_names()
        if not bus_names:
            self._status_lbl.setText("No bus open — connect first")
            return
        path_str = self._path_edit.text().strip()
        if not path_str:
            self._status_lbl.setText("Pick a log file first")
            return
        speed = float(self._speed_combo.currentData() or 1.0)
        # Pin a target so the whole replay sends to the same bus even
        # if a second bus is opened mid-playback. v0.1 picks the first
        # open bus; a target picker is a follow-up when multi-bus
        # replay flows show up in real use.
        target = bus_names[0]
        bus_reader = self._ctx.bus_reader
        if bus_reader.is_listen_only(target):
            self._status_lbl.setText(f"Bus {target!r} is listen-only — replay disabled")
            return
        send = lambda msg: bus_reader.send_message(msg, target)  # noqa: E731
        try:
            self._controller = ReplayController(Path(path_str), send, speed=speed)
        except Exception as exc:  # backend / file errors surface here
            self._status_lbl.setText(f"Error: {exc}")
            return
        self._controller.started.connect(self._on_started)
        self._controller.progress.connect(self._on_progress)
        self._controller.error.connect(self._on_error)
        self._controller.finished.connect(self._on_finished)
        self._controller.start()

    def _stop(self) -> None:
        if self._controller is None:
            return
        self._controller.stop(wait_ms=2000)

    def _on_started(self) -> None:
        self._status_lbl.setText("Playing…")
        self._action_btn.setText("Stop")
        self._refresh_action_icon()

    def _on_progress(self, sent: int) -> None:
        self._status_lbl.setText(f"Playing · {sent} frames sent")

    def _on_finished(self, sent: int) -> None:
        self._controller = None
        self._status_lbl.setText(f"Done · {sent} frames sent")
        self._action_btn.setText("Play")
        self._refresh_action_icon()

    def _refresh_action_icon(self) -> None:
        # Play when idle, Stop while a replay is running (re-tinted on theme
        # switch); playing iff a controller is live.
        playing = self._controller is not None
        self._action_btn.setIcon(icon("stop" if playing else "play"))

    def _on_error(self, message: str) -> None:
        self._status_lbl.setText(f"Error: {message}")

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._controller is not None:
            self._stop()
        super().closeEvent(event)
