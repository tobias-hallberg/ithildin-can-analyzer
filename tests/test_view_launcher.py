# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.view_launcher.ViewLauncherButton``."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QMdiArea, QVBoxLayout, QWidget

from ithildin.ui._drag_payload import (
    SignalPayload,
    pack_into_mime,
)
from ithildin.ui.signal_plot import SignalPlotWindow
from ithildin.ui.view_launcher import ViewLauncherButton
from ithildin.ui.window_manager import WindowManager
from tests.conftest import make_app_context


class _FakeView(QWidget):
    """Minimal kind-aware view (mirrors ChildView's surface for the registry)."""

    KIND = "message_trace"

    header = None

    def __init__(self, instance_id: str, params: dict[str, object] | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"Message trace · {instance_id}")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("message_trace"))

    def set_default_title(self, title: str) -> None:
        self.setWindowTitle(title)

    def save_state(self) -> dict[str, object]:
        return {}

    def restore_state(self, _state: dict[str, object]) -> None:
        return None


def _factory(_mgr, kind: str, instance_id: str, _params):  # type: ignore[no-untyped-def]
    return _FakeView(instance_id=instance_id)


def _build(qtbot, tmp_path: Path):  # type: ignore[no-untyped-def]
    area = QMdiArea()
    qtbot.addWidget(area)
    mgr = WindowManager(area, tmp_path / "layout.json", _factory)
    return mgr, area


def test_menu_starts_empty_with_new_entry(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    btn = ViewLauncherButton("message_trace", "Message trace", mgr)
    qtbot.addWidget(btn)

    actions = btn.menu().actions()
    # Empty state: a single italic disabled "No open …" row (the button
    # itself opens a new instance on click).
    assert len(actions) == 1
    assert "no open" in actions[0].text().lower()
    assert not actions[0].isEnabled()


def test_icon_name_kwarg_sets_button_icon(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtCore import Qt

    mgr, _area = _build(qtbot, tmp_path)
    btn = ViewLauncherButton("message_trace", "Message trace", mgr, icon_name="message_trace")
    qtbot.addWidget(btn)
    assert not btn.icon().isNull()
    # Style must be TextUnderIcon — toolbar widgets don't inherit the
    # toolbar's toolButtonStyle, so the launcher must set it itself.
    assert btn.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    # Text label is preserved.
    assert btn.text() == "Message trace"
    # Without icon_name, icon stays null but style + text still apply.
    plain = ViewLauncherButton("message_trace", "Message trace", mgr)
    qtbot.addWidget(plain)
    assert plain.icon().isNull()
    assert plain.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon


def test_menu_lists_open_instances(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    btn = ViewLauncherButton("message_trace", "Message trace", mgr)
    qtbot.addWidget(btn)

    mgr.open("message_trace", params={"channel": "CAN_1"})
    mgr.open("message_trace", params={"channel": "CAN_2"})
    real = [a for a in btn.menu().actions() if not a.isSeparator()]
    # One entry per open instance (the button itself opens a new one on click).
    assert len(real) == 2
    assert btn.text().endswith("(2)")


def test_drop_signal_spawns_seeded_plot(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Drop a signal payload on the Plot launcher → spawns a Plot with that curve."""

    ctx = make_app_context(tmp_path)

    def real_factory(_mgr, kind: str, instance_id: str, params):  # type: ignore[no-untyped-def]
        # Use the real SignalPlotWindow so add_signal exists.
        if kind == "signal_plot":
            view = SignalPlotWindow(instance_id=instance_id, params=params)
            view.bind(ctx)
            return view
        return _FakeView(instance_id=instance_id)

    area = QMdiArea()
    qtbot.addWidget(area)
    mgr = WindowManager(area, tmp_path / "layout.json", real_factory)
    btn = ViewLauncherButton("signal_plot", "Plot", mgr)
    qtbot.addWidget(btn)

    payload = SignalPayload(
        message="EngineCommand", message_id=0x100, signal="EngineSpeed", unit="rpm"
    )
    mime = pack_into_mime([payload])

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QDropEvent

    # Build a synthetic drop event and dispatch.
    event = QDropEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    btn.dropEvent(event)

    # One Plot opened with the curve seeded.
    plots = mgr.find_by_kind("signal_plot")
    assert len(plots) == 1
    view = plots[0].view
    assert "EngineCommand.EngineSpeed" in view.signal_paths()  # type: ignore[attr-defined]
