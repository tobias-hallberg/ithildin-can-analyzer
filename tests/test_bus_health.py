# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Bus health: receive-error classification, the health tick, error-frame sampling."""

from __future__ import annotations

import can
import pytest
import usb.core
from can.interfaces.pcan import basic
from can.interfaces.pcan.pcan import PcanCanOperationError

from ithildin.app_state.bus_reader import BusReader
from ithildin.core import bus as bus_module
from ithildin.core.bus import BusWorker, _pcan_level, _socketcan_level
from ithildin.core.ringbuf import RingBuffer
from ithildin.core.types import BusHealth, FrameRecord
from ithildin.workspace.schema import BusConfig
from tests.fake_bus import FakeBus, pcan_error, use_fake_bus

_CRTL = 0x04
_ACK = 0x20
_BUSOFF = 0x40
_RESTARTED = 0x100


@pytest.fixture(autouse=True)
def _fast_ticks(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(bus_module, "_HEALTH_TICK_S", 0.1)


@pytest.fixture
def reader():  # type: ignore[no-untyped-def]
    r = BusReader()
    yield r
    r.close()


def _open(reader: BusReader, qtbot, interface: str, *, listen_only: bool = False) -> None:  # type: ignore[no-untyped-def]
    cfg = BusConfig(
        name="CAN_1",
        interface=interface,  # type: ignore[arg-type]
        channel="X",
        bitrate=500_000,
        listen_only=listen_only,
    )
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)


def _error_frame(err_class: int = 0, data: bytes = b"") -> can.Message:
    return can.Message(arbitration_id=err_class, data=data, is_error_frame=True)


def _data_frame(arbitration_id: int, payload: int = 0) -> can.Message:
    return can.Message(arbitration_id=arbitration_id, data=bytes([payload]), is_extended_id=False)


def _records(reader: BusReader) -> list[FrameRecord]:
    frames, _dropped = reader.rings["CAN_1"].read_since(-1)
    return frames


def _state(state: str):  # type: ignore[no-untyped-def]
    return lambda _name, s, _detail: s == state


# ---- Error-frame sampling ----


def test_error_flood_is_sampled_with_exact_counts(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "virtual")

    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        for _ in range(2000):
            fake.rx.put(_error_frame())
    assert blocker.args == ["CAN_1", "errors", "error-frame flood"]
    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        pass
    assert blocker.args == ["CAN_1", "ok", ""]

    # The tail of the flood rides on the next sampled error frame.
    fake.rx.put(_error_frame())
    qtbot.waitUntil(lambda: sum(r.coalesced for r in _records(reader)) == 2001, timeout=2000)
    records = _records(reader)
    assert all(r.is_error_frame for r in records)
    assert len(records) <= 10


def test_isolated_error_frames_pass_unsampled(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "virtual")

    with qtbot.assertNotEmitted(reader.bus_health_changed):
        for n in range(1, 4):
            fake.rx.put(_error_frame())
            qtbot.waitUntil(lambda n=n: len(_records(reader)) == n, timeout=2000)
            # Past the sampling interval, so the next one is sampled too.
            qtbot.wait(150)
    assert [r.coalesced for r in _records(reader)] == [1, 1, 1]


def test_data_frames_are_never_dropped_in_a_flood(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "virtual")

    for i in range(100):
        fake.rx.put(_data_frame(0x100, i))
        for _ in range(20):
            fake.rx.put(_error_frame())
    fake.rx.put(_data_frame(0x200))
    qtbot.waitUntil(lambda: any(r.arbitration_id == 0x200 for r in _records(reader)), timeout=5000)
    data = [r.data[0] for r in _records(reader) if r.arbitration_id == 0x100]
    assert data == list(range(100))


# ---- PCAN ----


def test_pcan_bus_passive_raise_keeps_capturing(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "pcan")

    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
        fake.rx.put(PcanCanOperationError("The CAN controller is error passive"))
    assert blocker.args == ["CAN_1", "errors", "error passive"]
    fake.rx.put(_data_frame(0x123))
    qtbot.waitUntil(lambda: any(r.arbitration_id == 0x123 for r in _records(reader)), timeout=2000)
    assert reader.bus_names() == ["CAN_1"]

    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.status_code = 0
    assert blocker.args == ["CAN_1", "ok", ""]


@pytest.mark.parametrize("status", [0, basic.PCAN_ERROR_BUSHEAVY])
def test_pcan_error_the_status_does_not_explain_is_fatal(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, reader, status
) -> None:
    """Retrying would spin with nothing shown. python-can never raises for error warning."""
    fake = use_fake_bus(monkeypatch)
    fake.status_code = status
    _open(reader, qtbot, "pcan")

    with (
        qtbot.assertNotEmitted(reader.bus_health_changed),
        qtbot.waitSignal(reader.bus_failed, timeout=2000) as blocker,
    ):
        fake.rx.put(PcanCanOperationError(pcan_error(basic.PCAN_ERROR_ILLMODE)))
    assert pcan_error(basic.PCAN_ERROR_ILLMODE) in blocker.args[1]
    assert reader.health("CAN_1") is None


