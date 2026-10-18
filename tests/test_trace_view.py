# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.trace_view.TraceTreeModel`` — admission and modes."""

from __future__ import annotations

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
