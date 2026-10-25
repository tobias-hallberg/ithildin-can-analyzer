# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""The send path: one lock per bus, app-built cyclic tasks, worker-thread TX flush."""

from __future__ import annotations

import logging
import threading
import time
import uuid

import can
import pytest
from can.broadcastmanager import ThreadBasedCyclicSendTask

from ithildin.app_state.bus_reader import BusReader, BusReadOnlyError
from ithildin.workspace.schema import BusConfig
from tests.fake_bus import FakeBus, use_fake_bus

_MSG = can.Message(arbitration_id=0x100, data=b"\x01\x02", is_extended_id=False)


@pytest.fixture
def reader():  # type: ignore[no-untyped-def]
    r = BusReader()
    yield r
    r.close()


def _open(reader: BusReader, qtbot, interface: str = "pcan", *, listen_only: bool = False) -> None:  # type: ignore[no-untyped-def]
    channel = f"send-{uuid.uuid4().hex[:8]}" if interface == "virtual" else "X"
    cfg = BusConfig(
        name="CAN_1",
        interface=interface,  # type: ignore[arg-type]
        channel=channel,
        bitrate=500_000,
        listen_only=listen_only,
    )
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)


def _wait_flush_serviced(qtbot, reader: BusReader, fake: FakeBus) -> None:  # type: ignore[no-untyped-def]
    worker = reader._controllers["CAN_1"].worker
    qtbot.waitUntil(lambda: not worker._flush_requested, timeout=2000)
    # The flag clears just before the flush; the next poll comes after it.
    polls = fake.recv_calls
    qtbot.waitUntil(lambda: fake.recv_calls > polls, timeout=2000)


def test_cyclic_failure_emits_and_stops_task(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot)
    task = reader.start_periodic(_MSG, "CAN_1", 0.01)
    qtbot.waitUntil(lambda: len(fake.sent) >= 2, timeout=2000)

    with qtbot.waitSignal(reader.cyclic_failed, timeout=2000) as blocker:
        fake.send_exc = can.CanOperationError("transmit queue full")
    assert blocker.args == ["CAN_1", 0x100, "transmit queue full"]
    assert task.stopped
    assert task.thread is not None
    task.thread.join(timeout=1.0)
    assert not task.thread.is_alive()


def test_every_sender_holds_the_send_lock(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot)

    reader.send_message(_MSG, "CAN_1")
    other = threading.Thread(target=reader.send_message, args=(_MSG, "CAN_1"))
    other.start()
    other.join(timeout=2.0)
    reader.send_message_async(_MSG, "CAN_1")
    task = reader.start_periodic(_MSG, "CAN_1", 0.01)

    def senders() -> set[threading.Thread]:
        return {thread for _msg, thread, _held in fake.sent}

    # The GUI thread, a plain thread (Replay, MCP), a one-shot thread, a cyclic task.
    qtbot.waitUntil(lambda: len(senders()) == 4, timeout=2000)
    reader.stop_periodic("CAN_1", task)
    assert all(held for _msg, _thread, held in fake.sent)


def test_close_stops_tasks_under_lock_before_shutdown(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot)
    task = reader.start_periodic(_MSG, "CAN_1", 0.01)
    qtbot.waitUntil(lambda: len(fake.sent) >= 1, timeout=2000)

    seen: list[tuple[bool, bool]] = []
    real_shutdown = fake.shutdown

    def shutdown() -> None:
        assert fake.lock is not None
        seen.append((fake.lock.locked(), task.stopped))
        real_shutdown()

    fake.shutdown = shutdown  # type: ignore[method-assign]
    reader.close("CAN_1")
    assert seen == [(True, True)]
    after_close = len(fake.sent)
    qtbot.wait(40)
    assert len(fake.sent) == after_close


@pytest.mark.parametrize(
    ("interface", "listen_only", "resets", "flushes"),
    [
        ("pcan", False, 1, 0),
        ("kvaser", False, 0, 1),
        ("gs_usb", False, 0, 1),
        # IXXAT's flush waits INFINITE; a listen-only bus has nothing queued.
        ("ixxat", False, 0, 0),
        ("pcan", True, 0, 0),
        ("gs_usb", True, 0, 0),
        ("virtual", False, 0, 0),
    ],
)
def test_flush_tx_per_backend(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, reader, interface, listen_only, resets, flushes
) -> None:
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, interface, listen_only=listen_only)

    reader.flush_tx("CAN_1")
    _wait_flush_serviced(qtbot, reader, fake)
    assert (fake.resets, fake.flushes) == (resets, flushes)


