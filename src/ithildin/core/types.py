# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Shared core types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    seq: int = 0
    decoded: dict[str, Any] | None = field(default=None, compare=False)
