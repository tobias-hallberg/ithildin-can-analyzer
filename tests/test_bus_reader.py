# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``app_state.bus_reader.BusReader``."""

from __future__ import annotations

import threading
import time
import uuid

import can
import pytest
from PySide6.QtCore import QDeadlineTimer

from ithildin.app_state.bus_reader import BusReader, BusReadOnlyError
from ithildin.core.bus import BusController
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import FrameRecord
from ithildin.workspace.schema import BusConfig
from tests.conftest import kill_capture
from tests.fake_bus import FakeBus, use_fake_bus


def test_open_capture_close(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    channel = f"reader-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(
        name="CAN_1",
        interface="virtual",
        channel=channel,
        bitrate=500000,
    )
    reader = BusReader()

    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)
    assert reader.is_open
    assert reader.bus_name == "CAN_1"

    sender = can.Bus(interface="virtual", channel=channel)
    received_batches: list[list] = []
    reader.frames_received.connect(lambda batch: received_batches.append(batch))
    try:
        for i in range(3):
            sender.send(
                can.Message(
                    arbitration_id=0x100 + i,
                    data=bytes([i]),
                    is_extended_id=False,
                )
            )
    finally:
        sender.shutdown()

    qtbot.waitUntil(lambda: sum(len(b) for b in received_batches) >= 3, timeout=2000)
    reader.close()
    assert not reader.is_open
    assert reader.bus_name is None

    flat = [f for batch in received_batches for f in batch]
    assert {f.arbitration_id for f in flat} == {0x100, 0x101, 0x102}


def test_open_is_additive_across_buses(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """Opening a second bus must not close the first — multi-bus."""
    cfg_a = BusConfig(
        name="A", interface="virtual", channel=f"a-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    cfg_b = BusConfig(
        name="B", interface="virtual", channel=f"b-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    reader = BusReader()

    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_a)
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_b)

    # bus_name is the single-bus convenience and returns None when 2+ are open.
    assert reader.bus_name is None
    assert sorted(reader.bus_names()) == ["A", "B"]
    assert sorted(reader.rings.keys()) == ["A", "B"]

    # Closing one leaves the other running; bus_name returns to the survivor.
    reader.close("A")
    assert reader.bus_names() == ["B"]
    assert reader.bus_name == "B"

    reader.close()  # close all


def test_send_message_requires_target_when_ambiguous(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """``send_message`` raises ValueError when 2+ buses are open and no name given."""
    cfg_a = BusConfig(
        name="A", interface="virtual", channel=f"a-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    cfg_b = BusConfig(
        name="B", interface="virtual", channel=f"b-{uuid.uuid4().hex[:6]}", bitrate=500000
    )
    reader = BusReader()
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_a)
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg_b)

    msg = can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False)
    with pytest.raises(ValueError, match="bus_name"):
        reader.send_message(msg)

    # Naming a target works.
    reader.send_message(msg, "A")
    reader.close()


def test_send_message_raises_on_listen_only_bus(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """A bus opened with ``listen_only=True`` rejects every transmit."""
    channel = f"ro-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(
        name="RO",
        interface="virtual",
        channel=channel,
        bitrate=500000,
        listen_only=True,
    )
    reader = BusReader()
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)

    assert reader.is_listen_only("RO") is True
    assert reader.is_listen_only("does-not-exist") is False

    msg = can.Message(arbitration_id=0x100, data=b"\x00", is_extended_id=False)
    with pytest.raises(BusReadOnlyError, match="listen-only"):
        reader.send_message(msg)
    reader.close()


def test_own_send_is_captured_with_is_rx_false(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """receive_own_messages loops the app's own send back into the Trace, tagged TX."""
    channel = f"own-{uuid.uuid4().hex[:8]}"
    cfg = BusConfig(name="CAN_1", interface="virtual", channel=channel, bitrate=500000)
    reader = BusReader()
    captured: list = []
    reader.frames_received.connect(lambda batch: captured.extend(batch))
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)

    reader.send_message(can.Message(arbitration_id=0x321, data=b"\x01\x02", is_extended_id=False))
    qtbot.waitUntil(lambda: any(f.arbitration_id == 0x321 for f in captured), timeout=2000)
    own = next(f for f in captured if f.arbitration_id == 0x321)
    assert own.is_rx is False  # echoed own-send is tagged TX
    reader.close()


