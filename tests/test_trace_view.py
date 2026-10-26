# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.trace_view.TraceTreeModel`` — admission, modes, filter and cost."""

from __future__ import annotations

import gc
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QTreeView

from ithildin.core.decoder import Decoder
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord
from ithildin.ui.trace_view import ERROR_ROW_ID, Mode, TraceTreeModel


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
    counters: list[tuple[int, int, int]] = []
    model.counters_changed.connect(lambda *args: counters.append(args))

    model.clear()
    assert model.rowCount() == 0
    assert model.rx_count() == 0
    assert counters == [(0, 0, 0)]
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
    counters: list[tuple[int, int, int]] = []
    model.counters_changed.connect(lambda *args: counters.append(args))

    model.drain_now()
    assert counters == [(3, 251, 0)]
    # Recounted from the rows that stay when the subscription narrows.
    model.set_admit(lambda _b, _a, *, is_error=False: is_error)
    assert counters[-1] == (2, 251, 0)


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


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_error_frames_never_share_a_messages_key(qapp, mode: Mode) -> None:  # type: ignore[no-untyped-def]
    """An error class can equal a message ID on the same bus (PCAN's 0x1-0x8,
    SocketCAN's 0x100): the message keeps its own row, period and pin."""
    ring = RingBuffer(16)
    first, second = _frame("A", 0x100, ts=0.0), _frame("A", 0x100, ts=0.1)
    ring.append(first)
    ring.append(_error_frame("A", 0x100, ts=0.05))
    ring.append(second)
    ring.append(_error_frame("A", 0x100, ts=0.12))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.set_mode(mode)
    model.drain_now()

    assert model.latest_frame_for("A", 0x100) is second
    assert model.period_for("A", 0x100) == pytest.approx(0.1)
    latest_error = model.latest_frame_for("A", ERROR_ROW_ID)
    assert latest_error is not None and latest_error.timestamp == 0.12
    assert model.period_for("A", ERROR_ROW_ID) == pytest.approx(0.07)
    if mode == "overwrite":
        assert model.rowCount() == 2
        assert model.data(model.index(0, TraceTreeModel.COL_NAME)) == "Error frame"
        assert model.frame_at(1) is second


