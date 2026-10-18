# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Bounded ring buffer with drop-oldest semantics and per-consumer cursors.

Single producer (the bus capture thread), multiple consumers (trace view,
logger, MCP server). Each consumer remembers the last sequence number it
read; a consumer that falls behind by more than the buffer capacity sees
its missed frames counted as ``dropped`` so it can surface that to the user.
"""

from __future__ import annotations

import threading

from ithildin.core.types import FrameRecord


class RingBuffer:
    """Thread-safe fixed-capacity ring of ``FrameRecord``."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        self._buf: list[FrameRecord | None] = [None] * capacity
        self._next_seq = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_seq(self) -> int:
        with self._lock:
            return self._next_seq

    def append(self, frame: FrameRecord) -> int:
        """Append one frame, overwriting the oldest if full. Returns its seq."""
        with self._lock:
            seq = self._next_seq
            frame.seq = seq
            self._buf[seq % self._capacity] = frame
            self._next_seq += 1
            return seq

    def read_since(self, last_seq: int) -> tuple[list[FrameRecord], int]:
        """Return frames with seq > last_seq plus a count of frames missed.

        ``dropped`` is the number of frames that existed between ``last_seq``
        and the oldest still-resident frame — i.e. frames the consumer was
        too slow to see before they were overwritten.
        """
        with self._lock:
            current = self._next_seq
            if current == 0:
                return [], 0

            oldest_seq = max(0, current - self._capacity)
            want_from = last_seq + 1
            start = max(want_from, oldest_seq)
            dropped = max(0, oldest_seq - want_from)

            out: list[FrameRecord] = []
            for s in range(start, current):
                rec = self._buf[s % self._capacity]
                if rec is not None:
                    out.append(rec)
            return out, dropped