@pytest.mark.parametrize(
    "code",
    [
        basic.PCAN_ERROR_OVERRUN,
        basic.PCAN_ERROR_QOVERRUN,
        basic.PCAN_ERROR_QOVERRUN | basic.PCAN_ERROR_OVERRUN,
    ],
)
def test_pcan_overrun_keeps_capturing(qapp, qtbot, monkeypatch, reader, code) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    # One line per bit, as python-can words a code the driver has no text for.
    overruns = (basic.PCAN_ERROR_QOVERRUN, basic.PCAN_ERROR_OVERRUN)
    text = "\n".join(pcan_error(bit) for bit in overruns if code & bit)
    _open(reader, qtbot, "pcan")

    with qtbot.assertNotEmitted(reader.bus_health_changed):
        fake.rx.put(PcanCanOperationError(text))
        fake.rx.put(_data_frame(0x123))
        qtbot.waitUntil(
            lambda: any(r.arbitration_id == 0x123 for r in _records(reader)), timeout=2000
        )
    assert reader.bus_names() == ["CAN_1"]


@pytest.mark.parametrize(("listen_only", "resets"), [(False, 2), (True, 0)])
def test_worsening_flushes_tx(qapp, qtbot, monkeypatch, reader, listen_only, resets) -> None:  # type: ignore[no-untyped-def]
    """The worker flushes on its own, so MCP and Replay frames don't burst out later."""
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "pcan", listen_only=listen_only)

    with qtbot.waitSignal(
        reader.bus_health_changed, timeout=2000, check_params_cb=_state("errors")
    ):
        fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
    with qtbot.waitSignal(
        reader.bus_health_changed, timeout=2000, check_params_cb=_state("bus_off")
    ):
        fake.status_code = basic.PCAN_ERROR_BUSOFF
    # Better, but still degraded: no flush.
    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
    assert blocker.args == ["CAN_1", "errors", "error passive"]
    assert fake.resets == resets


def test_pcan_handle_error_is_lost_and_fails(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    """A pulled PCAN-USB only ever times out on recv; the status probe catches it."""
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "pcan")
    health: list[tuple[str, str, str]] = []
    reader.bus_health_changed.connect(lambda *args: health.append(args))

    with qtbot.waitSignal(reader.bus_failed, timeout=2000) as blocker:
        fake.status_code = basic.PCAN_ERROR_ILLHW
    assert health == [("CAN_1", "lost", "")]
    assert blocker.args == ["CAN_1", "Capture error on 'CAN_1': adapter disconnected"]
    assert reader.bus_names() == []
    assert reader.health("CAN_1") == (BusHealth.LOST, "")


@pytest.mark.parametrize(
    ("code", "level"),
    [
        (0, (BusHealth.OK, "")),
        (basic.PCAN_ERROR_BUSLIGHT, (BusHealth.ERRORS, "error warning")),
        (basic.PCAN_ERROR_BUSHEAVY, (BusHealth.ERRORS, "error warning")),
        (
            basic.PCAN_ERROR_BUSPASSIVE | basic.PCAN_ERROR_BUSHEAVY,
            (BusHealth.ERRORS, "error passive"),
        ),
        (basic.PCAN_ERROR_BUSOFF, (BusHealth.BUS_OFF, "")),
        (basic.PCAN_ERROR_ILLHW, (BusHealth.LOST, "")),
        (basic.PCAN_ERROR_ILLNET, (BusHealth.LOST, "")),
        (basic.PCAN_ERROR_ILLCLIENT, (BusHealth.LOST, "")),
        # Same field as the handle errors, but not one of them.
        (basic.PCAN_ERROR_HWINUSE, (BusHealth.OK, "")),
        (basic.PCAN_ERROR_NETINUSE, (BusHealth.OK, "")),
        (basic.PCAN_ERROR_NODRIVER, (BusHealth.LOST, "")),
        (basic.PCAN_ERROR_INITIALIZE, (BusHealth.LOST, "")),
        (basic.PCAN_ERROR_ILLOPERATION, (BusHealth.LOST, "")),
        (basic.PCAN_ERROR_REGTEST, (BusHealth.LOST, "")),
        (basic.PCAN_ERROR_QXMTFULL, (BusHealth.OK, "")),
        (basic.PCAN_ERROR_OVERRUN, (BusHealth.OK, "")),
    ],
)
def test_pcan_status_mapping(code: int, level: tuple[BusHealth, str]) -> None:
    assert _pcan_level(code) == level


