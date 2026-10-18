# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.ringbuf.RingBuffer``."""

from __future__ import annotations

import threading

import pytest

from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord


def _frame(seq_hint: int) -> FrameRecord:
    return FrameRecord(
        bus_name="test",
        timestamp=float(seq_hint),
        arbitration_id=0x100 + seq_hint,
        data=bytes([seq_hint & 0xFF]),
    )


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RingBuffer(0)


def test_append_and_read_from_empty() -> None:
    rb = RingBuffer(8)
    rb.append(_frame(0))
    rb.append(_frame(1))
    frames, dropped = rb.read_since(-1)
    assert [f.timestamp for f in frames] == [0.0, 1.0]
    assert dropped == 0
    assert frames[0].seq == 0
    assert frames[1].seq == 1


def test_read_since_advances_cursor() -> None:
    rb = RingBuffer(8)
    for i in range(4):
        rb.append(_frame(i))
    frames, _ = rb.read_since(-1)
    assert len(frames) == 4
    last_seq = frames[-1].seq
    rb.append(_frame(99))
    new_frames, dropped = rb.read_since(last_seq)
    assert len(new_frames) == 1
    assert new_frames[0].timestamp == 99.0
    assert dropped == 0


def test_drop_oldest_when_full() -> None:
    rb = RingBuffer(4)
    for i in range(10):
        rb.append(_frame(i))
    # Reading from before-anything-existed: oldest still in ring is seq 6.
    # Seqs 0..5 were dropped before this slow reader caught up.
    frames, dropped = rb.read_since(-1)
    assert [f.timestamp for f in frames] == [6.0, 7.0, 8.0, 9.0]
    assert dropped == 6


def test_multi_consumer_independent_cursors() -> None:
    rb = RingBuffer(8)
    for i in range(5):
        rb.append(_frame(i))
    fast_frames, _ = rb.read_since(-1)
    fast_cursor = fast_frames[-1].seq

    # A slow consumer hasn't read anything yet; it sees the same data.
    slow_frames, _ = rb.read_since(-1)
    assert [f.seq for f in slow_frames] == [f.seq for f in fast_frames]

    # Push more, fast consumer reads incrementally.
    for i in range(5, 8):
        rb.append(_frame(i))
    delta, dropped = rb.read_since(fast_cursor)
    assert [f.timestamp for f in delta] == [5.0, 6.0, 7.0]
    assert dropped == 0


def test_total_seq_monotonic() -> None:
    rb = RingBuffer(2)
    for i in range(7):
        rb.append(_frame(i))
    assert rb.total_seq == 7


def test_concurrent_writers_and_readers_do_not_crash() -> None:
    rb = RingBuffer(64)
    stop = threading.Event()

    def producer() -> None:
        i = 0
        while not stop.is_set():
            rb.append(_frame(i))
            i += 1

    def consumer() -> None:
        cursor = -1
        while not stop.is_set():
            frames, _ = rb.read_since(cursor)
            if frames:
                cursor = frames[-1].seq

    threads = [threading.Thread(target=producer)]
    threads += [threading.Thread(target=consumer) for _ in range(3)]
    for t in threads:
        t.start()
    # Let them race briefly.
    threading.Event().wait(0.1)
    stop.set()
    for t in threads:
        t.join(timeout=2.0)
        assert not t.is_alive()
    assert rb.total_seq > 0
