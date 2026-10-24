# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.trace_window.TraceWindow`` — drop dispatch, persistence."""

from __future__ import annotations

import uuid
from pathlib import Path

import can
from PySide6.QtCore import Qt

from ithildin.ui._drag_payload import (
    BusPayload,
    DbcPayload,
    MessagePayload,
)
from ithildin.ui.trace_view import TraceTreeModel
from ithildin.ui.trace_window import TraceWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import make_app_context


def test_empty_default_records_nothing(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    bus = ctx.bus_reader

    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        for i in range(4):
            sender.send(
                can.Message(
                    arbitration_id=0x100 + i,
                    data=bytes([i]),
                    is_extended_id=False,
                )
            )
    finally:
        sender.shutdown()

    qtbot.wait(150)
    assert win._model is not None
    assert win._model.rowCount() == 0
    bus.close()


def test_bus_drop_records_every_frame(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    bus = ctx.bus_reader

    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)
    qtbot.waitUntil(lambda: win._model is not None, timeout=2000)

    # Drop a BusPayload onto the tracked pane.
    assert win._tracked_pane is not None
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        sender.send(can.Message(arbitration_id=0x100, data=b"\x01", is_extended_id=False))
        sender.send(can.Message(arbitration_id=0x999, data=b"\x02", is_extended_id=False))
        sender.send(can.Message(arbitration_id=0x300, data=b"\x03", is_extended_id=False))
    finally:
        sender.shutdown()

    qtbot.waitUntil(
        lambda: win._model is not None and win._model.rowCount() >= 3,
        timeout=2000,
    )
    assert win._model is not None
    for r in range(win._model.rowCount()):
        idx = win._model.index(r, TraceTreeModel.COL_BUS)
        assert win._model.data(idx) == "CAN_1"
    bus.close()


def test_message_drop_resolves_via_database(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
    example_dbc_path: Path,
) -> None:
    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    bus = ctx.bus_reader
    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)
    qtbot.waitUntil(lambda: win._model is not None, timeout=2000)
    assert win._tracked_pane is not None

    # Drop a MessagePayload — the pane should resolve it to a (bus, dbc) pair
    # and add a msg-row in the tracked tree.
    win._tracked_pane.add_payload(MessagePayload(message="EngineCommand", message_id=0x100))

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        sender.send(can.Message(arbitration_id=0x100, data=b"\x00" * 8, is_extended_id=False))
        sender.send(can.Message(arbitration_id=0x200, data=b"\x00" * 4, is_extended_id=False))
        sender.send(can.Message(arbitration_id=0x100, data=b"\x01" * 8, is_extended_id=False))
    finally:
        sender.shutdown()

    qtbot.waitUntil(
        lambda: win._model is not None and win._model.rowCount() >= 2,
        timeout=2000,
    )
    assert win._model is not None
    for r in range(win._model.rowCount()):
        assert win._model.data(win._model.index(r, TraceTreeModel.COL_ID)) == "100"
    bus.close()


def test_dbc_drop_admits_every_message_in_dbc(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
    example_dbc_path: Path,
) -> None:
    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    bus = ctx.bus_reader
    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)
    qtbot.waitUntil(lambda: win._model is not None, timeout=2000)
    assert win._tracked_pane is not None

    # Drop the DBC — 0x100/0x200/0x300 are its messages; 0x400 is not and
    # must stay filtered out.
    win._tracked_pane.add_payload(DbcPayload(path=str(example_dbc_path)))

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        sender.send(can.Message(arbitration_id=0x100, data=b"\x00" * 8, is_extended_id=False))
        sender.send(can.Message(arbitration_id=0x200, data=b"\x00" * 4, is_extended_id=False))
        sender.send(can.Message(arbitration_id=0x300, data=b"\x00", is_extended_id=False))
        sender.send(can.Message(arbitration_id=0x400, data=b"\x00", is_extended_id=False))
    finally:
        sender.shutdown()

    qtbot.waitUntil(
        lambda: win._model is not None and win._model.rowCount() >= 3,
        timeout=2000,
    )
    assert win._model is not None
    seen_ids = {
        win._model.data(win._model.index(r, TraceTreeModel.COL_ID))
        for r in range(win._model.rowCount())
    }
    assert seen_ids == {"100", "200", "300"}
    bus.close()


