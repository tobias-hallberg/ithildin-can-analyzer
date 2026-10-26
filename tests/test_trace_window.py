# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.trace_window.TraceWindow`` — drop dispatch, persistence."""

from __future__ import annotations

import uuid
from pathlib import Path

import can
from PySide6.QtCore import Qt

from ithildin.app_state.context import AppContext
from ithildin.app_state.project import Project, new_unsaved
from ithildin.ui._drag_payload import (
    BusPayload,
    DbcPayload,
    MessagePayload,
)
from ithildin.ui.trace_view import TraceTreeModel
from ithildin.ui.trace_window import TraceWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import kill_capture, make_app_context


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


def test_case_toggle_flips_filter_sensitivity(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    assert win._model is not None

    assert not win._model._case_sensitive
    assert not win._case_action.isChecked()

    win._case_action.setChecked(True)
    assert win._model._case_sensitive

    win._case_action.setChecked(False)
    assert not win._model._case_sensitive


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
    assert win2._model is not None
    assert win2._model._case_sensitive


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
    sel = win._tree.selectionModel()
    assert sel is not None
    sel.setCurrentIndex(
        win._model.index(0, 0), sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows
    )

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
        idx = win._model.index(0, 0)
        sel = win._tree.selectionModel()
        assert sel is not None
        sel.setCurrentIndex(idx, sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows)
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
        idx = win._model.index(0, 0)
        sel = win._tree.selectionModel()
        assert sel is not None
        sel.setCurrentIndex(idx, sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows)
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


# ---- Health banner ----


def _trace_on_open_bus(qtbot, tmp_path: Path):  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"tr-{uuid.uuid4().hex[:8]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    return ctx, win


def test_health_banner_shows_bus_errors_and_hides_on_recovery(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win = _trace_on_open_bus(qtbot, tmp_path)

    ctx.bus_reader.bus_health_changed.emit("CAN_1", "errors", "no ACK")
    assert not win._banner.isHidden()
    assert win._banner.kind == "warn"
    assert win._banner._label.text().startswith("Bus CAN_1: bus errors (no ACK).")
    assert win._banner._action_btn.isHidden()

    ctx.bus_reader.bus_health_changed.emit("CAN_1", "ok", "")
    assert win._banner.isHidden()


def test_health_banner_never_replaces_a_failure_banner(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    ctx.bus_reader._failures["CAN_2"] = "adapter disconnected"
    ctx.bus_reader.bus_failed.emit("CAN_2", "adapter disconnected")
    ctx.bus_reader.bus_health_changed.emit("CAN_1", "bus_off", "")
    ctx.bus_reader.bus_health_changed.emit("CAN_1", "ok", "")
    assert not win._banner.isHidden()
    assert win._banner._label.text() == "Bus CAN_2 disconnected: adapter disconnected"


def test_health_detail_alone_keeps_a_dismissed_banner_closed(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win = _trace_on_open_bus(qtbot, tmp_path)
    emit = ctx.bus_reader.bus_health_changed.emit

    emit("CAN_1", "errors", "error warning")
    emit("CAN_1", "errors", "error passive")
    assert win._banner._label.text().startswith("Bus CAN_1: bus errors (error passive).")
    win._banner._close_btn.click()

    emit("CAN_1", "errors", "error warning")
    assert win._banner.isHidden()
    emit("CAN_1", "bus_off", "")
    assert not win._banner.isHidden()


def test_health_banner_keeps_the_worst_bus(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    emit = ctx.bus_reader.bus_health_changed.emit

    emit("CAN_1", "errors", "no ACK")
    emit("CAN_2", "bus_off", "")
    emit("CAN_3", "errors", "no ACK")
    assert win._banner._label.text().startswith("Bus CAN_2: bus-off.")
    emit("CAN_3", "ok", "")
    assert win._banner._label.text().startswith("Bus CAN_2: bus-off.")

    # The bus shown recovers: the worst one left takes over.
    emit("CAN_2", "ok", "")
    assert win._banner._label.text().startswith("Bus CAN_1: bus errors (no ACK).")
    ctx.bus_reader.bus_stopped.emit("CAN_1")
    assert win._banner.isHidden()


def test_banner_taken_by_another_message_frees_the_health_banner(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)

    ctx.bus_reader._failures["CAN_2"] = "adapter disconnected"
    ctx.bus_reader.bus_failed.emit("CAN_2", "adapter disconnected")
    win._on_superseded("Superseded by the whole bus")
    ctx.bus_reader.bus_health_changed.emit("CAN_1", "bus_off", "")
    assert win._banner._label.text().startswith("Bus CAN_1: bus-off.")


def test_bus_off_banner_returns_once_a_covering_banner_goes(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Bus-off doesn't change by itself, so the banner is CAN_1's only Reconnect."""
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    reader = ctx.bus_reader

    reader._failures["CAN_2"] = "adapter disconnected"
    reader.bus_failed.emit("CAN_2", "adapter disconnected")
    reader.bus_health_changed.emit("CAN_1", "bus_off", "")
    assert win._banner._label.text() == "Bus CAN_2 disconnected: adapter disconnected"

    # CAN_2 reconnected.
    del reader._failures["CAN_2"]
    reader.bus_started.emit("CAN_2")
    assert win._banner._label.text().startswith("Bus CAN_1: bus-off.")
    assert win._banner._action_btn.text() == "Reconnect"

    reader.error.emit("Bus 'CAN_3' failed to open")
    win._banner._close_btn.click()
    assert not win._banner.isHidden()
    assert win._banner._label.text().startswith("Bus CAN_1: bus-off.")


def test_bus_off_banner_reconnects_the_bus(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win = _trace_on_open_bus(qtbot, tmp_path)

    ctx.bus_reader.bus_health_changed.emit("CAN_1", "bus_off", "")
    assert win._banner.kind == "error"
    assert win._banner._action_btn.text() == "Reconnect"
    assert not win._banner._action_btn.isHidden()

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._banner._action_btn.click()
    assert ctx.bus_reader.bus_names() == ["CAN_1"]
    assert win._banner.isHidden()


# ---- Continuity across bus stop, failure and reconnect ----


def _send_ids(channel: str, *ids: int) -> None:
    sender = can.Bus(interface="virtual", channel=channel)
    try:
        for aid in ids:
            sender.send(can.Message(arbitration_id=aid, data=b"\x00", is_extended_id=False))
    finally:
        sender.shutdown()


def _trace_recording_bus(  # type: ignore[no-untyped-def]
    qtbot, tmp_path: Path, project: Project | None = None
) -> tuple[AppContext, TraceWindow, BusConfig]:
    """A Trace subscribed to bus CAN_1, open, with three rows in it."""
    ctx = make_app_context(tmp_path, project=project)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"tr-{uuid.uuid4().hex[:8]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    assert win._tracked_pane is not None
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))
    _send_ids(str(cfg.channel), 0x100, 0x101, 0x102)
    model = win._model
    assert model is not None
    qtbot.waitUntil(lambda: model.rowCount() == 3, timeout=2000)
    return ctx, win, cfg


def test_bus_failure_keeps_trace_rows(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, _cfg = _trace_recording_bus(qtbot, tmp_path)
    model = win._model
    assert model is not None

    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")
    assert win._model is model
    assert model.rowCount() == 3
    fg = model.data(model.index(0, TraceTreeModel.COL_ID), Qt.ItemDataRole.ForegroundRole)
    assert fg == model._dim_color


def test_reconnect_appends_without_replay_or_skip(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, cfg = _trace_recording_bus(qtbot, tmp_path)
    model = win._model
    assert model is not None
    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")
    t0 = ctx.bus_reader.session_t0

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._banner._action_btn.click()
    _send_ids(str(cfg.channel), 0x200, 0x201)
    qtbot.waitUntil(lambda: model.rowCount() == 5, timeout=2000)
    qtbot.wait(150)
    assert model.rowCount() == 5
    assert ctx.bus_reader.session_t0 == t0
    assert model.data(model.index(4, TraceTreeModel.COL_ID), Qt.ItemDataRole.ForegroundRole) is None


def test_second_reconnect_click_leaves_the_opening_bus_alone(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, _cfg = _trace_recording_bus(qtbot, tmp_path)
    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")
    stopped: list[str] = []
    ctx.bus_reader.bus_stopped.connect(stopped.append)

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._banner._action_btn.click()
        win._banner._action_btn.click()
    assert stopped == []
    assert ctx.bus_reader.bus_names() == ["CAN_1"]


def test_pinned_inspector_follows_a_reconnected_bus(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The reopened bus's new ring restarts seq at 0."""
    ctx, win, cfg = _trace_recording_bus(qtbot, tmp_path)
    model = win._model
    inspector = win._detail_view
    assert model is not None and inspector is not None
    win._latest_btn.click()
    sender = can.Bus(interface="virtual", channel=cfg.channel)
    try:
        sender.send(can.Message(arbitration_id=0x100, data=b"\xde\xad", is_extended_id=False))
        qtbot.waitUntil(lambda: model.rowCount() == 1, timeout=2000)
        sel = win._tree.selectionModel()
        assert sel is not None
        sel.setCurrentIndex(
            win._model.index(0, 0), sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows
        )
        qtbot.waitUntil(lambda: "DE" in inspector._byte_cells[0].text(), timeout=1000)
        with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
            kill_capture(ctx.bus_reader, "CAN_1")

        with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
            win._banner._action_btn.click()
        sender.send(can.Message(arbitration_id=0x100, data=b"\xbe\xef", is_extended_id=False))
        qtbot.waitUntil(lambda: "BE" in inspector._byte_cells[0].text(), timeout=2000)
    finally:
        sender.shutdown()


def test_reconnect_opens_the_workspace_config(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The bus's declaration may have changed since it was opened."""
    project = new_unsaved(user_root=tmp_path)
    ctx, win, cfg = _trace_recording_bus(qtbot, tmp_path, project=project)
    project.workspace.bus = [cfg.model_copy(update={"bitrate": 250000})]
    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._banner._action_btn.click()
    reopened = ctx.bus_reader.last_config_for("CAN_1")
    assert reopened is not None
    assert reopened.bitrate == 250000


def test_new_session_clears_trace(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, cfg = _trace_recording_bus(qtbot, tmp_path)
    model = win._model
    assert model is not None

    ctx.bus_reader.close()
    assert model.rowCount() == 3
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
        assert model.rowCount() == 0
    assert win._counts_label.text() == "RX 0   ERR 0"


def test_paused_trace_keeps_rows_on_bus_stop(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, _cfg = _trace_recording_bus(qtbot, tmp_path)
    model = win._model
    assert model is not None

    win._paused_btn.click()
    ctx.bus_reader.close()
    assert win._model is model
    assert model.rowCount() == 3


def test_stopping_one_bus_does_not_replay_the_other(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    channels = {}
    for name in ("A", "B"):
        channels[name] = f"tr-{uuid.uuid4().hex[:8]}"
        cfg = BusConfig(name=name, interface="virtual", channel=channels[name], bitrate=500000)
        with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
            ctx.bus_reader.open(cfg)
        assert win._tracked_pane is not None
        win._tracked_pane.add_payload(BusPayload(bus_name=name))
    _send_ids(channels["A"], 0x100, 0x101)
    _send_ids(channels["B"], 0x200, 0x201)
    model = win._model
    assert model is not None
    qtbot.waitUntil(lambda: model.rowCount() == 4, timeout=2000)

    ctx.bus_reader.close("B")
    qtbot.wait(150)
    assert model.rowCount() == 4
    ids = {model.data(model.index(r, TraceTreeModel.COL_ID)) for r in range(4)}
    assert ids == {"100", "101", "200", "201"}


def test_restored_latest_mode_reaches_the_model(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The model is built once in bind, before restore_state runs."""
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win.restore_state({"mode": "overwrite"})
    assert win._model is not None
    assert win._model.mode() == "overwrite"


# ---- Live window, filter and counters ----


def _select_row(win: TraceWindow, row: int) -> None:
    assert win._model is not None
    sel = win._tree.selectionModel()
    sel.setCurrentIndex(
        win._model.index(row, 0), sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows
    )


def test_filter_text_filters_rows_in_the_model(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _ctx, win, _cfg = _trace_recording_bus(qtbot, tmp_path)
    model = win._model
    assert model is not None
    assert win._tree.model() is model

    win._filter_input.setText("101")
    qtbot.waitUntil(lambda: model.rowCount() == 1, timeout=2000)
    assert model.data(model.index(0, TraceTreeModel.COL_ID)) == "101"
    win._filter_input.clear()
    qtbot.waitUntil(lambda: model.rowCount() == 3, timeout=2000)


def test_selection_cleared_when_row_leaves_live_window(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, cfg = _trace_recording_bus(qtbot, tmp_path)
    model = win._model
    assert model is not None
    model._live_rows = 3
    selected: list[object] = []
    ctx.events.frame_selected.connect(selected.append)
    # The oldest row, which the next frame pushes out of the window while
    # rows stay for the view to move the selection to.
    _select_row(win, 2)
    clicked = model.frame_at(2)
    assert clicked is not None and clicked.arbitration_id == 0x100
    assert selected == [clicked]

    _send_ids(str(cfg.channel), 0x200)
    qtbot.waitUntil(lambda: model.rx_count() == 4, timeout=2000)
    assert model.rowCount() == 3
    assert not win._tree.selectionModel().currentIndex().isValid()
    assert selected == [clicked]
    assert win._detail_view is not None
    assert win._detail_view._id_val.text() == "0x100 (256)"


def test_pause_shows_history_and_resume_skips_paused_frames(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win = _trace_on_open_bus(qtbot, tmp_path)
    model = win._model
    assert model is not None and win._tracked_pane is not None
    model._live_rows = 2
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))
    channel = str(ctx.bus_reader.last_config_for("CAN_1").channel)  # type: ignore[union-attr]
    ring = ctx.bus_reader.rings["CAN_1"]
    _send_ids(channel, 0x100, 0x101, 0x102, 0x103, 0x104)
    qtbot.waitUntil(lambda: model.rx_count() == 5, timeout=2000)
    assert model.rowCount() == 2

    win._paused_btn.click()
    assert model.rowCount() == 5
    assert model.data(model.index(4, TraceTreeModel.COL_ID)) == "100"
    _send_ids(channel, 0x300, 0x301)
    qtbot.waitUntil(lambda: ring.total_seq == 7, timeout=2000)
    qtbot.wait(100)
    assert model.rowCount() == 5

    win._running_btn.click()
    assert model.rowCount() == 2
    assert model.data(model.index(0, TraceTreeModel.COL_ID)) == "104"
    _send_ids(channel, 0x400)
    qtbot.waitUntil(
        lambda: model.data(model.index(0, TraceTreeModel.COL_ID)) == "400", timeout=2000
    )
    assert model.rx_count() == 6


def test_trace_does_not_decode_collapsed_rows(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.resize(900, 400)
    with qtbot.waitExposed(win):
        win.show()
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"tr-{uuid.uuid4().hex[:8]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    assert win._tracked_pane is not None
    win._tracked_pane.add_payload(BusPayload(bus_name="CAN_1"))
    sender = can.Bus(interface="virtual", channel=cfg.channel)
    try:
        for i in range(5):
            sender.send(
                can.Message(arbitration_id=0x100, data=bytes([i] * 8), is_extended_id=False)
            )
    finally:
        sender.shutdown()
    model = win._model
    assert model is not None
    qtbot.waitUntil(lambda: model.rowCount() == 5, timeout=2000)
    win._tree.viewport().repaint()

    frames, _ = ctx.bus_reader.rings["CAN_1"].read_since(-1)
    assert len(frames) == 5
    assert all(f.decoded is None for f in frames)
    win._tree.expand(model.index(0, 0))
    win._tree.viewport().repaint()
    assert frames[-1].decoded is not None
    assert all(f.decoded is None for f in frames[:-1])


def test_lost_label_only_when_nonzero(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    assert win._model is not None

    win._model.counters_changed.emit(3, 1, 0)
    assert win._counts_label.text() == "RX 3   ERR 1"
    win._model.counters_changed.emit(3, 1, 5)
    assert win._counts_label.text() == "RX 3   ERR 1   LOST 5"


# ---- Failure banner over every failed bus ----


def _trace_on_two_failed_buses(qtbot, tmp_path: Path):  # type: ignore[no-untyped-def]
    project = new_unsaved(user_root=tmp_path)
    project.workspace.bus = [
        BusConfig(
            name=name, interface="virtual", channel=f"tr-{uuid.uuid4().hex[:8]}", bitrate=500000
        )
        for name in ("CAN_1", "CAN_2")
    ]
    ctx = make_app_context(tmp_path, project=project)
    win = TraceWindow(instance_id="t1")
    qtbot.addWidget(win)
    win.bind(ctx)
    for cfg in project.workspace.bus:
        with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
            ctx.bus_reader.open(cfg)
    for cfg in project.workspace.bus:
        with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
            kill_capture(ctx.bus_reader, cfg.name)
    return ctx, win, project


def test_failure_banner_shows_the_newest_and_reconnects_all(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, project = _trace_on_two_failed_buses(qtbot, tmp_path)
    assert win._banner._label.text().startswith("Bus CAN_2 disconnected:")
    assert win._banner._label.text().endswith("(+1 more)")
    assert win._banner._action_btn.text() == "Reconnect all"
    # Pinned since they opened: the reconnect must carry the pin.
    project.workspace.bus = [
        b.model_copy(update={"hw_id": f"pin-{b.name}"}) for b in project.workspace.bus
    ]

    win._banner._action_btn.click()
    reader = ctx.bus_reader
    qtbot.waitUntil(
        lambda: all(reader.health(n) is not None for n in ("CAN_1", "CAN_2")), timeout=2000
    )
    for name in ("CAN_1", "CAN_2"):
        cfg = reader.last_config_for(name)
        assert cfg is not None and cfg.hw_id == f"pin-{name}"
    assert win._banner.isHidden()


def test_failure_banner_reconnecting_one_names_the_other(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, project = _trace_on_two_failed_buses(qtbot, tmp_path)

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(project.workspace.bus[1])
    assert win._banner._label.text().startswith("Bus CAN_1 disconnected:")
    assert win._banner._action_btn.text() == "Reconnect"


def test_dismissed_failure_banner_stays_closed_on_a_reconnect(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, win, project = _trace_on_two_failed_buses(qtbot, tmp_path)
    win._banner._close_btn.click()

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(project.workspace.bus[1])
    assert win._banner.isHidden()