def test_latest_row_children_follow_the_replacing_frame(qtbot, edge_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A multiplexed message's next frame may carry another branch's signals."""
    ring = RingBuffer(8)
    ring.append(_frame("A", 1601, data=bytes([225]) + bytes(7)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(edge_dbc_path)}, admit=_admit_all
    )
    model.set_mode("overwrite")
    model.drain_now()
    assert model.rowCount(model.index(0, 0)) == 5

    ring.append(_frame("A", 1601, data=bytes([128]) + bytes(7)))
    with qtbot.waitSignal(model.rowsRemoved, timeout=1000) as removed:
        model.drain_now()
    assert removed.args[1:] == [2, 4]
    assert model.rowCount(model.index(0, 0)) == 2

    ring.append(_frame("A", 1601, data=bytes([225]) + bytes(7)))
    with qtbot.waitSignal(model.rowsInserted, timeout=1000) as inserted:
        model.drain_now()
    assert inserted.args[1:] == [2, 4]
    assert model.rowCount(model.index(0, 0)) == 5


# ---- Live window, history and decode cost ----


def _shown_view(qtbot, model: TraceTreeModel) -> QTreeView:  # type: ignore[no-untyped-def]
    view = QTreeView()
    qtbot.addWidget(view)
    view.setUniformRowHeights(True)
    view.setModel(model)
    view.resize(800, 400)
    with qtbot.waitExposed(view):
        view.show()
    return view


def _lay_out(view: QTreeView) -> None:
    # A synchronous paint runs the view's pending item layout first.
    view.viewport().repaint()


def test_stream_running_exposes_only_live_tail(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(4096)
    for i in range(3000):
        ring.append(_frame("A", 0x100, ts=float(i)))
    model = TraceTreeModel({"A": ring}, admit=_admit_all, max_rows=2000, live_rows=100)
    model.drain_now()
    assert model.rowCount() == 100
    assert model.frame_at(0).timestamp == 2999.0  # type: ignore[union-attr]

    model.set_paused(True)
    assert model.rowCount() == 2000
    assert model.frame_at(0).timestamp == 2999.0  # type: ignore[union-attr]
    assert model.frame_at(1999).timestamp == 1000.0  # type: ignore[union-attr]

    model.set_paused(False)
    assert model.rowCount() == 100
    assert model.frame_at(99).timestamp == 2900.0  # type: ignore[union-attr]


def test_drain_layout_cost_independent_of_history(qtbot) -> None:  # type: ignore[no-untyped-def]
    class CountingModel(TraceTreeModel):
        calls = 0

        def index(self, row, column, parent=QModelIndex()):  # type: ignore[no-untyped-def]  # noqa: B008
            self.calls += 1
            return super().index(row, column, parent)

        def hasChildren(self, parent=QModelIndex()):  # type: ignore[no-untyped-def]  # noqa: B008
            self.calls += 1
            return super().hasChildren(parent)

    def drain_calls(history: int) -> int:
        ring = RingBuffer(32768)
        for i in range(history):
            ring.append(_frame("A", 0x100 + i % 8, ts=float(i)))
        model = CountingModel({"A": ring}, admit=_admit_all, live_rows=200)
        view = _shown_view(qtbot, model)
        model.drain_now()
        _lay_out(view)

        model.calls = 0
        for i in range(5):
            ring.append(_frame("A", 0x100, ts=history + float(i)))
        model.drain_now()
        _lay_out(view)
        assert model.rowCount() == 200
        return model.calls

    # Two depths, not a fixed bound: the count includes the paint's, which grows
    # with the rows a platform's font fits on screen. Laying out every stored
    # row would differ by ~36 000.
    assert abs(drain_calls(20_000) - drain_calls(2_000)) < 50


def test_collapsed_dbc_rows_are_not_decoded(qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(64)
    for i in range(50):
        ring.append(_frame("A", 0x100, ts=float(i), data=bytes(8)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    view = _shown_view(qtbot, model)
    model.drain_now()
    _lay_out(view)
    frames, _ = ring.read_since(-1)
    assert all(f.decoded is None for f in frames)
    assert model.hasChildren(model.index(0, 0))

    view.expand(model.index(0, 0))
    _lay_out(view)
    assert frames[-1].decoded is not None
    assert model.rowCount(model.index(0, 0)) == 2
    assert all(f.decoded is None for f in frames[:-1])


def test_decoded_row_stays_expandable_after_its_dbc_unbinds(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100, data=bytes(8)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    model.drain_now()
    top = model.index(0, 0)
    assert model.rowCount(top) == 2

    model.set_decoders({})
    assert model.hasChildren(top)
    assert model.rowCount(top) == 2


def test_expanded_latest_row_outlives_its_freed_frames(qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The view keeps the signal-row indexes it laid out across frame replacements."""
    ring = RingBuffer(4)
    ring.append(_frame("A", 0x100, data=bytes(8)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    model.set_mode("overwrite")
    view = _shown_view(qtbot, model)
    model.drain_now()
    view.expand(model.index(0, 0))
    _lay_out(view)
    laid_out = view.indexAt(view.visualRect(model.index(0, 0, model.index(0, 0))).center())

    # Every frame the row has shown is dropped by the ring and freed.
    for i in range(1, 41):
        ring.append(_frame("A", 0x100, ts=float(i), data=bytes([i]) + bytes(7)))
        model.drain_now()
        gc.collect()
        _lay_out(view)
    assert model.data(laid_out) == "    • EngineSpeed = 5 rpm"
    assert model.parent(laid_out) == model.index(0, 0)


def _flood_frame(i: int) -> FrameRecord:
    """Three sampled error frames to one data frame; all carry a DBC message's ID."""
    if i % 4:
        return _error_frame("A", 0x100, ts=i / 4000, coalesced=400)
    return _frame("A", 0x100, ts=i / 4000, data=bytes(8))


def test_flood_on_full_history_keeps_drains_fast(qtbot, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """100 k rows of history, then a 4 k frames/s flood, each drain laid out in a shown view."""
    ring = RingBuffer(1 << 17)
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    for i in range(100_000):
        ring.append(_flood_frame(i))
    view = _shown_view(qtbot, model)
    model.drain_now()
    _lay_out(view)

    seq = 100_000
    worst = 0.0
    for _ in range(30):
        for _ in range(140):
            ring.append(_flood_frame(seq))
            seq += 1
        start = time.perf_counter()
        model.drain_now()
        _lay_out(view)
        worst = max(worst, time.perf_counter() - start)

    assert worst < 0.25
    assert len(model._rows) == 100_000
    assert model.rowCount() == 1000
    frames, _ = ring.read_since(-1)
    assert all(f.decoded is None for f in frames)


def test_overwrite_decodes_only_previously_decoded_keys(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(16)
    ring.append(_frame("A", 0x100, data=bytes(8)))
    ring.append(_frame("A", 0x200, data=bytes(4)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    model.set_mode("overwrite")
    model.drain_now()
    assert all(f.decoded is None for f in ring.read_since(-1)[0])
    # The view expands the 0x100 row only.
    assert model.rowCount(model.index(0, 0)) == 2

    engine = _frame("A", 0x100, data=bytes(8))
    body = _frame("A", 0x200, data=bytes(4))
    ring.append(engine)
    ring.append(body)
    model.drain_now()
    assert engine.decoded is not None
    assert body.decoded is None


def test_overwrite_touches_each_row_once_per_drain(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(64)
    ring.append(_frame("A", 0x100, ts=0.0))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.set_mode("overwrite")
    model.drain_now()
    changed: list[int] = []
    model.dataChanged.connect(lambda top, _bot, *_: changed.append(top.row()))

    for i in range(1, 11):
        ring.append(_frame("A", 0x100, ts=i / 100, data=bytes([i])))
    model.drain_now()
    assert changed == [0]
    assert model.data(model.index(0, TraceTreeModel.COL_DATA)) == "0A"
    assert model.period_for("A", 0x100) == pytest.approx(0.01)


# ---- Text filter ----


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_text_filter_is_non_destructive(qapp, mode: Mode) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    for aid in (0x100, 0x200, 0x300):
        ring.append(_frame("A", aid))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.set_mode(mode)
    model.drain_now()

    model.set_text_filter("2", False)
    assert model.rowCount() == 1
    assert model.data(model.index(0, TraceTreeModel.COL_ID)) == "200"
    # Frames arriving under the filter obey it too.
    ring.append(_frame("A", 0x201))
    ring.append(_frame("A", 0x400))
    model.drain_now()
    assert model.rowCount() == 2

    model.set_text_filter("", False)
    assert model.rowCount() == 5


def test_text_filter_case_rules_on_message_name(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100, ts=1.0, data=bytes(8)))
    ring.append(_frame("A", 0x200, ts=2.0, data=bytes(4)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    model.drain_now()

    model.set_text_filter("engine", False)
    assert model.rowCount() == 1
    assert model.data(model.index(0, TraceTreeModel.COL_NAME)) == "EngineCommand"
    model.set_text_filter("engine", True)
    assert model.rowCount() == 0
    model.set_text_filter("Engine", True)
    assert model.rowCount() == 1
    # A filtered row still expands into its signals.
    assert model.rowCount(model.index(0, 0)) == 2


def test_text_filter_matches_a_literal_id_name_or_bus_substring(qapp) -> None:  # type: ignore[no-untyped-def]
    """Parity with the proxy it replaced: a literal match on the ID, Name or Bus cell."""
    ring_a = RingBuffer(8)
    ring_b = RingBuffer(8)
    ring_a.append(_frame("CAN_1", 0x100, ts=1.0))
    ring_a.append(
        FrameRecord(
            bus_name="CAN_1",
            timestamp=2.0,
            arbitration_id=0x18FF0001,
            data=b"",
            is_extended_id=True,
        )
    )
    ring_b.append(_error_frame("CAN_2", 0x4, ts=3.0, coalesced=37))
    model = TraceTreeModel({"CAN_1": ring_a, "CAN_2": ring_b}, admit=_admit_all)
    model.drain_now()

    def shown(text: str) -> set[str]:
        model.set_text_filter(text, False)
        return {model.data(model.index(r, TraceTreeModel.COL_ID)) for r in range(model.rowCount())}

    assert shown("1.0") == set()
    assert shown("ff00") == {"18FF0001"}
    assert shown("can_2") == {"004"}
    assert shown("×37") == {"004"}
    assert shown("error frame") == {"004"}
    assert shown("can_1") == {"100", "18FF0001"}
    assert shown("") == {"100", "18FF0001", "004"}


def test_text_filter_runs_over_the_full_history_while_paused(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(64)
    for i in range(40):
        ring.append(_frame("A", 0x100 + i % 2, ts=float(i)))
    model = TraceTreeModel({"A": ring}, admit=_admit_all, live_rows=5)
    model.drain_now()
    model.set_text_filter("101", False)
    assert model.rowCount() == 5
    assert model.frame_at(4).timestamp == 31.0  # type: ignore[union-attr]

    model.set_paused(True)
    assert model.rowCount() == 20
    model.set_text_filter("", False)
    assert model.rowCount() == 40


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_text_filter_keeps_expanded_and_current_rows(
    qtbot, example_dbc_path: Path, mode: Mode
) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100, ts=1.0, data=bytes(8)))
    ring.append(_frame("A", 0x200, ts=2.0, data=bytes(4)))
    model = TraceTreeModel(
        {"A": ring}, decoders={"A": Decoder.from_files(example_dbc_path)}, admit=_admit_all
    )
    model.set_mode(mode)
    view = _shown_view(qtbot, model)
    model.drain_now()
    engine = model.index(1 if mode == "append" else 0, 0)
    view.expand(engine)
    view.setCurrentIndex(model.index(1, 0, engine))
    _lay_out(view)

    model.set_text_filter("engine", False)
    _lay_out(view)
    top = model.index(0, 0)
    assert model.frame_at(top).arbitration_id == 0x100  # type: ignore[union-attr]
    assert view.isExpanded(top)
    assert view.currentIndex() == model.index(1, 0, top)

    model.set_text_filter("body", False)
    _lay_out(view)
    assert not view.currentIndex().isValid()


# ---- Lost frames and pause ----


def test_ring_overrun_counted_as_lost(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(16)
    ring.append(_frame("A", 0x100))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    counters: list[tuple[int, int, int]] = []
    model.counters_changed.connect(lambda *args: counters.append(args))
    model.drain_now()

    for _ in range(40):
        ring.append(_frame("A", 0x100))
    model.drain_now()
    assert counters[-1] == (17, 0, 24)


def test_lost_reported_even_when_nothing_admitted(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(16)
    ring.append(_frame("A", 0x100))
    model = TraceTreeModel({"A": ring}, admit=lambda _b, a, **_: a == 0x100)
    counters: list[tuple[int, int, int]] = []
    model.counters_changed.connect(lambda *args: counters.append(args))
    model.drain_now()

    for _ in range(40):
        ring.append(_frame("A", 0x200))
    model.drain_now()
    assert counters[-1] == (1, 0, 24)
    model.drain_now()
    assert len(counters) == 2


def test_fresh_model_on_wrapped_ring_reports_no_loss(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(16)
    for _ in range(40):
        ring.append(_frame("A", 0x100))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    counters: list[tuple[int, int, int]] = []
    model.counters_changed.connect(lambda *args: counters.append(args))
    model.drain_now()
    assert counters == [(16, 0, 0)]
    assert model.rowCount() == 16


def test_resume_skips_frames_from_pause(qapp) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(64)
    ring.append(_frame("A", 0x100, ts=1.0))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    counters: list[tuple[int, int, int]] = []
    model.counters_changed.connect(lambda *args: counters.append(args))
    model.drain_now()

    model.set_paused(True)
    assert not model._timer.isActive()
    for i in range(10):
        ring.append(_frame("A", 0x200, ts=2.0 + i))
    model.set_paused(False)
    assert model._timer.isActive()
    model.stop()
    model.drain_now()
    assert model.rowCount() == 1
    assert counters[-1] == (1, 0, 0)

    ring.append(_frame("A", 0x300, ts=20.0))
    model.drain_now()
    assert model.rowCount() == 2
    assert model.data(model.index(0, TraceTreeModel.COL_ID)) == "300"


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_resume_does_not_measure_the_pause_as_a_period(qapp, mode: Mode) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(64)
    ring.append(_frame("A", 0x100, ts=0.0))
    ring.append(_frame("A", 0x100, ts=0.5))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.set_mode(mode)
    model.drain_now()
    assert model.period_for("A", 0x100) == pytest.approx(0.5)

    model.set_paused(True)
    ring.append(_frame("A", 0x100, ts=30.0))
    model.set_paused(False)
    model.stop()
    ring.append(_frame("A", 0x100, ts=60.5))
    model.drain_now()
    assert model.period_for("A", 0x100) is None
    ring.append(_frame("A", 0x100, ts=61.0))
    model.drain_now()
    assert model.period_for("A", 0x100) == pytest.approx(0.5)


def test_unpausing_a_running_model_keeps_the_ring_history(qapp) -> None:  # type: ignore[no-untyped-def]
    """A restored Running state must not skip what the rings already hold."""
    ring = RingBuffer(8)
    ring.append(_frame("A", 0x100))
    model = TraceTreeModel({"A": ring}, admit=_admit_all)
    model.set_paused(False)
    model.drain_now()
    assert model.rowCount() == 1


def test_backing_eviction_keeps_view_consistent(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    ring = RingBuffer(1024)
    model = TraceTreeModel(
        {"A": ring},
        decoders={"A": Decoder.from_files(example_dbc_path)},
        admit=_admit_all,
        max_rows=50,
        live_rows=10,
    )
    model.set_text_filter("100", False)

    def check() -> None:
        matching = [f for f in model._rows if f.arbitration_id == 0x100]
        if not model._paused:
            matching = matching[-10:]
        shown = [model.frame_at(r) for r in range(model.rowCount())]
        assert shown == matching[::-1]
        for r in range(model.rowCount()):
            top = model.index(r, 0)
            child = model.index(1, 0, top)
            assert child.isValid()
            assert model.parent(child) == top

    seq = 0
    for batch in (3, 7, 1, 40, 13, 60, 2, 9, 120, 5):
        for _ in range(batch):
            aid = 0x100 if seq % 3 else 0x200
            ring.append(_frame("A", aid, ts=float(seq), data=bytes(8)))
            seq += 1
        model.drain_now()
        check()
        if batch == 13:
            model.set_paused(True)
            check()
            model.set_paused(False)
            model.stop()
            check()
    assert len(model._rows) == 50
