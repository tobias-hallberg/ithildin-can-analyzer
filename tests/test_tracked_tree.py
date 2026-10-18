# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui._tracked_tree`` — subscription tree + effective-set."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QWidget

from ithildin.app_state.database import Database
from ithildin.ui._tracked_tree import (
    TrackedTreeModel,
    TrackedTreePane,
    _BusEntry,
    _DbcEntry,
    _MsgEntry,
)


def test_bus_subscription_admits_every_frame_from_that_bus(qapp) -> None:  # type: ignore[no-untyped-def]
    model = TrackedTreeModel()
    model.recompute({})
    item, _ = model.add_bus("CAN_1")
    assert item is not None
    eff = model.effective()
    assert eff.admits("CAN_1", 0x100)
    assert eff.admits("CAN_1", 0x999)  # unknown ID
    assert not eff.admits("CAN_2", 0x100)


def test_dbc_subscription_admits_only_ids_in_that_dbc(qapp) -> None:  # type: ignore[no-untyped-def]
    model = TrackedTreeModel()
    path = Path("/tmp/example.dbc")
    model.recompute({path: frozenset({0x100, 0x200})})
    item, _ = model.add_dbc("CAN_1", path)
    assert item is not None
    eff = model.effective()
    assert eff.admits("CAN_1", 0x100)
    assert eff.admits("CAN_1", 0x200)
    assert not eff.admits("CAN_1", 0x300)
    assert not eff.admits("CAN_2", 0x100)


def test_msg_subscription_admits_only_that_id_on_that_bus(qapp) -> None:  # type: ignore[no-untyped-def]
    model = TrackedTreeModel()
    model.recompute({})
    item, _ = model.add_message(
        bus_name="CAN_1",
        dbc_path=Path("/tmp/example.dbc"),
        message_id=0x100,
        message_name="EngineCommand",
    )
    assert item is not None
    eff = model.effective()
    assert eff.admits("CAN_1", 0x100)
    assert not eff.admits("CAN_1", 0x200)
    assert not eff.admits("CAN_2", 0x100)


def test_redundant_drops_are_silent_no_ops(qapp) -> None:  # type: ignore[no-untyped-def]
    model = TrackedTreeModel()
    path = Path("/tmp/a.dbc")
    model.recompute({path: frozenset({0x100})})
    bus_item, _ = model.add_bus("CAN_1")
    assert bus_item is not None
    # Adding a DBC under a tracked bus is a no-op.
    dbc_item, msg = model.add_dbc("CAN_1", path)
    assert dbc_item is None
    assert msg is None
    # Adding a specific message under a tracked bus is a no-op.
    msg_item, _ = model.add_message(
        bus_name="CAN_1",
        dbc_path=path,
        message_id=0x100,
        message_name="X",
    )
    assert msg_item is None


def test_bus_drop_supersedes_existing_dbc_and_msg(qapp) -> None:  # type: ignore[no-untyped-def]
    model = TrackedTreeModel()
    path = Path("/tmp/a.dbc")
    model.recompute({path: frozenset({0x100})})
    model.add_dbc("CAN_1", path)
    model.add_message(
        bus_name="CAN_1",
        dbc_path=path,
        message_id=0x200,
        message_name="Other",
    )
    entries_before = model.entries()
    assert any(isinstance(e, _DbcEntry) for e in entries_before)

    received: list[str] = []
    model.superseded.connect(received.append)

    bus_item, msg = model.add_bus("CAN_1")
    assert bus_item is not None
    assert msg is not None and "Promoted" in msg
    assert received and "CAN_1" in received[0]
    # Only the bus row remains.
    entries_after = model.entries()
    assert len(entries_after) == 1
    assert isinstance(entries_after[0], _BusEntry)


def test_msg_subscription_does_not_double_count_when_dbc_added_after(  # type: ignore[no-untyped-def]
    qapp,
) -> None:
    """Adding a DBC subscription removes per-message subs on the same bus
    that the DBC would otherwise cover."""
    model = TrackedTreeModel()
    path = Path("/tmp/a.dbc")
    model.recompute({path: frozenset({0x100, 0x200})})
    model.add_message(
        bus_name="CAN_1",
        dbc_path=path,
        message_id=0x100,
        message_name="X",
    )
    received: list[str] = []
    model.superseded.connect(received.append)
    dbc_item, msg = model.add_dbc("CAN_1", path)
    assert dbc_item is not None
    assert msg is not None and "Promoted" in msg
    # The dbc remains; the original msg row is gone.
    entries = model.entries()
    assert any(isinstance(e, _DbcEntry) for e in entries)
    msg_entries = [e for e in entries if isinstance(e, _MsgEntry)]
    assert all(e.dbc_path != path for e in msg_entries)


def test_remove_cascades_through_subtree(qapp) -> None:  # type: ignore[no-untyped-def]
    model = TrackedTreeModel()
    path = Path("/tmp/a.dbc")
    model.recompute({path: frozenset({0x100, 0x200})})
    bus_item, _ = model.add_bus("CAN_1")
    assert bus_item is not None
    # Remove the bus row: every descendant key is dropped from the dedup table.
    model.remove_at(bus_item.index())
    assert model.rowCount() == 0
    assert not model.entries()
    eff = model.effective()
    assert not eff.admits("CAN_1", 0x100)


def test_serialize_round_trips_through_deserialize(qapp) -> None:  # type: ignore[no-untyped-def]
    """A round-trip of serialize → deserialize preserves the tree."""
    parent = QWidget()
    db = Database(parent)
    pane = TrackedTreePane(db, parent)
    pane.model.add_bus("CAN_1")
    pane.model.add_message(
        bus_name="CAN_2",
        dbc_path=Path("/tmp/a.dbc"),
        message_id=0x100,
        message_name="EngineCommand",
    )
    serialized = pane.serialize()

    parent2 = QWidget()
    db2 = Database(parent2)
    pane2 = TrackedTreePane(db2, parent2)
    pane2.deserialize(serialized)
    assert pane2.serialize() == serialized
