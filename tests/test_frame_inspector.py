# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.frame_inspector_view.FrameInspectorView``."""

from __future__ import annotations

from pathlib import Path

from ithildin.core.types import FrameRecord
from ithildin.ui.frame_inspector_view import FrameInspectorView
from tests.conftest import make_app_context


def test_renders_basic_fields(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    view = FrameInspectorView()
    qtbot.addWidget(view)
    view.bind(ctx)

    f = FrameRecord(
        bus_name="CAN_1",
        timestamp=12.345,
        arbitration_id=0x123,
        data=b"\xab\xcd\xef\x01",
        seq=42,
    )
    view.set_frame(f)

    assert "Frame #42" in view._header.text()
    assert "12.345s" in view._header.text()
    assert "CAN_1" in view._header.text()
    # ID cell combines hex and decimal so the identity grid uses one
    # slot instead of two stacked rows.
    assert view._id_val.text() == "0x123 (291)"
    assert view._dlc_val.text() == "4"
    # First four bytes filled, last four are placeholder.
    assert "AB" in view._byte_cells[0].text()
    assert "01" in view._byte_cells[3].text()
    assert "--" in view._byte_cells[7].text()


def test_observed_period_overrides_dbc(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A measured period from the trace model is rendered as ms."""
    ctx = make_app_context(tmp_path)
    view = FrameInspectorView()
    qtbot.addWidget(view)
    view.bind(ctx)

    f = FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x300, data=b"")
    # Sub-10 ms keeps one decimal; at or above 10 ms rounds to whole ms.
    view.set_frame(f, observed_period_s=0.0042)
    assert view._period_val.text() == "4.2 ms"

    view.set_frame(f, observed_period_s=0.0102)
    assert view._period_val.text() == "10 ms"

    view.set_frame(f, observed_period_s=0.050)
    assert view._period_val.text() == "50 ms"

    view.set_frame(f, observed_period_s=None)
    assert view._period_val.text() == "—"


def test_picks_up_cached_last_frame_on_late_bind(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """View built *after* a frame_selected emit still shows the cached frame."""
    ctx = make_app_context(tmp_path)
    f = FrameRecord(bus_name="A", timestamp=1.0, arbitration_id=0x200, data=b"\x01")
    ctx.events.frame_selected.emit(f)

    view = FrameInspectorView()
    qtbot.addWidget(view)
    view.bind(ctx)
    assert view._id_val.text() == "0x200 (512)"


def test_decoded_table_populates_when_dbc_loaded(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    view = FrameInspectorView()
    qtbot.addWidget(view)
    view.bind(ctx)

    # EngineSpeed: scale 0.125 → raw 6400, phys 800 (rpm).
    # IdleSetpoint: scale 1 → raw 700, phys 700 (rpm).
    raw = (6400).to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
    f = FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x100, data=raw)
    view.set_frame(f)

    assert view._name_val.text() == "EngineCommand"
    rows = {
        view._table.item(r, 0).text(): (
            view._table.item(r, 1).text(),
            view._table.item(r, 2).text(),
            view._table.item(r, 3).text(),
        )
        for r in range(view._table.rowCount())
    }
    assert "EngineSpeed" in rows
    assert "IdleSetpoint" in rows
    raw_speed, phys_speed, unit_speed = rows["EngineSpeed"]
    assert raw_speed == "6400"
    assert phys_speed == "800"
    assert unit_speed == "rpm"
    raw_idle, phys_idle, unit_idle = rows["IdleSetpoint"]
    assert raw_idle == "700"
    assert phys_idle == "700"
    assert unit_idle == "rpm"


def test_error_frame_shows_no_decode(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    view = FrameInspectorView()
    qtbot.addWidget(view)
    view.bind(ctx)
    raw = (6400).to_bytes(2, "little") + (700).to_bytes(2, "little") + bytes(4)
    view.set_frame(FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=0x100, data=raw))
    assert view._table.rowCount() == 2

    err = FrameRecord(
        bus_name="CAN_1",
        timestamp=0.0,
        arbitration_id=0x100,
        data=raw,
        is_error_frame=True,
        coalesced=12,
    )
    view.set_frame(err)
    assert view._name_val.text() == "Error frame ×12"
    assert view._sender_val.text() == "—"
    assert view._table.rowCount() == 0
    assert err.decoded is None


def test_undefined_multiplexer_value_shows_no_decode(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, edge_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(edge_dbc_path, ["CAN_1"])
    view = FrameInspectorView()
    qtbot.addWidget(view)
    view.bind(ctx)
    # ImdStatus defines OpCode 128 and 225 only.
    view.set_frame(FrameRecord(bus_name="CAN_1", timestamp=0.0, arbitration_id=1601, data=bytes(8)))
    assert view._name_val.text() == "ImdStatus"
    assert view._table.rowCount() == 0
