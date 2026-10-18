# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.ui_events.UiEvents``."""

from __future__ import annotations

from ithildin.core.types import FrameRecord
from ithildin.ui.ui_events import UiEvents


def test_last_selected_frame_caches_emissions(qapp) -> None:  # type: ignore[no-untyped-def]
    events = UiEvents()
    assert events.last_selected_frame is None

    f = FrameRecord(bus_name="A", timestamp=0.0, arbitration_id=0x100, data=b"")
    events.frame_selected.emit(f)
    assert events.last_selected_frame is f


def test_non_frame_emit_is_ignored(qapp) -> None:  # type: ignore[no-untyped-def]
    events = UiEvents()
    events.frame_selected.emit("not a frame")
    assert events.last_selected_frame is None
