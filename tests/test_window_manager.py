# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.window_manager.WindowManager`` over a ``QMdiArea``."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QLabel, QMdiArea, QVBoxLayout, QWidget

from ithildin.ui.window_manager import WindowManager
from ithildin.workspace.schema import ViewState


class _FakeView(QWidget):
    """Minimal stand-in for a ``ChildView``: a QWidget with a kind and instance_id."""

    # No ViewHeaderBar — WindowManager treats a None header as "no
    # maximized-window chrome to route", the same as a headerless view.
    header = None

    def __init__(self, kind: str, instance_id: str) -> None:
        super().__init__()
        self._kind = kind
        self._instance_id = instance_id
        self.setWindowTitle(f"{kind}:{instance_id}")
        self.resize(QSize(400, 300))
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.windowTitle()))

    def save_state(self) -> dict[str, object]:
        return {}

    def restore_state(self, _state: dict[str, object]) -> None:
        return None

    def set_default_title(self, title: str) -> None:
        self.setWindowTitle(title)


def _factory(_mgr, kind: str, instance_id: str, _params):  # type: ignore[no-untyped-def]
    return _FakeView(kind, instance_id)


def _build(qtbot, tmp_path: Path) -> tuple[WindowManager, QMdiArea]:
    """Build an MDI area + WindowManager pair pointed at tmp_path/layout.json."""
    area = QMdiArea()
    qtbot.addWidget(area)
    mgr = WindowManager(area, tmp_path / "layout.json", _factory)
    return mgr, area


