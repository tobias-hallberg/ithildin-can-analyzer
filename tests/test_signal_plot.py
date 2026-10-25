# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.signal_plot.SignalPlotWindow``."""

from __future__ import annotations

import math
import uuid
from pathlib import Path

import can
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from ithildin.app_state.project import Project
from ithildin.core.types import FrameRecord
from ithildin.ui.signal_plot import (
    SignalPlotWindow,
    _maxlen_for_window,
    _parse_window_text,
    _SwatchButton,
)
from ithildin.workspace.schema import BusConfig, PreferencesConfig, Workspace
from tests.conftest import kill_capture, make_app_context


def test_add_and_remove_signal_curve(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    win.add_signal("EngineCommand.EngineSpeed")
    assert "EngineCommand.EngineSpeed" in win.signal_paths()
    # Curve list switches from "empty hint" to "list" when a curve is added.
    assert win._curve_list._stack.currentWidget() is win._curve_list._scroll

    win.remove_signal("EngineCommand.EngineSpeed")
    assert win.signal_paths() == []
    assert win._curve_list._stack.currentWidget() is win._curve_list._empty_hint


def test_drop_payload_triggers_add(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from ithildin.ui._drag_payload import SignalPayload

    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    win._drop.payload_dropped.emit(
        SignalPayload(message="BodyStatus", message_id=0x200, signal="VehicleSpeed")
    )
    assert win.signal_paths() == ["BodyStatus.VehicleSpeed"]


def test_drop_multiple_signals_adds_all_curves(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A multi-select MIME packet adds one curve per packed payload."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QDropEvent

    from ithildin.ui._drag_payload import SignalPayload, pack_into_mime

    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    mime = pack_into_mime(
        [
            SignalPayload(message="BodyStatus", message_id=0x200, signal="VehicleSpeed"),
            SignalPayload(message="EngineCommand", message_id=0x100, signal="EngineSpeed"),
        ]
    )
    event = QDropEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    win._drop.dropEvent(event)

    assert sorted(win.signal_paths()) == [
        "BodyStatus.VehicleSpeed",
        "EngineCommand.EngineSpeed",
    ]


def test_filter_then_decode_updates_curve(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: open virtual bus, send frames, plot updates EngineSpeed curve."""
    channel = f"plot-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)

    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)

    # EngineSpeed scale is 0.125; raw 6400 = 800 rpm.
    sender = can.Bus(interface="virtual", channel=channel)
    try:
        for raw in (6400, 6800, 7200):
            payload = raw.to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
            sender.send(can.Message(arbitration_id=0x100, data=payload, is_extended_id=False))
    finally:
        sender.shutdown()

    curve = win._curves["EngineCommand.EngineSpeed"]
    qtbot.waitUntil(lambda: len(curve.values) >= 3, timeout=3000)
    assert list(curve.values)[:3] == [800.0, 850.0, 900.0]
    ctx.bus_reader.close()


def test_add_message_works_pre_capture(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Dropping a message onto a Plot before any bus is open must add curves.

    Regression: the old guard rejected ``add_message`` whenever
    ``bus_reader.bus_name is None``, which is precisely the launch-time
    state. ``Database.decoder_for_message`` now resolves the message
    through any loaded DBC.
    """
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])  # no bus opened

    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(ctx)

    assert ctx.bus_reader.bus_name is None
    win.add_message("EngineCommand")

    assert sorted(win.signal_paths()) == [
        "EngineCommand.EngineSpeed",
        "EngineCommand.IdleSetpoint",
    ]


def test_save_and_restore_state(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """save_state then restore_state on a fresh window re-creates the curve list."""
    ctx = make_app_context(tmp_path)
    src = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(src)
    src.bind(ctx)
    src.add_signal("BodyStatus.VehicleSpeed")
    src.add_signal("EngineCommand.EngineSpeed")
    snap = src.save_state()
    assert snap["signals"] == [
        "BodyStatus.VehicleSpeed",
        "EngineCommand.EngineSpeed",
    ]
    # No curve is hidden by default → no hidden_signals key in the
    # saved state.
    assert "hidden_signals" not in snap
    # Left pane is visible by default.
    assert snap["left_pane_visible"] is True

    dst = SignalPlotWindow(instance_id="p2")
    qtbot.addWidget(dst)
    dst.bind(ctx)
    dst.restore_state(snap)
    assert sorted(dst.signal_paths()) == [
        "BodyStatus.VehicleSpeed",
        "EngineCommand.EngineSpeed",
    ]


def test_merge_axes_enabled_only_for_same_unit(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Merge is allowed only when every visible curve shares a type, and
    collapsing folds the per-curve axis columns into one."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(ctx)
    plot = win._plot

    # Two rpm signals → mergeable; two axis columns until merged.
    win.add_signal("EngineCommand.EngineSpeed")
    win.add_signal("EngineCommand.IdleSetpoint")
    assert plot._merge_enabled() is True
    assert len(plot._axis_column_widths()) == 2

    plot.set_y_merged(True)
    assert plot.is_y_merged() is True
    assert len(plot._axis_column_widths()) == 1

    # Adding a km/h signal breaks the shared type → merge auto-resets.
    win.add_signal("BodyStatus.VehicleSpeed")
    assert plot._merge_enabled() is False
    assert plot.is_y_merged() is False
    assert len(plot._axis_column_widths()) == 3


def test_merge_survives_same_type_add(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """A same-type signal added while merged keeps the axes joined; a
    different-type one only suspends the join until it's removed."""
    from ithildin.ui._gl_plot import GLPlotWidget

    plot = GLPlotWidget()
    qtbot.addWidget(plot)
    for path in ("a", "b"):
        plot.add_curve(path, QColor("white"))
        plot.set_curve_unit(path, "rpm")
    plot.set_y_merged(True)
    assert plot.is_y_merged() is True

    # Mimic add_signal's order: the curve appears with an empty unit,
    # then the unit is set a moment later. The merge must not be dropped.
    plot.add_curve("c", QColor("white"))
    plot.set_curve_unit("c", "rpm")
    assert plot.is_y_merged() is True
    assert len(plot._axis_column_widths()) == 1

    # A different unit suspends the join (intent preserved)...
    plot.add_curve("d", QColor("white"))
    plot.set_curve_unit("d", "km/h")
    assert plot.is_y_merged() is False
    assert plot.is_merge_requested() is True

    # ...and removing it rejoins automatically.
    plot.remove_curve("d")
    assert plot.is_y_merged() is True


def test_merged_state_persists(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """A merged sub-plot round-trips through save/restore."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    src = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(src)
    src.bind(ctx)
    src.add_signal("EngineCommand.EngineSpeed")
    src.add_signal("EngineCommand.IdleSetpoint")
    src._plot.set_y_merged(True)

    snap = src.save_state()
    dst = SignalPlotWindow(instance_id="p2")
    qtbot.addWidget(dst)
    dst.bind(ctx)
    dst.restore_state(snap)
    assert dst._plot.is_y_merged() is True


def test_save_and_restore_visibility(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Hiding a curve survives save → restore."""
    ctx = make_app_context(tmp_path)
    src = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(src)
    src.bind(ctx)
    src.add_signal("BodyStatus.VehicleSpeed")
    src.add_signal("EngineCommand.EngineSpeed")
    # Hide one via the same path the eye click takes.
    src._on_visibility_toggled("BodyStatus.VehicleSpeed", False)
    snap = src.save_state()
    assert snap["hidden_signals"] == ["BodyStatus.VehicleSpeed"]

    dst = SignalPlotWindow(instance_id="p2")
    qtbot.addWidget(dst)
    dst.bind(ctx)
    dst.restore_state(snap)
    assert dst._curves["BodyStatus.VehicleSpeed"].visible is False
    assert dst._curves["EngineCommand.EngineSpeed"].visible is True


def test_unrelated_message_is_filtered_out(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """If a frame's message is not a curve's parent, no decode happens."""
    ctx = make_app_context(tmp_path)
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    # No DBC loaded; a frame for any ID should not cause a decode attempt.
    f = FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x999, data=b"")
    win._on_frames_received([f])  # must not raise
    assert len(win._curves["EngineCommand.EngineSpeed"].values) == 0


def test_window_seconds_default_is_twelve(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    assert win._window_seconds == 12.0
    assert win._window_seconds_user_set is False


def test_project_default_window_applied_on_bind(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """A non-default project preference should propagate to new plots."""
    project = Project(
        path=None,
        workspace=Workspace(
            name="t",
            preferences=PreferencesConfig(default_plot_window_seconds=45.0),
        ),
        user_root=tmp_path / "_root",
    )
    ctx = make_app_context(tmp_path, project=project)
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(ctx)
    assert win._window_seconds == 45.0


def test_apply_window_seconds_resizes_curve_deques(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win.add_signal("EngineCommand.EngineSpeed")
    curve = win._curves["EngineCommand.EngineSpeed"]
    assert curve.times.maxlen == _maxlen_for_window(12.0)

    win._apply_window_seconds(60.0, mark_user=True)
    assert win._window_seconds == 60.0
    assert curve.times.maxlen == _maxlen_for_window(60.0)
    assert curve.values.maxlen == _maxlen_for_window(60.0)


def test_save_restore_round_trips_window_seconds(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    src = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(src)
    src.bind(ctx)
    src.add_signal("BodyStatus.VehicleSpeed")
    src._apply_window_seconds(30.0, mark_user=True)
    snap = src.save_state()
    assert snap["window_seconds"] == 30.0

    dst = SignalPlotWindow(instance_id="p2")
    qtbot.addWidget(dst)
    dst.bind(ctx)
    dst.restore_state(snap)
    assert dst._window_seconds == 30.0


def test_window_seconds_not_persisted_when_at_project_default(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """A window left at the project default should not write window_seconds."""
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    snap = win.save_state()
    assert "window_seconds" not in snap


def test_parse_window_text_accepts_units() -> None:
    assert _parse_window_text("30") == 30.0
    assert _parse_window_text("30 s") == 30.0
    assert _parse_window_text("2 min") == 120.0
    assert _parse_window_text("2m") == 120.0
    assert _parse_window_text("0.5 s") == 0.5
    assert _parse_window_text("") is None
    assert _parse_window_text("nope") is None
    assert _parse_window_text("-1") is None
    assert _parse_window_text("99999") is None  # over the ceiling


def test_stacked_subplots_round_trip(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Two stacked sub-plots with independent signals + a hidden curve
    survive save → restore via the new ``subplots`` state key."""
    ctx = make_app_context(tmp_path)
    src = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(src)
    src.bind(ctx)
    src.add_signal("BodyStatus.VehicleSpeed")  # lands in sub-plot 0
    # Spawn a second sub-plot and add a signal directly to it.
    src._add_subplot()
    src._subplots[1].add_signal("EngineCommand.EngineSpeed")
    src._subplots[1]._on_visibility_toggled("EngineCommand.EngineSpeed", False)

    snap = src.save_state()
    assert isinstance(snap["subplots"], list)
    assert len(snap["subplots"]) == 2
    assert snap["subplots"][0]["signals"] == ["BodyStatus.VehicleSpeed"]
    assert snap["subplots"][1]["signals"] == ["EngineCommand.EngineSpeed"]
    assert snap["subplots"][1]["hidden_signals"] == ["EngineCommand.EngineSpeed"]

    dst = SignalPlotWindow(instance_id="p2")
    qtbot.addWidget(dst)
    dst.bind(ctx)
    dst.restore_state(snap)
    assert len(dst._subplots) == 2
    assert dst._subplots[0].signal_paths() == ["BodyStatus.VehicleSpeed"]
    assert dst._subplots[1].signal_paths() == ["EngineCommand.EngineSpeed"]
    assert dst._subplots[1]._curves["EngineCommand.EngineSpeed"].visible is False


def _two_curve_plot(qtbot, tmp_path: Path) -> SignalPlotWindow:  # type: ignore[no-untyped-def]
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win.add_signal("BodyStatus.VehicleSpeed")
    win.add_signal("EngineCommand.EngineSpeed")
    return win


def test_color_menu_pins_and_resets_curve_color(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = _two_curve_plot(qtbot, tmp_path)
    sp = win._subplots[0]
    path = "BodyStatus.VehicleSpeed"
    auto_color = sp._curves[path].color()

    menu = sp._build_color_menu(path)
    assert menu is not None
    swatches = menu.findChildren(_SwatchButton)
    auto = menu.actions()[-1]
    assert len(swatches) == 6
    assert auto.text() == "Automatic colour"
    assert auto.isChecked()
    assert not any(s.isChecked() for s in swatches)

    picked = swatches[3].color()
    swatches[3].click()
    curve = sp._curves[path]
    assert curve.user_color
    assert curve.color() == picked
    assert sp._plot._curves[path].color == picked
    assert sp._curve_list._rows[path]._color_bar._color == picked

    menu = sp._build_color_menu(path)
    assert menu is not None
    swatches = menu.findChildren(_SwatchButton)
    assert [s.isChecked() for s in swatches] == [False, False, False, True, False, False]
    assert not menu.actions()[-1].isChecked()

    menu.actions()[-1].trigger()
    assert not curve.user_color
    assert curve.color() == auto_color
    assert sp._plot._curves[path].color == auto_color


def test_clicking_color_bar_requests_color_menu(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = _two_curve_plot(qtbot, tmp_path)
    pane = win._subplots[0]._curve_list
    # Swap the menu-opening slot for a recorder so no modal menu runs.
    pane.color_menu_requested.disconnect()
    requested: list[str] = []
    pane.color_menu_requested.connect(lambda path, _pos: requested.append(path))

    row = pane._rows["EngineCommand.EngineSpeed"]
    qtbot.mouseClick(row._color_bar, Qt.MouseButton.LeftButton)

    assert requested == ["EngineCommand.EngineSpeed"]
    assert pane.selected_paths() == []


def test_user_color_survives_theme_refresh(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = _two_curve_plot(qtbot, tmp_path)
    sp = win._subplots[0]
    sp.set_curve_color("BodyStatus.VehicleSpeed", QColor("#123456"))
    sp._curves["EngineCommand.EngineSpeed"].set_color(QColor("#000000"))

    sp._refresh_plot_theme()

    assert sp._curves["BodyStatus.VehicleSpeed"].color() == QColor("#123456")
    cycle = sp._palette["curves"]
    assert isinstance(cycle, list)
    assert sp._curves["EngineCommand.EngineSpeed"].color() == QColor(str(cycle[1]))


def test_save_and_restore_curve_colors(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    src = _two_curve_plot(qtbot, tmp_path)
    src._subplots[0].set_curve_color("BodyStatus.VehicleSpeed", QColor("#123456"))

    snap = src.save_state()
    assert isinstance(snap["subplots"], list)
    assert snap["subplots"][0]["colors"] == {"BodyStatus.VehicleSpeed": "#123456"}

    dst = SignalPlotWindow(instance_id="p2")
    qtbot.addWidget(dst)
    dst.bind(make_app_context(tmp_path))
    dst.restore_state(snap)
    restored = dst._curves["BodyStatus.VehicleSpeed"]
    assert restored.user_color
    assert restored.color() == QColor("#123456")
    assert not dst._curves["EngineCommand.EngineSpeed"].user_color


# ---- Continuity across bus stop, failure and reconnect ----

_SPEED = "EngineCommand.EngineSpeed"


def _send_speeds(channel: str, *raws: int) -> None:
    sender = can.Bus(interface="virtual", channel=channel)
    try:
        for raw in raws:
            payload = raw.to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
            sender.send(can.Message(arbitration_id=0x100, data=payload, is_extended_id=False))
    finally:
        sender.shutdown()


def _plot_with_samples(qtbot, tmp_path: Path, dbc: Path):  # type: ignore[no-untyped-def]
    """A plot of EngineSpeed on open bus CAN_1, holding 800, 850 and 900 rpm."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(dbc, ["CAN_1"])
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal(_SPEED)
    cfg = BusConfig(
        name="CAN_1", interface="virtual", channel=f"plot-{uuid.uuid4().hex[:8]}", bitrate=500000
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    _send_speeds(str(cfg.channel), 6400, 6800, 7200)
    curve = win._curves[_SPEED]
    qtbot.waitUntil(lambda: len(curve.values) == 3, timeout=3000)
    return ctx, win, cfg, curve


def test_plot_keeps_samples_on_bus_failure(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx, _win, _cfg, curve = _plot_with_samples(qtbot, tmp_path, example_dbc_path)

    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")
    assert [v for v in curve.values if not math.isnan(v)] == [800.0, 850.0, 900.0]
    assert curve.last_value == 900.0


def test_plot_breaks_line_at_bus_loss(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx, win, cfg, curve = _plot_with_samples(qtbot, tmp_path, example_dbc_path)
    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        kill_capture(ctx.bus_reader, "CAN_1")

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.reopen(cfg)
    _send_speeds(str(cfg.channel), 8000)
    qtbot.waitUntil(lambda: len(curve.values) == 5, timeout=3000)
    assert math.isnan(curve.values[3])
    assert curve.values[4] == 1000.0
    # The fourth sample starts a new chain instead of joining the third.
    assert win._plot._curves[_SPEED].segment_count == 2


def test_degraded_health_breaks_the_line(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx, _win, _cfg, curve = _plot_with_samples(qtbot, tmp_path, example_dbc_path)

    ctx.bus_reader.bus_health_changed.emit("CAN_1", "ok", "")
    assert len(curve.values) == 3
    ctx.bus_reader.bus_health_changed.emit("CAN_1", "errors", "no ACK")
    assert len(curve.values) == 4
    assert math.isnan(curve.values[3])


def test_x_axis_advances_while_bus_silent(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx, win, _cfg, _curve = _plot_with_samples(qtbot, tmp_path, example_dbc_path)
    gl = win._plot
    before = gl._x_max

    win._sample_wall -= 5
    win._refresh_legend_live()
    assert gl._x_max - before == pytest.approx(5.0, abs=0.5)

    # With no bus open nothing is captured, so the axis stays put.
    ctx.bus_reader.close()
    stopped_at = gl._x_max
    win._sample_wall -= 5
    win._refresh_legend_live()
    assert gl._x_max == stopped_at


def test_new_session_clears_plot(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx, _win, cfg, curve = _plot_with_samples(qtbot, tmp_path, example_dbc_path)

    ctx.bus_reader.close()
    assert [v for v in curve.values if not math.isnan(v)] == [800.0, 850.0, 900.0]
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
        assert len(curve.values) == 0


def _engine_frame(raw: int, *, is_error_frame: bool = False) -> FrameRecord:
    payload = raw.to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
    return FrameRecord(
        bus_name="CAN_1",
        timestamp=1.0,
        arbitration_id=0x100,
        data=payload,
        is_error_frame=is_error_frame,
    )


def test_error_frame_with_a_dbc_id_is_not_plotted(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    ctx.bus_reader._session_t0 = 0.0
    win = SignalPlotWindow(instance_id="p1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal(_SPEED)
    curve = win._curves[_SPEED]

    err = _engine_frame(6400, is_error_frame=True)
    win._on_frames_received([err])
    assert len(curve.values) == 0
    assert err.decoded is None

    win._on_frames_received([_engine_frame(6400)])
    assert list(curve.values) == [800.0]