def test_flush_runs_on_the_worker_thread(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "kvaser")
    threads: list[threading.Thread] = []
    fake.flush_tx_buffer = lambda: threads.append(threading.current_thread())  # type: ignore[method-assign]

    reader.flush_tx("CAN_1")
    qtbot.waitUntil(lambda: bool(threads), timeout=2000)
    assert threads[0] is not threading.main_thread()


def test_flush_failure_is_logged_not_raised(qapp, qtbot, monkeypatch, caplog, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    fake.flush_exc = can.CanOperationError("canERR_HARDWARE")
    _open(reader, qtbot, "kvaser")
    errors: list[str] = []
    reader.error.connect(errors.append)

    reader.flush_tx("CAN_1")
    qtbot.waitUntil(
        lambda: any(r.getMessage() == "TX flush on 'CAN_1' failed" for r in caplog.records),
        timeout=2000,
    )
    (record,) = [r for r in caplog.records if r.getMessage() == "TX flush on 'CAN_1' failed"]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert reader.bus_names() == ["CAN_1"]
    assert errors == []


def test_send_message_async_reports_failure_off_thread(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, caplog, reader
) -> None:
    fake = use_fake_bus(monkeypatch)
    fake.send_exc = can.CanOperationError("no ACK")
    _open(reader, qtbot)

    with qtbot.waitSignal(reader.send_failed, timeout=2000) as blocker:
        reader.send_message_async(_MSG, "CAN_1")
    assert blocker.args == ["CAN_1", 0x100, "no ACK"]
    (record,) = [
        r for r in caplog.records if r.getMessage() == "One-shot send of 0x100 on 'CAN_1' failed"
    ]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


def test_send_message_async_refuses_synchronously(qapp, qtbot, reader) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RuntimeError, match="not open"):
        reader.send_message_async(_MSG, "CAN_1")
    _open(reader, qtbot, "virtual", listen_only=True)
    with pytest.raises(BusReadOnlyError):
        reader.send_message_async(_MSG, "CAN_1")


def test_ixxat_uses_app_cyclic_task_and_double_stop_is_safe(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, reader
) -> None:
    """Regression: IXXAT's hardware scheduler task raised when stopped twice."""
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "ixxat")

    task = reader.start_periodic(_MSG, "CAN_1", 0.01)
    assert isinstance(task, ThreadBasedCyclicSendTask)
    qtbot.waitUntil(lambda: len(fake.sent) >= 1, timeout=2000)
    reader.stop_periodic("CAN_1", task)
    reader.stop_periodic("CAN_1", task)
    reader.close("CAN_1")
    reader.stop_periodic("CAN_1", task)
    assert task.stopped


def test_send_after_close_raises_runtime_error(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    use_fake_bus(monkeypatch)
    _open(reader, qtbot)
    worker = reader._controllers["CAN_1"].worker
    reader.close("CAN_1")

    with pytest.raises(RuntimeError, match="not open"):
        worker.send(_MSG)
    with pytest.raises(RuntimeError, match="not open"):
        worker.send_periodic(_MSG, 0.01)
    with pytest.raises(RuntimeError):
        reader.send_message(_MSG, "CAN_1")


def test_closing_bus_refuses_sends_without_waiting_for_shutdown(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, reader
) -> None:
    """A worker that failed on its own holds the send lock through shutdown."""
    fake = use_fake_bus(monkeypatch)
    # Kvaser: a receive error without a hardware code is fatal there.
    _open(reader, qtbot, "kvaser")
    in_shutdown = threading.Event()
    release = threading.Event()
    real_shutdown = fake.shutdown

    def shutdown() -> None:
        in_shutdown.set()
        release.wait(timeout=5.0)
        real_shutdown()

    fake.shutdown = shutdown  # type: ignore[method-assign]
    fake.recv_exc = can.CanOperationError("adapter gone")
    try:
        assert in_shutdown.wait(timeout=2.0)
        # The queued stopped hasn't run yet, so the bus still looks open.
        assert reader.bus_names() == ["CAN_1"]
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="not open"):
            reader.start_periodic(_MSG, "CAN_1", 0.01)
        with pytest.raises(RuntimeError, match="not open"):
            reader.send_message(_MSG, "CAN_1")
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
    qtbot.waitUntil(lambda: reader.bus_names() == [], timeout=2000)


def test_single_bus_properties_track_open_buses(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    use_fake_bus(monkeypatch, FakeBus())
    assert reader.bus_name is None
    assert reader.ring is None
    _open(reader, qtbot)
    assert reader.bus_name == "CAN_1"
    assert reader.ring is reader.rings["CAN_1"]