def test_open_emits_signal_and_registers(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    with qtbot.waitSignal(mgr.window_opened, timeout=1000) as blocker:
        wid = mgr.open("replay")
    assert blocker.args == [wid, "replay"]
    assert len(mgr.list_windows()) == 1


def test_singleton_kind_focuses_existing(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    a = mgr.open("replay")
    b = mgr.open("replay")
    assert a == b
    assert len(mgr.find_by_kind("replay")) == 1


def test_multi_instance_kind_makes_new_each_time(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    a = mgr.open("message_trace", params={"channel": "CAN_1"})
    b = mgr.open("message_trace", params={"channel": "CAN_2"})
    assert a != b
    assert len(mgr.find_by_kind("message_trace")) == 2


def test_close_deregisters(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    wid = mgr.open("record")
    with qtbot.waitSignal(mgr.window_closed, timeout=1000) as blocker:
        mgr.close(wid)
    assert blocker.args == [wid]
    assert mgr.list_windows() == []


def test_minimize_hides_subwindow(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Minimizing a subwindow hides it; ``showNormal`` brings it back."""
    mgr, area = _build(qtbot, tmp_path)
    area.show()
    mgr.open("message_trace", params={"channel": "CAN_1"})
    sub = mgr.list_windows()[0].subwindow
    qtbot.waitUntil(sub.isVisible, timeout=1000)

    sub.showMinimized()
    qtbot.waitUntil(lambda: not sub.isVisible(), timeout=1000)
    assert sub.isMinimized()

    sub.showNormal()
    qtbot.waitUntil(lambda: sub.isVisible() and not sub.isMinimized(), timeout=1000)


def test_subwindow_added_to_area(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, area = _build(qtbot, tmp_path)
    wid = mgr.open("message_trace", params={"channel": "CAN_1"})
    sub_windows = area.subWindowList()
    assert len(sub_windows) == 1
    entry = mgr.list_windows()[0]
    assert entry.window_id == wid
    assert entry.subwindow in sub_windows


def test_cascade_offsets_new_subwindows(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, area = _build(qtbot, tmp_path)
    area.resize(1400, 1100)  # Big enough that cascading two 640×480 wins does not wrap.
    area.show()
    a = mgr.open("message_trace", params={"channel": "CAN_1"})
    b = mgr.open("message_trace", params={"channel": "CAN_2"})
    entries = {e.window_id: e for e in mgr.list_windows()}
    p1 = entries[a].subwindow.pos()
    p2 = entries[b].subwindow.pos()
    # Newly-spawned cascade by 28 px (handoff §2.1).
    assert p2.x() == p1.x() + 28
    assert p2.y() == p1.y() + 28


def test_new_subwindow_fits_inside_viewport(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A new window's geometry never extends past the visible viewport."""
    mgr, area = _build(qtbot, tmp_path)
    area.resize(500, 250)  # Tighter than _FakeView's 400×300 — height must clamp.
    area.show()
    qtbot.waitUntil(lambda: area.viewport().size().height() > 0, timeout=1000)

    mgr.open("message_trace", params={"channel": "CAN_1"})
    sub = mgr.list_windows()[0].subwindow
    g = sub.geometry()
    vp = area.viewport().size()
    assert g.x() >= 0 and g.y() >= 0
    assert g.x() + g.width() <= vp.width()
    assert g.y() + g.height() <= vp.height()


def test_cascade_wraps_when_past_viewport_edge(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Cascade wraps to (0, 0) when the next slot would extend past the viewport."""
    mgr, area = _build(qtbot, tmp_path)
    area.resize(420, 320)  # Fits one 400×300; cascade(28,28)+400×300 overflows.
    area.show()
    qtbot.waitUntil(lambda: area.viewport().size().width() > 0, timeout=1000)

    a = mgr.open("message_trace", params={"channel": "CAN_1"})
    b = mgr.open("message_trace", params={"channel": "CAN_2"})
    entries = {e.window_id: e for e in mgr.list_windows()}
    p2 = entries[b].subwindow.pos()
    # Both axes wrap because cascade(28, 28) + 400×300 overflows the 420×320 viewport.
    assert p2.x() == 0
    assert p2.y() == 0
    assert a != b


def test_atomic_write_via_tmp_replace(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    mgr.open("message_trace")
    mgr.flush()
    layout = tmp_path / "layout.json"
    assert layout.exists()
    assert not (tmp_path / "layout.json.tmp").exists()
    data = json.loads(layout.read_text(encoding="utf-8"))
    assert [w["kind"] for w in data["windows"]] == ["message_trace"]


def test_restore_from_workspace_creates_views(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Workspace ``views[]`` is the source of truth for which windows exist."""
    mgr, _area = _build(qtbot, tmp_path)
    mgr.restore_from_workspace(
        [
            ViewState(kind="message_trace", instance_id="abc123", params={"channel": "CAN_1"}),
            ViewState(kind="record", instance_id="def456"),
        ]
    )
    assert len(mgr.list_windows()) == 2
    by_kind = {e.kind: e.instance_id for e in mgr.list_windows()}
    assert by_kind["message_trace"] == "abc123"
    assert by_kind["record"] == "def456"


def test_restore_from_disk_applies_geometry_to_existing_windows(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """``restore_from_disk`` matches by instance_id; orphan entries are dropped."""
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "windows": [
                    {
                        "kind": "message_trace",
                        "instance_id": "abc123",
                        "params": {"channel": "CAN_1"},
                        "geometry": {"x": 30, "y": 40, "w": 500, "h": 250},
                    },
                    {
                        "kind": "record",
                        "instance_id": "ghost-iid",
                        "params": {},
                        "geometry": {"x": 0, "y": 0, "w": 200, "h": 100},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    mgr, _area = _build(qtbot, tmp_path)
    mgr.restore_from_workspace(
        [ViewState(kind="message_trace", instance_id="abc123", params={"channel": "CAN_1"})]
    )

    restored = mgr.restore_from_disk()
    assert restored == 1  # only the matching trace window; ghost record dropped
    entry = mgr.list_windows()[0]
    geom = entry.subwindow.geometry()
    assert (geom.width(), geom.height()) == (500, 250)
    assert (geom.x(), geom.y()) == (30, 40)


def test_restore_missing_file_is_zero(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    mgr, _area = _build(qtbot, tmp_path)
    assert mgr.restore_from_disk() == 0


def test_snapshot_views_captures_state(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """``snapshot_views`` returns each entry's kind / instance_id / params / state."""
    mgr, _area = _build(qtbot, tmp_path)
    mgr.open("message_trace", params={"channel": "CAN_1"}, instance_id="iid-1")
    mgr.open("record", instance_id="iid-2")

    snap = mgr.snapshot_views()
    by_iid = {s["instance_id"]: s for s in snap}
    assert by_iid["iid-1"]["kind"] == "message_trace"
    assert by_iid["iid-1"]["params"] == {"channel": "CAN_1"}
    # _FakeView does not override save_state, so the default empty dict is returned.
    assert by_iid["iid-1"]["state"] == {}
