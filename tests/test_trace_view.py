# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.trace_view.TraceTreeModel`` — admission and modes."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from ithildin.core.decoder import Decoder
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord
from ithildin.ui.trace_view import TraceTreeModel


def _frame(bus: str, aid: int, ts: float = 0.0, data: bytes = b"") -> FrameRecord:
    return FrameRecord(bus_name=bus, timestamp=ts, arbitration_id=aid, data=data)


def _error_frame(bus: str, aid: int, ts: float = 0.0, coalesced: int = 1) -> FrameRecord:
    return FrameRecord(
        bus_name=bus,
        timestamp=ts,
        arbitration_id=aid,
        data=bytes(8),
        is_error_frame=True,
        coalesced=coalesced,
    )


def _admit_all(_bus: str, _aid: int, *, is_error: bool = False) -> bool:
    return True


def test_admit_predicate_filters_frames(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200, 0x123):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring}, admit=lambda _b, a, **_: a == 0x200)
    model.drain_now()
    assert model.rowCount() == 1
    idx = model.index(0, TraceTreeModel.COL_ID)
    assert model.data(idx) == "200"


def test_no_subscription_admits_nothing(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring})  # default admit = reject all
    model.drain_now()
    assert model.rowCount() == 0


def test_overwrite_keys_on_bus_and_id(qapp) -> None:  # type: ignore[no-untyped-def]
    """Two buses with the same arbitration ID produce two distinct rows."""
    ring_a = RingBuffer(8)
    ring_b = RingBuffer(8)
    ring_a.append(_frame("A", 0x100, ts=1.0))
    ring_b.append(_frame("B", 0x100, ts=2.0))
    model = TraceTreeModel({"A": ring_a, "B": ring_b}, admit=_admit_all)
    model.set_mode("overwrite")
    model.drain_now()
    assert model.rowCount() == 2
    bus_col_values = [
        model.data(model.index(r, TraceTreeModel.COL_BUS)) for r in range(model.rowCount())
    ]
    assert set(bus_col_values) == {"A", "B"}


def test_set_admit_evicts_now_disallowed_rows(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200, 0x300):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.drain_now()
    assert model.rowCount() == 3
    model.set_admit(lambda _b, a, **_: a == 0x200)
    assert model.rowCount() == 1
    assert model.data(model.index(0, TraceTreeModel.COL_ID)) == "200"


def test_overwrite_set_admit_drops_stale_keys(qapp) -> None:  # type: ignore[no-untyped-def]
    ring_a = RingBuffer(8)
    ring_b = RingBuffer(8)
    ring_a.append(_frame("A", 0x100))
    ring_b.append(_frame("B", 0x100))
    model = TraceTreeModel({"A": ring_a, "B": ring_b}, admit=_admit_all)
    model.set_mode("overwrite")
    model.drain_now()
    assert model.rowCount() == 2
    # Now restrict to bus A only.
    model.set_admit(lambda b, _a, **_: b == "A")
    assert model.rowCount() == 1
    assert model.data(model.index(0, TraceTreeModel.COL_BUS)) == "A"


def test_append_orders_newest_first(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100, ts=1.0))
    ring.append(_frame("A", 0x200, ts=2.0))
    ring.append(_frame("A", 0x300, ts=3.0))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.drain_now()
    # Row 0 = newest (ts=3.0, aid=0x300).
    assert model.data(model.index(0, TraceTreeModel.COL_ID)) == "300"
    assert model.data(model.index(2, TraceTreeModel.COL_ID)) == "100"


def test_model_is_not_retained_by_the_theme(qapp) -> None:  # type: ignore[no-untyped-def]
    """A replaced model must be freed, with its rows and the rings it holds."""
    import gc
    import weakref

    model = TraceTreeModel({"A": RingBuffer(8)})
    ref = weakref.ref(model)
    del model
    gc.collect()
    assert ref() is None


# ---- Continuity across bus topology changes ----


def test_set_rings_resets_cursor_for_replacement_ring(qapp) -> None:  # type: ignore[no-untyped-def]
    old = RingBuffer(8)
    for i in range(5):
        old.append(_frame("A", 0x100 + i, ts=float(i)))
    model = TraceTreeModel({"A": old}, admit=_admit_all)
    model.drain_now()
    assert model.rowCount() == 5

    # A reconnect: same name, new ring, seq back at 0.
    new = RingBuffer(8)
    for i in range(2):
        new.append(_frame("A", 0x200 + i, ts=10.0 + i))
    model.set_rings({"A": new})
    model.drain_now()
    assert model.rowCount() == 7

    model.set_rings({"A": new})
    model.drain_now()
    assert model.rowCount() == 7


def test_set_rings_keeps_rows_of_removed_bus(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200, 0x300):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.drain_now()

    model.set_rings({})
    model.drain_now()
    assert model.rowCount() == 3


def test_clear_resets_rows_and_counters(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200, 0x300):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.drain_now()
    counters: list[tuple[int, int]] = []
    model.counters_changed.connect(lambda rx, err: counters.append((rx, err)))

    model.clear()
    assert model.rowCount() == 0
    assert model.rx_count() == 0
    assert counters == [(0, 0)]
    # Cursors are kept: the ring is not replayed.
    model.drain_now()
    assert model.rowCount() == 0
    ring.append(_frame("A", 0x400))
    model.drain_now()
    assert model.rowCount() == 1