def test_pause_segment_stops_model_timer(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Clicking Paused freezes the trace; clicking Running resumes it."""
    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    bus = ctx.bus_reader

    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)
    qtbot.waitUntil(lambda: win._model is not None, timeout=2000)
    assert win._model is not None
    assert win._model._timer.isActive()
    # Default state: Running checked, Paused unchecked.
    assert win._running_btn.isChecked()
    assert not win._paused_btn.isChecked()

    win._paused_btn.click()
    assert win._paused_btn.isChecked()
    assert not win._running_btn.isChecked()
    assert not win._model._timer.isActive()

    win._running_btn.click()
    assert win._running_btn.isChecked()
    assert not win._paused_btn.isChecked()
    assert win._model._timer.isActive()

    bus.close()


def test_mode_segment_round_trips_through_state(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Stream/Latest segmented selector persists the same wire tokens
    ("append" / "overwrite") so existing .itp files keep working."""
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    # Stream is the default; Latest starts unchecked.
    assert win._stream_btn.isChecked()
    assert not win._latest_btn.isChecked()

    win._latest_btn.click()
    assert win._latest_btn.isChecked()
    assert not win._stream_btn.isChecked()

    state = win.save_state()
    assert state["mode"] == "overwrite"

    win2 = TraceWindow(instance_id="t2")
    qtbot.addWidget(win2)
    win2.bind(ctx)
    win2.restore_state(state)
    assert win2._latest_btn.isChecked()
    assert not win2._stream_btn.isChecked()


def test_mode_segment_is_mutually_exclusive(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The QButtonGroup keeps exactly one mode button checked at a time."""
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    win._latest_btn.setChecked(True)
    assert win._latest_btn.isChecked()
    assert not win._stream_btn.isChecked()

    win._stream_btn.setChecked(True)
    assert win._stream_btn.isChecked()
    assert not win._latest_btn.isChecked()


def test_legacy_append_mode_state_round_trips(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """An .itp written before the relabel — mode='append' — restores
    into Stream selected."""
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    # Move off the default first so restore_state actually has to flip
    # something back.
    win._latest_btn.setChecked(True)
    win.restore_state({"mode": "append"})
    assert win._stream_btn.isChecked()
    assert not win._latest_btn.isChecked()


def test_legacy_message_trace_state_round_trips(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
    example_dbc_path: Path,
) -> None:
    """A legacy state dict from MessageTraceWindow restores into TraceWindow."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    legacy_state: dict[str, object] = {
        "filter": "",
        "paused": False,
        "mode": "append",
        "tracked": [
            {"id": 0x100, "name": "EngineCommand"},
            {"id": 0x200, "name": "BodyStatus"},
        ],
    }

    win = TraceWindow(instance_id="t1", params={"channel": "CAN_1"})
    qtbot.addWidget(win)
    win.bind(ctx)
    win.restore_state(legacy_state)

    assert win._tracked_pane is not None
    eff = win._tracked_pane.effective()
    assert eff.admits("CAN_1", 0x100)
    assert eff.admits("CAN_1", 0x200)
    assert not eff.admits("CAN_1", 0x300)


def test_legacy_bus_trace_state_round_trips(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A legacy state dict from BusTraceWindow restores into TraceWindow."""
    ctx = make_app_context(tmp_path)
    legacy_state: dict[str, object] = {
        "filter": "",
        "paused": False,
        "tracked_buses": ["CAN_1", "CAN_2"],
    }
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.restore_state(legacy_state)

    assert win._tracked_pane is not None
    eff = win._tracked_pane.effective()
    assert eff.admits("CAN_1", 0x999)
    assert eff.admits("CAN_2", 0x123)
    assert not eff.admits("CAN_3", 0x100)


def test_save_restore_new_state_shape(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
    example_dbc_path: Path,
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    assert win._tracked_pane is not None
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))

    state = win.save_state()
    assert isinstance(state.get("tracked"), list)
    tracked = state["tracked"]
    assert isinstance(tracked, list)
    assert tracked[0] == {"kind": "bus", "bus": "CAN_1"}

    win2 = TraceWindow(instance_id="t2")
    qtbot.addWidget(win2)
    win2.bind(ctx)
    win2.restore_state(state)
    assert win2._tracked_pane is not None
    eff = win2._tracked_pane.effective()
    assert eff.admits("CAN_1", 0x100)


def test_case_toggle_flips_proxy_sensitivity(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    assert win._proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseInsensitive
    assert not win._case_action.isChecked()

    win._case_action.setChecked(True)
    assert win._proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseSensitive

    win._case_action.setChecked(False)
    assert win._proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseInsensitive


def test_case_sensitive_state_round_trips(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    win._case_action.setChecked(True)
    state = win.save_state()
    assert state.get("case_sensitive") is True

    win2 = TraceWindow(instance_id="t2")
    qtbot.addWidget(win2)
    win2.bind(ctx)
    win2.restore_state(state)
    assert win2._case_action.isChecked()
    assert win2._proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseSensitive


def test_toggle_left_pane_hides_and_restores(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.resize(900, 400)
    win.show()
    qtbot.waitExposed(win)

    assert win._tracked_pane is not None
    assert win._tracked_pane.isVisible()
    win._on_toggle_left_pane()
    assert not win._tracked_pane.isVisible()
    win._on_toggle_left_pane()
    assert win._tracked_pane.isVisible()


def test_row_selection_updates_detail_pane(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
) -> None:
    """Selecting a frame row refreshes the embedded detail view in place."""
    import can

    from ithildin.ui.frame_inspector_view import FrameInspectorView

    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    assert isinstance(win._detail_view, FrameInspectorView)

    bus = ctx.bus_reader
    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)
    assert win._tracked_pane is not None
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        sender.send(can.Message(arbitration_id=0x321, data=b"\xde\xad", is_extended_id=False))
    finally:
        sender.shutdown()

    qtbot.waitUntil(
        lambda: win._model is not None and win._model.rowCount() >= 1,
        timeout=2000,
    )

    # Programmatic row selection → row-changed slot → detail view update.
    proxy_idx = win._proxy.index(0, 0)
    sel = win._tree.selectionModel()
    assert sel is not None
    sel.setCurrentIndex(proxy_idx, sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows)

    qtbot.waitUntil(
        lambda: win._detail_view._id_val.text() == "0x321 (801)",
        timeout=1000,
    )
    assert "DE" in win._detail_view._byte_cells[0].text()
    bus.close()


def test_latest_mode_inspector_follows_pinned_id(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
) -> None:
    """In Latest mode the row is a live view, so clicking it pins the
    inspector to its (bus, id) and subsequent arrivals for that ID
    refresh the inspector without re-clicking."""
    import can

    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    # Switch the segmented selector to Latest (overwrite) BEFORE any
    # frames arrive so we don't have to migrate state.
    win._latest_btn.click()

    bus = ctx.bus_reader
    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)
    assert win._tracked_pane is not None
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        sender.send(can.Message(arbitration_id=0x321, data=b"\xde\xad", is_extended_id=False))
        qtbot.waitUntil(
            lambda: win._model is not None and win._model.rowCount() >= 1,
            timeout=2000,
        )

        # Click the row; pin captures (CAN_1, 0x321).
        proxy_idx = win._proxy.index(0, 0)
        sel = win._tree.selectionModel()
        assert sel is not None
        sel.setCurrentIndex(proxy_idx, sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows)
        qtbot.waitUntil(
            lambda: "DE" in win._detail_view._byte_cells[0].text(),
            timeout=1000,
        )
        assert win._pinned_key == ("CAN_1", 0x321)

        # Send another 0x321 with different bytes — no re-click. Latest
        # mode auto-refreshes via the drain.
        sender.send(can.Message(arbitration_id=0x321, data=b"\xbe\xef", is_extended_id=False))
        qtbot.waitUntil(
            lambda: "BE" in win._detail_view._byte_cells[0].text(),
            timeout=2000,
        )
        assert "EF" in win._detail_view._byte_cells[1].text()
    finally:
        sender.shutdown()
        bus.close()


def test_stream_mode_inspector_freezes_on_clicked_frame(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
) -> None:
    """In Stream mode each row is an individual historical frame, so
    clicking it should freeze the inspector on THAT frame — new
    arrivals for the same ID must not overwrite it."""
    import can

    channel = f"tr-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    # Stream (append) is the default; assert and continue.
    assert win._stream_btn.isChecked()

    bus = ctx.bus_reader
    with qtbot.waitSignal(bus.bus_started, timeout=2000):
        bus.open(cfg)
    assert win._tracked_pane is not None
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        sender.send(can.Message(arbitration_id=0x321, data=b"\xde\xad", is_extended_id=False))
        qtbot.waitUntil(
            lambda: win._model is not None and win._model.rowCount() >= 1,
            timeout=2000,
        )

        # Click the original row. Inspector should show the clicked
        # frame, and no pin should be set.
        proxy_idx = win._proxy.index(0, 0)
        sel = win._tree.selectionModel()
        assert sel is not None
        sel.setCurrentIndex(proxy_idx, sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows)
        qtbot.waitUntil(
            lambda: "DE" in win._detail_view._byte_cells[0].text(),
            timeout=1000,
        )
        assert win._pinned_key is None

        # Send another 0x321 with different bytes. In Stream mode the
        # inspector must stay on the originally-clicked frame.
        sender.send(can.Message(arbitration_id=0x321, data=b"\xbe\xef", is_extended_id=False))
        # Wait long enough for at least one drain to occur (drain timer
        # runs at ~30 Hz). If the inspector were going to update it
        # would have by now.
        qtbot.wait(200)
        assert "DE" in win._detail_view._byte_cells[0].text()
        assert "AD" in win._detail_view._byte_cells[1].text()
    finally:
        sender.shutdown()
        bus.close()


def test_detail_pane_hidden_by_default(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
) -> None:
    """Fresh trace window starts with the inspector pane hidden; toolbar
    toggle reveals it and the splitter hands it a non-zero height."""
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.resize(900, 600)
    win.show()
    qtbot.waitExposed(win)

    assert win._detail_view is not None
    assert not win._detail_view.isVisible()

    win._toggle_detail_btn.click()
    assert win._detail_view.isVisible()
    sizes = win._detail_splitter.sizes()
    assert len(sizes) == 2 and sizes[1] > 0

    win._toggle_detail_btn.click()
    assert not win._detail_view.isVisible()


def test_filter_clear_button_hidden_until_text(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Same contract as the left dock's filter — see test_left_dock.py."""
    from PySide6.QtWidgets import QToolButton

    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win.show()
    qapp.processEvents()

    buttons = [b for b in win._filter_input.findChildren(QToolButton) if b.toolTip() == "Clear"]
    assert len(buttons) == 1
    button = buttons[0]
    assert button.isVisible() is False

    win._filter_input.setText("0x100")
    qapp.processEvents()
    assert button.isVisible() is True

    win._clear_action.trigger()
    qapp.processEvents()
    assert win._filter_input.text() == ""
    assert button.isVisible() is False