# ---- Lifecycle: identity, teardown, timed-out close ----


@pytest.fixture
def reader():  # type: ignore[no-untyped-def]
    r = BusReader()
    yield r
    assert r.shutdown() == []


def _virtual(name: str = "CAN_1") -> BusConfig:
    return BusConfig(
        name=name, interface="virtual", channel=f"lc-{uuid.uuid4().hex[:8]}", bitrate=500000
    )


def _open(reader: BusReader, qtbot, cfg: BusConfig) -> None:  # type: ignore[no-untyped-def]
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)


def _hung_fake(monkeypatch) -> FakeBus:  # type: ignore[no-untyped-def]
    """A bus whose receive hangs in the vendor call until ``block`` is set."""
    fake = FakeBus()
    fake.block = threading.Event()
    return use_fake_bus(monkeypatch, fake)


def _send(channel: str, arbitration_id: int, count: int = 1) -> None:
    peer = can.Bus(interface="virtual", channel=channel)
    try:
        for i in range(count):
            peer.send(can.Message(arbitration_id=arbitration_id, data=bytes([i])))
    finally:
        peer.shutdown()


def test_late_stopped_ignored_after_immediate_reopen(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    """The old worker's queued ``stopped`` lands after the reopen; it must not
    take the new bus down."""
    cfg = _virtual()
    _open(reader, qtbot, cfg)
    old = reader._controllers["CAN_1"]

    with qtbot.waitSignals([reader.bus_started, old.finished], timeout=2000):
        reader.close("CAN_1")
        reader.open(cfg)
    assert reader.bus_names() == ["CAN_1"]

    received: list[int] = []
    reader.frames_received.connect(lambda batch: received.extend(f.arbitration_id for f in batch))
    _send(str(cfg.channel), 0x123)
    qtbot.waitUntil(lambda: 0x123 in received, timeout=2000)


def test_signals_from_a_closed_controller_are_ignored(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    cfg = _virtual()
    _open(reader, qtbot, cfg)
    old = reader._controllers["CAN_1"]
    reader.close("CAN_1")
    reader.open(cfg)
    emitted: list[object] = []
    for signal in (reader.bus_started, reader.bus_health_changed, reader.cyclic_failed):
        signal.connect(lambda *args: emitted.append(args))

    old.started.emit()
    old.health.emit("bus_off", "")
    old.cyclic_failed.emit(0x100, "send failed")
    old.stopped.emit()
    assert emitted == []
    assert reader.health("CAN_1") is None
    assert reader.bus_names() == ["CAN_1"]
    qtbot.waitUntil(lambda: reader.health("CAN_1") is not None, timeout=2000)


def test_close_delivers_ring_tail(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    cfg = _virtual()
    _open(reader, qtbot, cfg)
    received: list[int] = []
    reader.frames_received.connect(lambda batch: received.extend(f.arbitration_id for f in batch))
    ring = reader.rings["CAN_1"]

    _send(str(cfg.channel), 0x200, count=5)
    # No event processing: the fan-out timer never runs before close().
    deadline = time.monotonic() + 2.0
    while ring.total_seq < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    reader.close()
    assert received == [0x200] * 5


def test_failure_delivers_tail_before_bus_failed(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    for i in range(3):
        fake.rx.put(can.Message(arbitration_id=0x300 + i, data=b"", is_extended_id=False))
    fake.rx.put(can.CanOperationError("adapter gone"))
    events: list[tuple[str, object]] = []
    reader.frames_received.connect(
        lambda batch: events.extend(("frame", f.arbitration_id) for f in batch)
    )
    reader.bus_failed.connect(lambda name, _msg: events.append(("failed", name)))
    reader.bus_stopped.connect(lambda name: events.append(("stopped", name)))

    reader.open(_virtual())
    with qtbot.waitSignal(reader.bus_stopped, timeout=2000):
        # The worker runs to its failure before any fan-out tick.
        assert reader._controllers["CAN_1"].wait(QDeadlineTimer(2000))
    assert events == [
        ("frame", 0x300),
        ("frame", 0x301),
        ("frame", 0x302),
        ("failed", "CAN_1"),
        ("stopped", "CAN_1"),
    ]


def test_close_timeout_retires_and_refuses_reopen(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = _hung_fake(monkeypatch)
    cfg = _virtual()
    _open(reader, qtbot, cfg)
    stopped: list[str] = []
    errors: list[str] = []
    reader.bus_stopped.connect(stopped.append)
    reader.error.connect(errors.append)
    try:
        reader.close(wait_ms=100)
        assert stopped == ["CAN_1"]
        assert not reader.is_open
        assert reader.is_closing("CAN_1")
        assert "did not stop within 0.1 s" in errors[-1]

        reader.open(cfg)
        assert "still closing" in errors[-1]
        assert not reader.is_open

        with qtbot.waitSignal(reader.bus_released, timeout=2000) as blocker:
            fake.block.set()
        assert blocker.args == ["CAN_1"]
    finally:
        fake.block.set()
    assert not reader.is_closing("CAN_1")
    monkeypatch.undo()
    _open(reader, qtbot, cfg)


def test_retiring_channel_refused_under_new_name(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = _hung_fake(monkeypatch)
    cfg = _virtual()
    _open(reader, qtbot, cfg)
    errors: list[str] = []
    reader.error.connect(errors.append)
    try:
        reader.close(wait_ms=50)
        reader.open(cfg.model_copy(update={"name": "CAN_2"}))
        assert "bus 'CAN_1' on the same channel or adapter is still closing" in errors[-1]
        assert not reader.is_open
    finally:
        with qtbot.waitSignal(reader.bus_released, timeout=2000):
            fake.block.set()


def test_failures_last_until_reopened_or_forgotten(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    a, b = _virtual("A"), _virtual("B")
    for cfg in (a, b):
        _open(reader, qtbot, cfg)
    for name in ("A", "B"):
        with qtbot.waitSignal(reader.bus_failed, timeout=2000):
            kill_capture(reader, name)
    assert list(reader.failures()) == ["A", "B"]

    _open(reader, qtbot, a)
    assert list(reader.failures()) == ["B"]
    reader.forget("B")
    assert reader.failures() == {}


def test_retiring_adapter_refused_on_another_channel(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    """A pinned adapter that moved channels is still the one the closing bus holds."""
    fake = _hung_fake(monkeypatch)
    cfg = BusConfig(
        name="CAN_1", interface="pcan", channel="PCAN_USBBUS1", bitrate=500000, hw_id="0000002a.0"
    )
    _open(reader, qtbot, cfg)
    errors: list[str] = []
    reader.error.connect(errors.append)
    try:
        reader.close(wait_ms=50)
        reader.open(cfg.model_copy(update={"name": "CAN_2", "channel": "PCAN_USBBUS2"}))
        assert "bus 'CAN_1' on the same channel or adapter is still closing" in errors[-1]
        assert not reader.is_open
    finally:
        with qtbot.waitSignal(reader.bus_released, timeout=2000):
            fake.block.set()


def test_close_all_shares_one_deadline(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = _hung_fake(monkeypatch)
    _open(reader, qtbot, _virtual("A"))
    _open(reader, qtbot, _virtual("B"))
    try:
        start = time.monotonic()
        reader.close(wait_ms=500)
        elapsed = time.monotonic() - start
        # One deadline for both; waiting per bus would take 1 s.
        assert elapsed < 0.9
        assert reader.is_closing("A") and reader.is_closing("B")
    finally:
        fake.block.set()
    qtbot.waitUntil(lambda: not reader.is_closing("A") and not reader.is_closing("B"), timeout=2000)


def test_retired_controller_never_emits_bus_started(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = FakeBus()
    fake.open_gate = threading.Event()
    use_fake_bus(monkeypatch, fake)
    started: list[str] = []
    reader.bus_started.connect(started.append)
    reader.open(_virtual())
    try:
        reader.close(wait_ms=50)
        assert reader.is_closing("CAN_1")
    finally:
        with qtbot.waitSignal(reader.bus_released, timeout=2000):
            fake.open_gate.set()
    assert started == []
    assert fake.shutdown_calls == 1


def test_controllers_deleted_after_close(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _open(reader, qtbot, _virtual("A"))
    _open(reader, qtbot, _virtual("B"))
    reader.close("A")
    reader.close()
    qtbot.waitUntil(lambda: not reader.findChildren(BusController), timeout=2000)


def test_shutdown_returns_stuck_names(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = _hung_fake(monkeypatch)
    _open(reader, qtbot, _virtual())
    try:
        assert reader.shutdown(wait_ms=50) == ["CAN_1"]
    finally:
        with qtbot.waitSignal(reader.bus_released, timeout=2000):
            fake.block.set()


def test_bus_stopped_once_per_close(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _open(reader, qtbot, _virtual())
    old = reader._controllers["CAN_1"]
    stopped: list[str] = []
    reader.bus_stopped.connect(stopped.append)

    with qtbot.waitSignal(old.finished, timeout=2000):
        reader.close()
        assert stopped == ["CAN_1"]
    assert stopped == ["CAN_1"]


def test_forget_drops_config_and_lost_health(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    from ithildin.core.types import BusHealth

    _open(reader, qtbot, _virtual("A"))
    _open(reader, qtbot, _virtual("B"))
    reader.close()
    reader._health["A"] = (BusHealth.LOST, "")

    reader.forget("A")
    assert reader.last_config_for("A") is None
    assert reader.health("A") is None
    assert reader.last_config_for("B") is not None
    reader.forget()
    assert reader.last_configs() == []


# ---- Capture session ----


def _session_with_t0(reader: BusReader, qtbot) -> tuple[BusConfig, float]:  # type: ignore[no-untyped-def]
    cfg = _virtual()
    _open(reader, qtbot, cfg)
    _send(str(cfg.channel), 0x100)
    qtbot.waitUntil(lambda: reader.session_t0 is not None, timeout=2000)
    t0 = reader.session_t0
    assert t0 is not None
    return cfg, t0


def test_session_t0_survives_worker_failure(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _cfg, t0 = _session_with_t0(reader, qtbot)
    emitted: list[object] = []
    reader.session_t0_changed.connect(emitted.append)

    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        kill_capture(reader, "CAN_1")
    assert reader.session_t0 == t0
    assert emitted == []


def test_reopen_after_failure_continues_session(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    cfg, t0 = _session_with_t0(reader, qtbot)
    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        kill_capture(reader, "CAN_1")
    emitted: list[object] = []
    reader.session_t0_changed.connect(emitted.append)
    received: list[int] = []
    reader.frames_received.connect(lambda batch: received.extend(f.arbitration_id for f in batch))

    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.reopen(cfg)
    _send(str(cfg.channel), 0x200)
    qtbot.waitUntil(lambda: 0x200 in received, timeout=2000)
    assert reader.session_t0 == t0
    assert emitted == []


def test_reopen_of_the_last_open_bus_keeps_the_session(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    """The bus-off Reconnect closes and opens the only bus, still one session."""
    cfg, t0 = _session_with_t0(reader, qtbot)
    emitted: list[object] = []
    reader.session_t0_changed.connect(emitted.append)

    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.reopen(cfg)
    assert reader.session_t0 == t0
    assert emitted == []


def test_explicit_close_keeps_t0_until_next_open(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    cfg, t0 = _session_with_t0(reader, qtbot)
    emitted: list[object] = []
    reader.session_t0_changed.connect(emitted.append)

    reader.close()
    assert reader.session_t0 == t0
    assert emitted == []
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)
        assert emitted == [None]
        assert reader.session_t0 is None


def test_close_after_a_failure_ends_the_session(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    """Disconnect with every bus already failed (or Open Project) still ends it."""
    cfg, _t0 = _session_with_t0(reader, qtbot)
    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        kill_capture(reader, "CAN_1")
    emitted: list[object] = []
    reader.session_t0_changed.connect(emitted.append)

    reader.close()
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)
    assert emitted == [None]


def test_closing_a_bus_that_isnt_open_keeps_the_session(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    """Removing an unopened bus from the project while another has failed."""
    cfg, t0 = _session_with_t0(reader, qtbot)
    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        kill_capture(reader, "CAN_1")
    emitted: list[object] = []
    reader.session_t0_changed.connect(emitted.append)

    reader.close("CAN_2")
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.reopen(cfg)
    assert emitted == []
    assert reader.session_t0 == t0


def test_refused_open_keeps_the_session(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    """An open refused while the bus is still closing must not wipe the views."""
    fake = _hung_fake(monkeypatch)
    cfg = _virtual()
    _open(reader, qtbot, cfg)
    emitted: list[object] = []
    reader.session_t0_changed.connect(emitted.append)
    try:
        reader.close(wait_ms=50)
        reader.open(cfg)
        assert not reader.is_open
        assert emitted == []
    finally:
        with qtbot.waitSignal(reader.bus_released, timeout=2000):
            fake.block.set()


def _flood(ring: RingBuffer, count: int) -> None:
    for i in range(count):
        ring.append(
            FrameRecord(bus_name="CAN_1", timestamp=float(i), arbitration_id=0x100, data=b"")
        )


def _fan_out_spy(reader: BusReader) -> tuple[list[list[FrameRecord]], list[tuple[str, int]]]:
    batches: list[list[FrameRecord]] = []
    dropped: list[tuple[str, int]] = []
    reader.frames_received.connect(batches.append)
    reader.frames_dropped.connect(lambda name, count: dropped.append((name, count)))
    return batches, dropped


def test_fan_out_caps_a_backlog_and_reports_the_skip(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _open(reader, qtbot, _virtual())
    reader._timer.stop()
    batches, dropped = _fan_out_spy(reader)

    _flood(reader.rings["CAN_1"], 10_000)
    reader._fan_out()
    assert [len(b) for b in batches] == [8192]
    assert batches[0][-1].seq == 9999
    assert dropped == [("CAN_1", 1808)]


def test_fan_out_reports_ring_overrun(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _open(reader, qtbot, _virtual())
    reader._timer.stop()
    batches, dropped = _fan_out_spy(reader)

    _flood(reader.rings["CAN_1"], 70_000)
    reader._fan_out()
    assert [len(b) for b in batches] == [8192]
    assert dropped == [("CAN_1", 70_000 - 8192)]


def test_fan_out_silent_when_keeping_up(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _open(reader, qtbot, _virtual())
    reader._timer.stop()
    batches, dropped = _fan_out_spy(reader)
    ring = reader.rings["CAN_1"]

    for _ in range(2):
        _flood(ring, 8192)
        reader._fan_out()
    assert [len(b) for b in batches] == [8192, 8192]
    assert dropped == []


def test_session_t0_is_the_first_frame_of_a_capped_backlog(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _open(reader, qtbot, _virtual())
    reader._timer.stop()
    _fan_out_spy(reader)

    _flood(reader.rings["CAN_1"], 10_000)
    reader._fan_out()
    assert reader.session_t0 == 0.0


def test_fan_out_skip_silent_without_frame_listeners(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    _open(reader, qtbot, _virtual())
    reader._timer.stop()
    dropped: list[tuple[str, int]] = []
    reader.frames_dropped.connect(lambda name, count: dropped.append((name, count)))

    _flood(reader.rings["CAN_1"], 10_000)
    reader._fan_out()
    assert dropped == []
