# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.trace_view.TraceTreeModel`` — admission and modes."""

from __future__ import annotations

from PySide6.QtCore import Qt

from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord
from ithildin.ui.trace_view import TraceTreeModel


def _frame(bus: str, aid: int, ts: float = 0.0, data: bytes = b"") -> FrameRecord:
    return FrameRecord(bus_name=bus, timestamp=ts, arbitration_id=aid, data=data)


def test_admit_predicate_filters_frames(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200, 0x123):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring}, admit=lambda b, a: a == 0x200)
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
    model = TraceTreeModel({"A": ring_a, "B": ring_b}, admit=lambda _b, _a: True)
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
    model = TraceTreeModel({"A": ring}, admit=lambda _b, _a: True)
    model.drain_now()
    assert model.rowCount() == 3
    model.set_admit(lambda _b, a: a == 0x200)
    assert model.rowCount() == 1
    assert model.data(model.index(0, TraceTreeModel.COL_ID)) == "200"


def test_overwrite_set_admit_drops_stale_keys(qapp) -> None:  # type: ignore[no-untyped-def]
    ring_a = RingBuffer(8)
    ring_b = RingBuffer(8)
    ring_a.append(_frame("A", 0x100))
    ring_b.append(_frame("B", 0x100))
    model = TraceTreeModel({"A": ring_a, "B": ring_b}, admit=lambda _b, _a: True)
    model.set_mode("overwrite")
    model.drain_now()
    assert model.rowCount() == 2
    # Now restrict to bus A only.
    model.set_admit(lambda b, _a: b == "A")
    assert model.rowCount() == 1
    assert model.data(model.index(0, TraceTreeModel.COL_BUS)) == "A"


def test_append_orders_newest_first(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100, ts=1.0))
    ring.append(_frame("A", 0x200, ts=2.0))
    ring.append(_frame("A", 0x300, ts=3.0))
    model = TraceTreeModel({"A": ring}, admit=lambda _b, _a: True)
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
    model = TraceTreeModel({"A": old}, admit=lambda _b, _a: True)
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
    model = TraceTreeModel({"A": ring}, admit=lambda _b, _a: True)
    model.drain_now()

    model.set_rings({})
    model.drain_now()
    assert model.rowCount() == 3


def test_clear_resets_rows_and_counters(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200, 0x300):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring}, admit=lambda _b, _a: True)
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
    model = TraceTreeModel({"A": ring_a, "B": ring_b}, admit=lambda _b, _a: True)
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
    model = TraceTreeModel({"A": ring}, admit=lambda _b, _a: True)
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
