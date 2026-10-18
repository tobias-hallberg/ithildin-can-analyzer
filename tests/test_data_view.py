# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.data_view.DataViewWindow``."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QHeaderView, QProgressBar

from ithildin.core.types import FrameRecord
from ithildin.ui.data_view import DataViewWindow
from tests.conftest import make_app_context


def test_add_signal_creates_row(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    win.add_signal("EngineCommand.EngineSpeed")
    assert win.signal_paths() == ["EngineCommand.EngineSpeed"]
    assert win._table.rowCount() == 1
    # Signal-centric display (the default): the row shows the short name.
    assert win._table.item(0, 0).text() == "EngineSpeed"


def test_add_message_works_pre_capture(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Dropping a message before a bus is open must populate rows."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(ctx)

    assert ctx.bus_reader.bus_name is None
    win.add_message("BodyStatus")

    assert sorted(win.signal_paths()) == ["BodyStatus.DoorOpen", "BodyStatus.VehicleSpeed"]


def test_value_updates_after_decode(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Each ingested frame updates the Value cell to the latest reading."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    # Raw 800 / 1600 / 1200 rpm (raw 6400 / 12800 / 9600).
    for raw in (6400, 12800, 9600):
        payload = raw.to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
        f = FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x100, data=payload)
        win._on_frames_received([f])

    row = win._rows["EngineCommand.EngineSpeed"]
    assert row.last == 1200
    assert win._table.item(0, 1).text() == "1200"


def test_unit_pulled_from_dbc(qapp, qtbot, tmp_path: Path, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")
    assert win._table.item(0, 2).text() == "rpm"


def test_dbc_range_drives_gauge(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """The gauge fills relative to the DBC-declared range, not observed traffic."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("EngineCommand.EngineSpeed")

    row = win._rows["EngineCommand.EngineSpeed"]
    assert row.dbc_min == 0
    assert row.dbc_max == 8000

    # Halfway through the DBC range: raw = 4000 / 0.125 = 32000.
    payload = (32000).to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
    f = FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x100, data=payload)
    win._on_frames_received([f])

    gauge = win._table.cellWidget(0, 3)
    assert isinstance(gauge, QProgressBar)
    # Allow a tiny scaling tolerance — gauge range is 0..1000.
    assert 490 <= gauge.value() <= 510


def test_gauge_hidden_when_dbc_lacks_range(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Signals without DBC min/max get no visible gauge."""
    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    win.add_signal("Unknown.NoRangeSignal")  # no DBC loaded → no min/max
    gauge = win._table.cellWidget(0, 3)
    assert isinstance(gauge, QProgressBar)
    assert not gauge.isVisibleTo(win._table)


def test_min_max_columns_are_gone(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The table now has 4 columns; no Min/Max."""
    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    assert win._table.columnCount() == 4
    headers = [win._table.horizontalHeaderItem(c).text() for c in range(4)]
    assert headers == ["Signal", "Value", "Unit", "Range"]


def test_signal_column_is_interactively_resizable(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The leftmost column must be Interactive so the user can drag its edge."""
    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(make_app_context(tmp_path))

    h = win._table.horizontalHeader()
    assert h.sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive
    # Range column stays Stretch so the table fills horizontally.
    assert h.sectionResizeMode(3) == QHeaderView.ResizeMode.Stretch


def test_value_cell_shows_enum_name(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Signals with a DBC value table render as the enum NAME, not the int."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    win = DataViewWindow(instance_id="d1")
    qtbot.addWidget(win)
    win.bind(ctx)
    win.add_signal("GearState.Gear")

    f = FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x300, data=bytes([3]))
    win._on_frames_received([f])

    row = win._rows["GearState.Gear"]
    assert row.last == "DRIVE"
    assert win._table.item(0, 1).text() == "DRIVE"

    gauge = win._table.cellWidget(0, 3)
    assert isinstance(gauge, QProgressBar)
    assert not gauge.isVisibleTo(win._table)
