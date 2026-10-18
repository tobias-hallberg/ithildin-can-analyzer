# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.signal_monitor.SignalMonitorWindow``."""

from __future__ import annotations

from pathlib import Path

from ithildin.core.types import FrameRecord
from ithildin.ui.signal_monitor import SignalMonitorWindow
from tests.conftest import make_app_context


def test_add_signal_creates_bar(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    win.add_signal("EngineCommand.EngineSpeed")
    assert win.signal_paths() == ["EngineCommand.EngineSpeed"]
    assert len(win._rows) == 1
    row = win._rows["EngineCommand.EngineSpeed"]
    assert row.color_index == 0
    assert row.bar.parent() is win._content


def test_add_message_works_pre_capture(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)

    assert ctx.bus_reader.bus_name is None
    win.add_message("BodyStatus")

    assert sorted(win.signal_paths()) == ["BodyStatus.DoorOpen", "BodyStatus.VehicleSpeed"]


def test_value_updates_after_decode(qapp, qtbot, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    for raw in (6400, 12800, 9600):
        payload = raw.to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
        f = FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x100, data=payload)
        win._on_frames_received([f])

    row = win._rows["EngineCommand.EngineSpeed"]
    assert row.last == 1200.0
    assert row.bar.value_label.text() == "1200.000"


def test_unit_label_pulled_from_dbc(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    row = win._rows["EngineCommand.EngineSpeed"]
    assert row.unit == "rpm"
    assert row.bar.unit_label.text() == "rpm"
    assert row.bar.unit_label.isVisible() or not win.isVisible()


def test_unit_label_hidden_when_no_unit(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("Msg.Sig")

    row = win._rows["Msg.Sig"]
    assert row.unit == ""
    assert not row.bar.unit_label.isVisible()


def test_non_numeric_value_renders_as_string(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("Gear.State")

    row = win._rows["Gear.State"]
    # Simulate post-decode assignment (the ingest path normally writes here).
    row.last = "Drive"
    win._render()
    assert row.bar.value_label.text() == "Drive"


def test_color_index_persists_across_save_restore(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("Msg.A")
    win.add_signal("Msg.B")
    win.add_signal("Msg.C")

    state = win.save_state()
    assert state["signals"] == [
        {"path": "Msg.A", "color": 0},
        {"path": "Msg.B", "color": 1},
        {"path": "Msg.C", "color": 2},
    ]

    win2 = SignalMonitorWindow(instance_id="m2")
    qtbot.addWidget(win2)
    win2.bind(ctx)
    win2.restore_state(state)
    assert [(p, win2._rows[p].color_index) for p in win2.signal_paths()] == [
        ("Msg.A", 0),
        ("Msg.B", 1),
        ("Msg.C", 2),
    ]


def test_remove_signal_keeps_others_colors_stable(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("Msg.A")
    win.add_signal("Msg.B")
    win.add_signal("Msg.C")

    win.remove_signal("Msg.B")

    assert win.signal_paths() == ["Msg.A", "Msg.C"]
    assert win._rows["Msg.A"].color_index == 0
    assert win._rows["Msg.C"].color_index == 2

    # Next add picks the next index after the highest seen so far.
    win.add_signal("Msg.D")
    assert win._rows["Msg.D"].color_index == 3


def test_end_to_end_user_flow_add_bus_bind_drag_connect(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    import uuid

    import can

    from ithildin.app_state.project import new_unsaved
    from ithildin.ui.main_window import MainWindow
    from ithildin.workspace.schema import BusConfig

    channel = f"flow-{uuid.uuid4().hex[:8]}"
    project = new_unsaved()
    ctx = make_app_context(tmp_path, project=project)

    win_main = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win_main)
    win_main.bind_events(ctx)

    dock = win_main._left_dock_widget
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)
    dock._on_bus_declared(cfg)

    ctx.database.import_dbc(example_dbc_path)
    dock._on_bind_requested("CAN_1", str(example_dbc_path))

    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win_main._on_connect_toggled()

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        payload = (6400).to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
        sender.send(can.Message(arbitration_id=0x100, data=payload, is_extended_id=False))
    finally:
        sender.shutdown()

    qtbot.waitUntil(lambda: win._rows["EngineCommand.EngineSpeed"].last == 800.0, timeout=3000)
    assert win._rows["EngineCommand.EngineSpeed"].bar.value_label.text() == "800.000"
    ctx.bus_reader.close()


def test_end_to_end_via_toolbar_connect(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    import uuid

    import can

    from ithildin.app_state.project import new_unsaved
    from ithildin.ui.main_window import MainWindow
    from ithildin.workspace.schema import BusConfig

    channel = f"mon-tb-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    project = new_unsaved()
    project.workspace.bus = [cfg]
    ctx = make_app_context(tmp_path, project=project)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win_main = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win_main)
    win_main.bind_events(ctx)

    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win_main._on_connect_toggled()

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        payload = (6400).to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
        sender.send(can.Message(arbitration_id=0x100, data=payload, is_extended_id=False))
    finally:
        sender.shutdown()

    qtbot.waitUntil(lambda: win._rows["EngineCommand.EngineSpeed"].last == 800.0, timeout=3000)
    assert win._rows["EngineCommand.EngineSpeed"].bar.value_label.text() == "800.000"
    ctx.bus_reader.close()


def test_end_to_end_open_all_then_can_send(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    import uuid

    import can

    from ithildin.workspace.schema import BusConfig

    channel = f"mon-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = SignalMonitorWindow(instance_id="m1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open_all([cfg])

    sender = can.Bus(interface="virtual", channel=channel)
    try:
        payload = (6400).to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
        sender.send(can.Message(arbitration_id=0x100, data=payload, is_extended_id=False))
    finally:
        sender.shutdown()

    qtbot.waitUntil(lambda: win._rows["EngineCommand.EngineSpeed"].last == 800.0, timeout=3000)
    assert win._rows["EngineCommand.EngineSpeed"].bar.value_label.text() == "800.000"
    ctx.bus_reader.close()
