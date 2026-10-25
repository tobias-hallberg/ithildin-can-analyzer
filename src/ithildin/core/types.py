# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Shared core types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BusHealth(StrEnum):
    """A bus's state as its capture worker sees it. Degraded iff not ``OK``."""

    OK = "ok"
    ERRORS = "errors"
    BUS_OFF = "bus_off"
    LOST = "lost"

    @property
    def label(self) -> str:
        return _HEALTH_LABELS[self]

    def describe(self, detail: str) -> str:
        """The label with its detail, e.g. "bus errors (error passive)"."""
        return f"{self.label} ({detail})" if detail else self.label


_HEALTH_LABELS: dict[BusHealth, str] = {
    BusHealth.OK: "live",
    BusHealth.ERRORS: "bus errors",
    BusHealth.BUS_OFF: "bus-off",
    BusHealth.LOST: "adapter lost",
}

HEALTH_RANK: dict[BusHealth, int] = {
    BusHealth.OK: 0,
    BusHealth.ERRORS: 1,
    BusHealth.BUS_OFF: 2,
    BusHealth.LOST: 3,
}


@dataclass
class FrameRecord:
    """One captured CAN frame plus optional decode cache.

    ``seq`` is filled by ``RingBuffer.append``. ``decoded`` is populated lazily
    by consumers (trace view, MCP) the first time they need a decoded view —
    keeps the bus thread doing as little as possible.
    """

    bus_name: str
    timestamp: float
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False
    is_fd: bool = False
    is_error_frame: bool = False
    is_remote_frame: bool = False
    is_rx: bool = True
    """``False`` for the app's own transmits echoed back via
    ``receive_own_messages``; ``True`` for frames observed on the bus."""
    coalesced: int = 1
    """For a sampled error frame, how many error frames it stands for: itself
    plus those the worker dropped since the previous sample."""
    seq: int = 0
    decoded: dict[str, Any] | None = field(default=None, compare=False)