# ---- IXXAT / Kvaser ----


@pytest.mark.parametrize(
    ("message", "state", "detail"),
    [
        ("Error warning limit exceeded", "errors", "error warning"),
        ("Bus coupling error", "errors", "bus coupling error"),
        ("Bus off status", "bus_off", ""),
    ],
)
def test_ixxat_status_errors_are_recoverable(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, reader, message, state, detail
) -> None:
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "ixxat")

    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.rx.put(can.CanOperationError(message))
    assert blocker.args == ["CAN_1", state, detail]
    assert reader.bus_names() == ["CAN_1"]
    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        pass
    assert blocker.args == ["CAN_1", "ok", ""]


def test_ixxat_unknown_error_is_fatal(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "ixxat")

    with (
        qtbot.assertNotEmitted(reader.bus_health_changed),
        qtbot.waitSignal(reader.bus_failed, timeout=2000) as blocker,
    ):
        fake.rx.put(can.CanOperationError("Unknown CAN error message code 9"))
    assert "Unknown CAN error message code 9" in blocker.args[1]
    assert reader.health("CAN_1") is None


def test_ixxat_data_overrun_is_not_degraded(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "ixxat")

    with qtbot.assertNotEmitted(reader.bus_health_changed, wait=300):
        fake.rx.put(can.CanOperationError("Data overrun occurred"))
    assert reader.health("CAN_1") == (BusHealth.OK, "")


@pytest.mark.parametrize("code", [-15, -26])  # canERR_HARDWARE, canERR_NOCARD
def test_kvaser_hardware_error_is_lost(qapp, qtbot, monkeypatch, reader, code) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "kvaser")
    health: list[tuple[str, str, str]] = []
    reader.bus_health_changed.connect(lambda *args: health.append(args))

    with qtbot.waitSignal(reader.bus_failed, timeout=2000) as blocker:
        fake.rx.put(can.CanOperationError("canReadWait failed - Hardware error", error_code=code))
    assert health == [("CAN_1", "lost", "")]
    assert "Hardware error" in blocker.args[1]
    assert reader.health("CAN_1") == (BusHealth.LOST, "")


@pytest.mark.parametrize(
    ("cause", "health"),
    [
        (
            usb.core.USBError("No such device (it may have been disconnected)", -4, 19),
            (BusHealth.LOST, ""),
        ),
        (None, None),
    ],
)
def test_gs_usb_recv_error_is_lost_only_from_usb(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, reader, cause, health
) -> None:
    """GsUsbBus raises from a USBError when its probe finds the adapter gone."""
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "gs_usb")
    exc = can.CanOperationError("gs_usb adapter lost")
    exc.__cause__ = cause

    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        fake.rx.put(exc)
    assert reader.health("CAN_1") == health


# ---- SocketCAN / gs_usb error frames ----


@pytest.mark.parametrize("interface", ["socketcan", "gs_usb"])
def test_error_frames_drive_the_controller_state(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, reader, interface
) -> None:
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, interface)

    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.rx.put(_error_frame(_CRTL, bytes([0, 0x20])))
    assert blocker.args == ["CAN_1", "errors", "error passive"]
    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.rx.put(_error_frame(_BUSOFF))
    assert blocker.args == ["CAN_1", "bus_off", ""]
    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.rx.put(_error_frame(_RESTARTED))
    assert blocker.args == ["CAN_1", "ok", ""]


def test_socketcan_state_clears_on_data_after_quiet(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(bus_module, "_STICKY_CLEAR_S", 0.2)
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "socketcan")

    with qtbot.waitSignal(
        reader.bus_health_changed, timeout=2000, check_params_cb=_state("errors")
    ):
        fake.rx.put(_error_frame(_CRTL, bytes([0, 0x08])))
    # A silent bus keeps the last state the controller reported.
    with qtbot.assertNotEmitted(reader.bus_health_changed, wait=400):
        pass
    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000) as blocker:
        fake.rx.put(_data_frame(0x100))
    assert blocker.args == ["CAN_1", "ok", ""]


@pytest.mark.parametrize(
    ("err_class", "crtl", "level"),
    [
        (_BUSOFF, 0, (BusHealth.BUS_OFF, "")),
        (_RESTARTED, 0, (BusHealth.OK, "")),
        (_CRTL, 0x40, (BusHealth.OK, "")),
        (_CRTL, 0x10, (BusHealth.ERRORS, "error passive")),
        (_CRTL, 0x04, (BusHealth.ERRORS, "error warning")),
        (_ACK, 0, (BusHealth.ERRORS, "no ACK")),
        (0x08, 0, None),
    ],
)
def test_socketcan_error_frame_mapping(
    err_class: int, crtl: int, level: tuple[BusHealth, str] | None
) -> None:
    assert _socketcan_level(_error_frame(err_class, bytes([0, crtl]))) == level


