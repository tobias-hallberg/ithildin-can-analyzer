# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.transmit_window.TransmitWindow``."""

from __future__ import annotations

import gc
import uuid
import weakref
from pathlib import Path
from unittest.mock import MagicMock

import can
from PySide6.QtCore import Qt

from ithildin.core.transmit import TxMessage, TxSetup
from ithildin.ui._drag_payload import MessagePayload, SignalPayload
from ithildin.ui.theme import ThemeManager
from ithildin.ui.transmit.controls import TxDropdown
from ithildin.ui.transmit_window import TransmitWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import make_app_context
from tests.fake_bus import use_fake_bus


def _open_virtual_bus(ctx, qtbot, *, name: str = "CAN_1", listen_only: bool = False):  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name=name,
        interface="virtual",
        channel=f"tx-{uuid.uuid4().hex[:8]}",
        bitrate=500000,
        listen_only=listen_only,
    )
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)


def test_window_opens_empty(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    assert win._editor_title.text() == "No transmit selected"
    assert win._setup.transmits == []


def test_save_restore_round_trip(qapp, qtbot, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A populated TX setup survives save → restore."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual_bus(ctx, qtbot, name="CAN_1")

    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    # Stub the scheduler so apply() doesn't try to start real cyclic tasks.
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]

    win._setup = TxSetup(
        master_enabled=True,
        transmits=[
            TxMessage(
                bus_name="CAN_1",
                message_id=0x100,
                message_name="EngineCommand",
                mode="periodic",
                period_ms=50,
                active=True,
                signal_values={"EngineSpeed": 800.0},
            )
        ],
    )
    state = win.save_state()

    win2 = TransmitWindow(instance_id="tx2")
    qtbot.addWidget(win2)
    win2.bind(make_app_context(tmp_path))
    win2._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    win2.restore_state(state)
    assert len(win2._setup.transmits) == 1
    tx = win2._setup.transmits[0]
    assert tx.bus_name == "CAN_1"
    assert tx.message_id == 0x100
    assert tx.message_name == "EngineCommand"
    assert tx.mode == "periodic"
    assert tx.period_ms == 50
    assert tx.active is True
    assert tx.signal_values == {"EngineSpeed": 800.0}
    # Safety: master always reloads OFF — user must explicitly re-arm.
    assert win2._setup.master_enabled is False


def test_master_toggle_flips_status_bar_object_name(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    assert win._status_label.objectName() == "TxStatusBarOff"
    win._on_master_toggled(True)
    assert win._setup.master_enabled is True
    assert win._status_label.objectName() == "TxStatusBarOn"
    win._on_master_toggled(False)
    assert win._status_label.objectName() == "TxStatusBarOff"


def test_master_toggle_syncs_delegate_transmitting_state(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The row delegate paints from the scheduler's view of what is live."""
    ctx = make_app_context(tmp_path)
    _open_virtual_bus(ctx, qtbot)
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    _seed(win, 2)
    win._setup.transmits[0].active = True
    win._apply()
    tx = win._setup.transmits[0]

    win._on_master_toggled(True)
    assert win._list_delegate._is_transmitting(tx) is True
    win._on_master_toggled(False)
    assert win._list_delegate._is_transmitting(tx) is False


def test_master_toggle_restates_selected_action_button(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """ActionButton holds the only master-gated setEnabled in the view."""
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    _seed(win, 2)
    win._select(0)
    assert win._action_btn._inner.isEnabled() is False

    win._on_master_toggled(True)
    assert win._action_btn._inner.isEnabled() is True
    win._on_master_toggled(False)
    assert win._action_btn._inner.isEnabled() is False


def test_listen_only_bus_disables_master(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    _open_virtual_bus(ctx, qtbot, name="RO", listen_only=True)

    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    # Seed an active transmit on the listen-only bus.
    win._setup.transmits.append(
        TxMessage(
            bus_name="RO",
            message_id=0x100,
            message_name="EngineCommand",
            mode="periodic",
            active=True,
        )
    )
    win._refresh_listen_only_gate()
    assert win._master_btn.isEnabled() is False
    assert "listen-only" in win._master_btn.toolTip()


def test_bulk_buttons_enabled_state(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._setup.transmits.append(
        TxMessage(
            bus_name="CAN_1",
            message_id=0x100,
            message_name="EngineCommand",
            mode="one-shot",
            active=False,
        )
    )
    win._refresh_bulk_buttons()
    assert win._start_all_btn.isEnabled() is True
    assert win._stop_all_btn.isEnabled() is False
    win._setup.transmits[0].active = True
    win._refresh_bulk_buttons()
    assert win._start_all_btn.isEnabled() is False
    assert win._stop_all_btn.isEnabled() is True


# ---------- polish-pass behaviors ----------


def _seed(win: TransmitWindow, n: int = 2) -> None:
    win._setup = TxSetup(
        transmits=[
            TxMessage(
                bus_name="CAN_1",
                message_id=0x100 + i,
                message_name=f"Msg{i}",
            )
            for i in range(n)
        ],
    )
    win._rebuild_list()
    # Hands the new setup to the scheduler, as restore_state does.
    win._apply()


def test_empty_state_hides_header_widgets(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win.show()
    qapp.processEvents()
    assert win._mode_seg.isVisible() is False
    assert win._action_btn.isVisible() is False
    assert win._period_field.isVisible() is False


def test_remove_at_pops_and_shifts_selection(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    _seed(win, n=3)
    win._select(1)
    win._remove_at(1)
    assert len(win._setup.transmits) == 2
    # Row 1 was removed; selection slides to the new row at index 1
    # (originally row 2), so the editor still has focus on a row.
    assert win._selected == 1
    assert win._setup.transmits[win._selected].message_id == 0x102


def test_remove_last_row_clears_selection(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    _seed(win, n=1)
    win._select(0)
    win._remove_at(0)
    assert win._setup.transmits == []
    assert win._selected == -1


def test_duplicate_add_selects_existing_and_toasts(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    _seed(win, n=2)
    win._select(0)
    before_text = win._status_label.text()
    win._on_transmit_chosen("CAN_1", 0x101, "Msg1")
    assert len(win._setup.transmits) == 2  # no duplicate appended
    assert win._selected == 1  # existing row selected
    assert "Already in list" in win._status_label.text() or win._status_label.text() != before_text


def test_drop_message_adds_transmit(qapp, qtbot, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="ch0", bitrate=500000)
    ]
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]

    win._on_payload_dropped(MessagePayload(message="EngineCommand", message_id=0x100))
    assert len(win._setup.transmits) == 1
    tx = win._setup.transmits[0]
    assert tx.bus_name == "CAN_1"
    assert tx.message_id == 0x100
    assert tx.message_name == "EngineCommand"


def test_drop_signal_adds_parent_message(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="ch0", bitrate=500000)
    ]
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]

    win._on_payload_dropped(
        SignalPayload(
            message="EngineCommand",
            message_id=0x100,
            signal="EngineSpeed",
            unit="rpm",
        )
    )
    assert len(win._setup.transmits) == 1
    assert win._setup.transmits[0].message_id == 0x100


def test_drop_message_with_enum_signal_populates_editor(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """GearState has a 4-value enum (enum-seg → TxSegmented); the editor must
    build it without choking on cantools NamedSignalValue choice labels."""
    ctx = make_app_context(tmp_path)
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="ch0", bitrate=500000)
    ]
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]

    # First drop auto-selects the new row, so this also runs _populate_editor.
    win._on_payload_dropped(MessagePayload(message="GearState", message_id=0x300))
    assert len(win._setup.transmits) == 1
    assert win._selected == 0
    assert win._editor_title.text().startswith("GearState")


def test_mux_selector_dropdown_shows_only_the_selected_branch(
    qapp, qtbot, tmp_path: Path, edge_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="ch0", bitrate=500000)
    ]
    ctx.database.add_dbc(edge_dbc_path, ["CAN_1"])
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]

    win._on_payload_dropped(MessagePayload(message="ImdCtrl", message_id=1600))
    selector = win._signal_widgets["Request_mux"]
    assert isinstance(selector, TxDropdown)
    assert selector.value == 98
    rows = win._signal_rows
    assert not rows["Excitation_off"].isHidden()
    assert rows["Set_max_voltage"].isHidden()
    assert rows["Restart"].isHidden()

    selector.set_value(240)
    assert win._setup.transmits[0].signal_values["Request_mux"] == 240
    assert not rows["Set_max_voltage"].isHidden()
    assert rows["Excitation_off"].isHidden()


def test_list_summary_shows_raw_hex_for_wide_signals(
    qapp, qtbot, tmp_path: Path, edge_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Regression: the list summary showed 64-bit values in decimal, unlike the editor's grid."""
    ctx = make_app_context(tmp_path)
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="ch0", bitrate=500000)
    ]
    ctx.database.add_dbc(edge_dbc_path, ["CAN_1"])
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]

    win._on_payload_dropped(MessagePayload(message="WideScaled", message_id=124))
    grid = win._signal_widgets["Odometer"]
    grid.set_value(0x0123456789ABCDEF)
    grid._cells[7].editingFinished.emit()
    tx = win._setup.transmits[0]
    # Factor 10, offset -1000: the stored value is the exact physical int.
    assert tx.signal_values["Odometer"] == 0x0123456789ABCDEF * 10 - 1000
    assert win._list_delegate._summary_for(tx) == "Odometer:0x0123456789ABCDEF"


