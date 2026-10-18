# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.bus.BusController`` against python-can's virtual backend."""

from __future__ import annotations

import uuid

import can
import pytest

from ithildin.core.bus import BusController
from ithildin.core.ringbuf import RingBuffer
from ithildin.workspace.schema import BusConfig


@pytest.fixture
def virtual_channel() -> str:
    return f"ithildin-test-{uuid.uuid4().hex[:8]}"


def test_capture_lifecycle(qapp, qtbot, virtual_channel: str) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name="Test",
        interface="virtual",
        channel=virtual_channel,
        bitrate=500000,
    )
    ring = RingBuffer(64)
    controller = BusController(cfg, ring)

    with qtbot.waitSignal(controller.started, timeout=2000):
        controller.start()

    sender = can.Bus(interface="virtual", channel=virtual_channel)
    try:
        for i in range(5):
            sender.send(
                can.Message(
                    arbitration_id=0x100 + i,
                    data=bytes([i, i, i, i]),
                    is_extended_id=False,
                )
            )
    finally:
        sender.shutdown()

    qtbot.waitUntil(lambda: ring.total_seq >= 5, timeout=2000)
    assert controller.stop(wait_ms=2000)

    frames, _ = ring.read_since(-1)
    assert len(frames) == 5
    assert {f.arbitration_id for f in frames} == {0x100, 0x101, 0x102, 0x103, 0x104}
    assert all(f.bus_name == "Test" for f in frames)


def test_open_failure_emits_error(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name="Bad",
        interface="virtual",
        channel="x",
        bitrate=500000,
    )
    ring = RingBuffer(8)
    controller = BusController(cfg, ring)

    def boom() -> can.BusABC:
        raise RuntimeError("simulated driver failure")

    controller.worker._open_bus = boom  # type: ignore[method-assign]

    with qtbot.waitSignal(controller.error, timeout=2000, raising=True) as blocker:
        controller.start()
    assert "simulated driver failure" in blocker.args[0]
    assert controller.stop(wait_ms=2000)


@pytest.mark.parametrize(
    "interface, expected_kwarg, expected_value",
    [
        ("pcan", "state", can.BusState.PASSIVE),
        ("kvaser", "driver_mode", False),
        ("cantact", "monitor", True),
    ],
)
def test_listen_only_dispatches_backend_kwarg(
    interface: str, expected_kwarg: str, expected_value: object
) -> None:
    """``listen_only=True`` must translate to the backend's native kwarg."""
    from ithildin.core.bus import _apply_listen_only_kwargs

    kwargs: dict[str, object] = {}
    _apply_listen_only_kwargs(kwargs, interface)
    assert kwargs == {expected_kwarg: expected_value}


def test_listen_only_virtual_is_noop() -> None:
    """Virtual backend has no native silent mode — the app-level gate covers it."""
    from ithildin.core.bus import _apply_listen_only_kwargs

    kwargs: dict[str, object] = {}
    _apply_listen_only_kwargs(kwargs, "virtual")
    assert kwargs == {}


def test_ixxat_parses_in_schema() -> None:
    """IXXAT is a supported interface; integer channels are allowed."""
    cfg = BusConfig(
        name="IXXAT_1",
        interface="ixxat",
        channel=0,
        bitrate=500_000,
    )
    assert cfg.interface == "ixxat"
    assert cfg.channel == 0


def test_ixxat_listen_only_is_rejected_at_schema_level() -> None:
    """python-can's IXXAT classic backend has no silent kwarg, so the
    schema rejects the combo rather than letting it degrade silently to
    an ACKing bus."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BusConfig(
            name="IXXAT_1",
            interface="ixxat",
            channel=0,
            bitrate=500_000,
            listen_only=True,
        )