# ---- The health tick ----


@pytest.fixture
def ticking(qapp):  # type: ignore[no-untyped-def]
    """A worker, never started, ticked by hand against a fake bus."""
    fakes: list[FakeBus] = []

    def make(interface: str) -> tuple[BusWorker, FakeBus, list[tuple[str, str]]]:
        cfg = BusConfig(name="CAN_1", interface=interface, channel="X", bitrate=500_000)  # type: ignore[arg-type]
        worker = BusWorker("CAN_1", cfg, RingBuffer(64))
        emitted: list[tuple[str, str]] = []
        worker.health.connect(lambda state, detail: emitted.append((state, detail)))
        fakes.append(FakeBus())
        return worker, fakes[-1], emitted

    yield make
    for fake in fakes:
        fake.shutdown()


def test_recovery_needs_two_clean_ticks_in_a_row(ticking) -> None:  # type: ignore[no-untyped-def]
    worker, fake, emitted = ticking("pcan")
    fake.status_code = basic.PCAN_ERROR_BUSOFF
    worker._health_tick(fake, 0.0)
    assert emitted == [("bus_off", "")]

    for status in (0, basic.PCAN_ERROR_BUSOFF, 0):
        fake.status_code = status
        worker._health_tick(fake, 0.0)
    assert emitted == [("bus_off", "")]
    worker._health_tick(fake, 0.0)
    assert emitted == [("bus_off", ""), ("ok", "")]


@pytest.mark.parametrize(
    ("interface", "detail"),
    [("pcan", "error passive"), ("ixxat", "error warning"), ("socketcan", "no ACK")],
)
def test_a_specific_detail_beats_the_flood(ticking, interface, detail) -> None:  # type: ignore[no-untyped-def]
    """Ties go to the source that says the most, so the detail doesn't flap."""
    worker, fake, emitted = ticking(interface)
    fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
    if interface == "ixxat":
        worker._note_recv_error(fake, can.CanOperationError("Error warning limit exceeded"))
    for _ in range(100):
        worker._count_error_frame(_error_frame(_ACK), 0.0)
    worker._health_tick(fake, 0.0)
    assert emitted == [("errors", detail)]


def test_pcan_error_warning_counts_only_while_errors_arrive(ticking) -> None:  # type: ignore[no-untyped-def]
    """The controller holds error warning until it next transmits, long after a re-plug."""
    worker, fake, emitted = ticking("pcan")
    fake.status_code = basic.PCAN_ERROR_BUSHEAVY
    worker._health_tick(fake, 0.0)
    assert emitted == []

    worker._count_error_frame(_error_frame(), 0.0)
    worker._health_tick(fake, 0.0)
    assert emitted == [("errors", "error warning")]
    worker._health_tick(fake, 0.0)
    worker._health_tick(fake, 0.0)
    assert emitted == [("errors", "error warning"), ("ok", "")]


# ---- BusReader ----


def test_health_is_none_while_opening(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    use_fake_bus(monkeypatch)
    cfg = BusConfig(name="CAN_1", interface="pcan", channel="X", bitrate=500_000)

    with qtbot.waitSignal(reader.bus_opening, timeout=1000) as blocker:
        reader.open(cfg)
    assert blocker.args == ["CAN_1"]
    assert reader.health("CAN_1") is None
    qtbot.waitUntil(lambda: reader.health("CAN_1") == (BusHealth.OK, ""), timeout=2000)


def test_health_is_held_and_cleared_on_close(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "pcan")

    with qtbot.waitSignal(reader.bus_health_changed, timeout=2000):
        fake.status_code = basic.PCAN_ERROR_BUSOFF
    assert reader.health("CAN_1") == (BusHealth.BUS_OFF, "")
    reader.close("CAN_1")
    assert reader.health("CAN_1") is None


def test_lost_survives_failure_and_clears_on_reopen(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "pcan")
    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        fake.status_code = basic.PCAN_ERROR_ILLHW
    assert reader.health("CAN_1") == (BusHealth.LOST, "")

    use_fake_bus(monkeypatch, FakeBus())
    cfg = reader.last_config_for("CAN_1")
    assert cfg is not None
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(cfg)
        assert reader.health("CAN_1") is None
    assert reader.health("CAN_1") == (BusHealth.OK, "")


def test_close_clears_lost(qapp, qtbot, monkeypatch, reader) -> None:  # type: ignore[no-untyped-def]
    fake = use_fake_bus(monkeypatch)
    _open(reader, qtbot, "pcan")
    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        fake.status_code = basic.PCAN_ERROR_ILLHW

    reader.close("CAN_1")
    assert reader.health("CAN_1") is None