def test_closed_bus_rows_render_dim(qapp) -> None:  # type: ignore[no-untyped-def]
    ring_a = RingBuffer(8)
    ring_b = RingBuffer(8)
    ring_a.append(_frame("A", 0x100, ts=1.0))
    ring_b.append(_frame("B", 0x200, ts=2.0))
    model = TraceTreeModel({"A": ring_a, "B": ring_b}, admit=_admit_all)
    model.drain_now()
    model.set_rings({"A": ring_a})

    fg = Qt.ItemDataRole.ForegroundRole
    # Row 0 is the newest: bus B's, now closed.
    assert model.data(model.index(0, TraceTreeModel.COL_ID), fg) == model._dim_color
    assert model.data(model.index(0, TraceTreeModel.COL_DIR), fg) == model._dim_color
    assert model.data(model.index(1, TraceTreeModel.COL_ID), fg) is None
    assert model.data(model.index(1, TraceTreeModel.COL_DIR), fg) == model._rx_color


def test_latest_mode_marks_quiet_ids_stale(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100))
    ring.append(_frame("A", 0x200))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.set_mode("overwrite")
    model.drain_now()
    fg = Qt.ItemDataRole.ForegroundRole

    model._arrival[("A", 0x100)] -= 10
    model._sweep_stale()
    # Overwrite rows sort by ID.
    assert model.data(model.index(0, TraceTreeModel.COL_ID), fg) == model._dim_color
    assert model.data(model.index(1, TraceTreeModel.COL_ID), fg) is None

    ring.append(_frame("A", 0x100))
    model.drain_now()
    assert model.data(model.index(0, TraceTreeModel.COL_ID), fg) is None


# ---- Error frames ----


def test_error_frame_not_decoded_or_expandable(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """An error frame whose ID is a DBC message's still reads as an error frame."""
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100, ts=1.0, data=bytes(8)))
    err = _error_frame("A", 0x100, ts=2.0)
    ring.append(err)
    decoder = Decoder.from_files(example_dbc_path)
    model = TraceTreeModel({"A": ring}, decoders={"A": decoder}, admit=_admit_all)
    model.drain_now()

    err_idx = model.index(0, 0)
    assert model.frame_at(err_idx) is err
    assert not model.hasChildren(err_idx)
    assert model.rowCount(err_idx) == 0
    assert not model.index(0, 0, err_idx).isValid()
    assert model.data(model.index(0, TraceTreeModel.COL_NAME)) == "Error frame"
    assert err.decoded is None
    # The data frame with the same ID still expands into its signals.
    assert model.hasChildren(model.index(1, 0))
    assert model.data(model.index(1, TraceTreeModel.COL_NAME)) == "EngineCommand"


def test_error_frame_name_tinted_in_err_colour(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_error_frame("A", 0x4, coalesced=37))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.drain_now()

    fg = Qt.ItemDataRole.ForegroundRole
    assert model.data(model.index(0, TraceTreeModel.COL_NAME)) == "Error frame ×37"
    assert model.data(model.index(0, TraceTreeModel.COL_NAME), fg) == model._err_color
    assert model.data(model.index(0, TraceTreeModel.COL_ID), fg) is None
    # A closed bus's row is dim throughout, its error tint included.
    model.set_rings({})
    assert model.data(model.index(0, TraceTreeModel.COL_NAME), fg) == model._dim_color


def test_err_counter_sums_coalesced(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_error_frame("A", 0x4, ts=1.0))
    ring.append(_frame("A", 0x100, ts=2.0))
    ring.append(_error_frame("A", 0x4, ts=3.0, coalesced=250))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    counters: list[tuple[int, int]] = []
    model.counters_changed.connect(lambda rx, err: counters.append((rx, err)))

    model.drain_now()
    assert counters == [(3, 251)]
    # Recounted from the rows that stay when the subscription narrows.
    model.set_admit(lambda _b, _a, *, is_error=False: is_error)
    assert counters[-1] == (2, 251)


def test_latest_mode_does_not_decode_error_frames(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    err = _error_frame("A", 0x100)
    ring.append(err)
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    model.set_mode("overwrite")
    model.drain_now()

    assert model.rowCount() == 1
    assert err.decoded is None
    assert not model.hasChildren(model.index(0, 0))


def test_latest_set_admit_drops_error_rows_by_their_own_admission(qapp) -> None:  # type: ignore[no-untyped-def]
    """A narrowed subscription that admits an ID's data frames still drops its error row."""
    ring = RingBuffer(8)
    ring.append(_error_frame("A", 0x4))
    data = _frame("A", 0x100)
    ring.append(data)
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.set_mode("overwrite")
    model.drain_now()
    assert model.rowCount() == 2

    def data_frames_only(_b: str, a: int, *, is_error: bool = False) -> bool:
        return not is_error and a in (0x4, 0x100)

    model.set_admit(data_frames_only)
    assert model.rowCount() == 1
    assert model.frame_at(0) is data
    model.set_admit(data_frames_only)
    assert model.rowCount() == 1


def test_latest_row_children_follow_the_replacing_frame(qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100, data=bytes(8)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    model.set_mode("overwrite")
    model.drain_now()
    assert model.rowCount(model.index(0, 0)) == 2

    ring.append(_error_frame("A", 0x100))
    with qtbot.waitSignal(model.rowsRemoved, timeout=1000) as removed:
        model.drain_now()
    assert removed.args[1:] == [0, 1]
    assert model.rowCount(model.index(0, 0)) == 0

    ring.append(_frame("A", 0x100, data=bytes(8)))
    with qtbot.waitSignal(model.rowsInserted, timeout=1000) as inserted:
        model.drain_now()
    assert inserted.args[1:] == [0, 1]
    assert model.rowCount(model.index(0, 0)) == 2