def test_drop_unbound_message_is_rejected(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    before_text = win._status_label.text()

    win._on_payload_dropped(MessagePayload(message="EngineCommand", message_id=0x100))
    assert win._setup.transmits == []
    assert win._status_label.text() != before_text


def test_adding_transmit_preserves_prior_selection(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    _seed(win, n=2)
    win._select(0)
    win._on_transmit_chosen("CAN_1", 0x300, "NewMsg")
    assert len(win._setup.transmits) == 3
    assert win._selected == 0  # focus stays on the prior selection


def test_period_field_rejects_zero(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    _seed(win, n=1)
    win._setup.transmits[0].mode = "periodic"
    win._setup.transmits[0].period_ms = 100
    win._select(0)
    # Simulate a focus-out with text "0" in the edit.
    win._period_field._edit.setText("0")
    captured: list[int] = []
    win._period_field.changed.connect(captured.append)
    win._period_field._emit()
    assert captured == []  # no zero emitted
    assert win._period_field._edit.text() == "100"  # restored to last good


def test_delegate_toggle_selects_target_row(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    _seed(win, n=3)
    win._select(0)
    # Click the run-state button on row 2 — selection must follow.
    win._on_delegate_toggle(2)
    assert win._selected == 2
    assert win._setup.transmits[2].active is True


def test_apply_is_debounced_for_signal_changes(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    win._scheduler.apply = MagicMock()  # type: ignore[union-attr]
    _seed(win, n=1)
    win._select(0)
    # Reset the apply counter; the populate above already called it.
    win._scheduler.apply.reset_mock()  # type: ignore[union-attr]
    # Hammer the signal-change path 50 times in tight succession.
    for v in range(50):
        win._on_signal_changed("EngineSpeed", float(v))
    # Debounce hasn't fired yet — apply count should be 0.
    assert win._scheduler.apply.call_count == 0  # type: ignore[union-attr]
    # Let the QTimer fire.
    qtbot.wait(80)
    # Exactly one reconcile after the burst.
    assert win._scheduler.apply.call_count == 1  # type: ignore[union-attr]


# ---------- disarm and bus lifecycle ----------


def _armed_window(ctx, qtbot) -> TransmitWindow:  # type: ignore[no-untyped-def]
    """A bound window transmitting EngineCommand periodically on CAN_1."""
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win._on_transmit_chosen("CAN_1", 0x100, "EngineCommand")
    tx = win._setup.transmits[0]
    tx.mode = "periodic"
    tx.period_ms = 20
    tx.active = True
    win._apply()
    win._on_master_toggled(True)
    assert win._setup.master_enabled is True
    return win


def test_closed_window_ignores_bus_signals(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Regression: lambdas capturing the window kept firing after it was deleted."""
    ctx = make_app_context(tmp_path)
    win = TransmitWindow(instance_id="tx1")
    win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    win.bind(ctx)
    with qtbot.waitSignal(win.destroyed, timeout=2000):
        win.close()

    ctx.bus_reader.bus_started.emit("CAN_1")
    ctx.bus_reader.bus_stopped.emit("CAN_1")
    ctx.bus_reader.bus_failed.emit("CAN_1", "gone")
    ctx.bus_reader.cyclic_failed.emit("CAN_1", 0x100, "boom")
    ctx.bus_reader.send_failed.emit("CAN_1", 0x100, "boom")
    ctx.bus_reader.bus_health_changed.emit("CAN_1", "errors", "no ACK")


def test_bus_loss_turns_master_off_and_shows_banner(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, tmp_path: Path, example_dbc_path: Path
) -> None:
    fake = use_fake_bus(monkeypatch)
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    # Kvaser: a receive error without a hardware code is fatal there.
    cfg = BusConfig(name="CAN_1", interface="kvaser", channel="X", bitrate=500000)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(cfg)
    win = _armed_window(ctx, qtbot)
    reports: list[tuple[str, str]] = []
    ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))

    with qtbot.waitSignal(win._scheduler.disarmed, timeout=2000):  # type: ignore[union-attr]
        fake.recv_exc = can.CanOperationError("adapter gone")
    assert win._master_btn.is_on is False
    assert win._status_label.objectName() == "TxStatusBarOff"
    assert not win._disarm_banner.isHidden()
    assert win._disarm_banner._label.text().startswith("Bus CAN_1 lost (")
    assert "TX disarmed" in win._disarm_banner._label.text()
    # The disarm banner says why; the master-off banner would only repeat it.
    assert win._master_off_banner.isHidden()
    ((message, kind),) = reports
    assert kind == "warn"
    assert message.startswith("TX disarmed: Bus CAN_1 lost (")


def test_rearming_hides_disarm_banner(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual_bus(ctx, qtbot)
    win = _armed_window(ctx, qtbot)

    with qtbot.waitSignal(win._scheduler.disarmed, timeout=2000):  # type: ignore[union-attr]
        ctx.bus_reader.close("CAN_1")
    assert win._disarm_banner._label.text().startswith("Bus CAN_1 closed — TX disarmed")
    # Reconnecting does not resume TX by itself.
    _open_virtual_bus(ctx, qtbot)
    assert win._setup.master_enabled is False
    assert not win._disarm_banner.isHidden()

    win._on_master_toggled(True)
    assert win._setup.master_enabled is True
    assert win._disarm_banner.isHidden()


def test_activating_a_row_on_a_listen_only_bus_disarms(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Through the one disarm path, with its banner and Report entry, not a
    silent master OFF."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1", "RO"])
    _open_virtual_bus(ctx, qtbot)
    _open_virtual_bus(ctx, qtbot, name="RO", listen_only=True)
    win = _armed_window(ctx, qtbot)
    reports: list[tuple[str, str]] = []
    ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))

    win._on_transmit_chosen("RO", 0x200, "BodyStatus")
    win._setup.transmits[-1].active = True
    with qtbot.waitSignal(win._scheduler.disarmed, timeout=2000):  # type: ignore[union-attr]
        win._apply()
    assert win._setup.master_enabled is False
    assert win._master_btn.is_on is False
    assert win._master_btn.isEnabled() is False
    assert win._disarm_banner._label.text().startswith("Bus RO is listen-only — TX disarmed")
    assert reports == [("TX disarmed: Bus RO is listen-only", "warn")]


def test_bus_info_shows_closed_after_disconnect(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    _open_virtual_bus(ctx, qtbot)
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    assert win._bus_info_text("CAN_1") == "CAN_1 · 500 kbps"

    with qtbot.waitSignal(ctx.bus_reader.bus_stopped, timeout=2000):
        ctx.bus_reader.close("CAN_1")
    assert win._bus_info_text("CAN_1") == "CAN_1 · 500 kbps (closed)"


def test_counts_reflect_live_tasks(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """A periodic row whose task died stops counting at once, before any disarm."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    _open_virtual_bus(ctx, qtbot)
    win = _armed_window(ctx, qtbot)
    tx = win._setup.transmits[0]
    assert win._count_label.text() == "1 / 1"
    assert win._list_delegate._is_transmitting(tx) is True

    win._scheduler._tasks[("CAN_1", 0x100)].stop()  # type: ignore[union-attr]
    win._refresh_counts()
    assert win._setup.master_enabled is True
    assert win._count_label.text() == "0 / 1"
    assert win._list_delegate._is_transmitting(tx) is False


def test_cyclic_failure_disarms_window(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, tmp_path: Path, example_dbc_path: Path
) -> None:
    fake = use_fake_bus(monkeypatch)
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(BusConfig(name="CAN_1", interface="pcan", channel="X", bitrate=500000))
    win = _armed_window(ctx, qtbot)
    reports: list[tuple[str, str]] = []
    ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))

    with qtbot.waitSignal(win._scheduler.disarmed, timeout=2000):  # type: ignore[union-attr]
        fake.send_exc = can.CanOperationError("transmit queue full")
    assert win._master_btn.is_on is False
    assert win._count_label.text() == "0 / 1"
    assert win._disarm_banner._label.text().startswith(
        "Periodic 0x100 on CAN_1 failed (transmit queue full) — TX disarmed"
    )
    assert reports == [
        ("TX disarmed: Periodic 0x100 on CAN_1 failed (transmit queue full)", "warn")
    ]


def test_theme_change_after_editor_rebuild_is_safe(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Regression: signal controls pinned themselves to ThemeManager forever."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win._on_transmit_chosen("CAN_1", 0x100, "EngineCommand")
    win._on_transmit_chosen("CAN_1", 0x200, "BodyStatus")
    old_control = weakref.ref(win._signal_widgets["EngineSpeed"])

    for idx in (1, 0, 1):
        win._select(idx)
    gc.collect()
    assert old_control() is None
    ThemeManager.instance().theme_changed.emit(ThemeManager.instance().current_tokens())


def test_bus_errors_disarm_and_show_in_status_bar(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, tmp_path: Path, example_dbc_path: Path
) -> None:
    from can.interfaces.pcan import basic

    from ithildin.core import bus as bus_module

    monkeypatch.setattr(bus_module, "_HEALTH_TICK_S", 0.1)
    fake = use_fake_bus(monkeypatch)
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(BusConfig(name="CAN_1", interface="pcan", channel="X", bitrate=500000))
    win = _armed_window(ctx, qtbot)
    win._select(0)

    with qtbot.waitSignal(win._scheduler.disarmed, timeout=2000) as blocker:  # type: ignore[union-attr]
        fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
    assert blocker.args == ["Bus CAN_1: bus errors (error passive)"]
    assert win._master_btn.is_on is False
    assert win._status_label.text().endswith("CAN_1 · 500 kbps · bus errors (error passive)")

    # Re-arming on a bus that is still degraded is the user's call, and holds.
    win._on_master_toggled(True)
    with qtbot.assertNotEmitted(win._scheduler.disarmed, wait=300):  # type: ignore[union-attr]
        pass
    assert win._setup.master_enabled is True

    with qtbot.waitSignal(ctx.bus_reader.bus_health_changed, timeout=2000):
        fake.status_code = 0
    assert win._status_label.text().endswith("CAN_1 · 500 kbps")
