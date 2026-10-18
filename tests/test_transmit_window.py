# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.transmit_window.TransmitWindow``."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

from ithildin.core.transmit import TxMessage, TxSetup
from ithildin.ui._drag_payload import MessagePayload, SignalPayload
from ithildin.ui.transmit.controls import TxDropdown
from ithildin.ui.transmit_window import TransmitWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import make_app_context


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


def test_master_toggle_syncs_delegate_master_state(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The row delegate paints from its own master flag — it must be pushed."""
    win = TransmitWindow(instance_id="tx1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))
    _seed(win, 2)
    win._setup.transmits[0].active = True

    win._on_master_toggled(True)
    assert win._list_delegate._master_enabled is True
    win._on_master_toggled(False)
    assert win._list_delegate._master_enabled is False


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
