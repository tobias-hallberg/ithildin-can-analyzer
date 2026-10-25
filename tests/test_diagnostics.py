# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``app_state.diagnostics``.

``install()`` rewires process-wide state (root logger, stdio, excepthooks,
faulthandler, the Qt message handler), so every test that calls it runs in
a subprocess against a ``tmp_path`` log directory.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer, QtMsgType

from ithildin.app_state import diagnostics
from ithildin.app_state.diagnostics import RateLimitFilter, ReportRelay, StallWatchdog
from ithildin.ui.main_window import MainWindow


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _raised(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


def _record(
    name: str = "test",
    level: int = logging.WARNING,
    msg: str = "hello",
    args: tuple[object, ...] = (),
    exc: BaseException | None = None,
) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def test_rate_limit_burst_then_summary() -> None:
    clock = _Clock()
    limiter = RateLimitFilter(2, 10.0, clock=clock)

    assert [limiter.filter(_record()) for _ in range(5)] == [True, True, False, False, False]

    clock.now = 11.0
    original = _record()
    summary = limiter.filter(original)
    assert isinstance(summary, logging.LogRecord)
    assert summary.getMessage() == "hello [3 similar suppressed]"
    assert original.getMessage() == "hello"
    assert limiter.filter(_record()) is True
    assert limiter.filter(_record()) is False


def test_rate_limit_summary_leaves_formatting_to_the_handler() -> None:
    clock = _Clock()
    limiter = RateLimitFilter(1, 10.0, clock=clock)
    limiter.filter(_record(msg="status %d", args=(1,)))
    limiter.filter(_record(msg="status %d", args=(2,)))

    clock.now = 11.0
    summary = limiter.filter(_record(msg="status %d", args=("BUSHEAVY",)))

    assert isinstance(summary, logging.LogRecord)
    assert summary.msg == "status %d [1 similar suppressed]"
    assert summary.args == ("BUSHEAVY",)


def test_rate_limit_reenters_on_the_same_thread() -> None:
    limiter = RateLimitFilter(1, 10.0, clock=_Clock())
    results: list[bool | logging.LogRecord] = []

    def log_while_held() -> None:
        with limiter._lock:
            results.append(limiter.filter(_record()))

    thread = threading.Thread(target=log_while_held, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert results == [True]


def test_rate_limit_keys_on_template() -> None:
    limiter = RateLimitFilter(1, 10.0, clock=_Clock())

    assert limiter.filter(_record(msg="x=%d", args=(1,))) is True
    assert [limiter.filter(_record(msg="x=%d", args=(n,))) for n in range(2, 6)] == [False] * 4
    assert limiter.filter(_record(msg="y=%d", args=(1,))) is True


def test_rate_limit_separates_exception_sites() -> None:
    limiter = RateLimitFilter(1, 10.0, clock=_Clock())

    assert limiter.filter(_record(exc=_raised(ValueError("a")))) is True
    assert limiter.filter(_record(exc=_raised(KeyError("b")))) is True
    assert limiter.filter(_record(exc=_raised(ValueError("c")))) is False


def test_relay_mirrors_errors_except_can_bcm(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    relay = ReportRelay(tmp_path / "ithildin-test.log")
    seen: list[str] = []
    relay.message.connect(seen.append)

    relay.handler.handle(_record("can.pcan", logging.WARNING, "Bus error: heavy"))
    relay.handler.handle(_record("ithildin.core.bus", logging.WARNING))
    relay.handler.handle(_record("can.bcm", logging.ERROR))
    relay.handler.handle(_record("ithildin.report", logging.INFO))
    assert seen == []

    relay.handler.handle(_record("can.pcan", logging.ERROR, "Bus error: %s", ("heavy",)))
    relay.handler.handle(
        _record("uvicorn.error", logging.CRITICAL, "Tool crashed", exc=_raised(ValueError("boom")))
    )
    assert seen[0] == "can.pcan: Bus error: heavy"
    assert seen[1].startswith("uvicorn.error: Tool crashed")
    assert "ValueError: boom" in seen[1]
    assert "ithildin-test.log" in seen[1]


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no text")


def test_relay_never_raises_into_the_logging_call(qtbot, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(logging, "raiseExceptions", False)
    relay = ReportRelay(tmp_path / "ithildin-test.log")
    seen: list[str] = []
    relay.message.connect(seen.append)

    relay.handler.handle(_record(level=logging.ERROR, msg="status %d", args=("BUSHEAVY",)))
    relay.handler.handle(_record(level=logging.ERROR, exc=_raised(_Unprintable())))

    assert seen == []


def test_relay_rate_limits_repeats(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    relay = ReportRelay(tmp_path / "ithildin-test.log")
    seen: list[str] = []
    relay.message.connect(seen.append)

    for _ in range(5):
        relay.handler.handle(_record("ithildin.uncaught", logging.ERROR, "same"))

    assert len(seen) == 3


def test_relay_from_worker_thread_lands_on_gui_thread(qtbot, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    win = MainWindow(prompt_on_close=False)
    qtbot.addWidget(win)
    relay = ReportRelay(tmp_path / "ithildin-test.log")
    win.bind_log_relay(relay)

    on_gui_thread: list[bool] = []
    append = win._report_widget.append

    def recording_append(message: str, *, kind: str = "info") -> None:
        on_gui_thread.append(threading.current_thread() is threading.main_thread())
        append(message, kind=kind)  # type: ignore[arg-type]

    monkeypatch.setattr(win._report_widget, "append", recording_append)
    logger = logging.getLogger("can.test")
    logger.addHandler(relay.handler)
    try:
        worker = threading.Thread(target=lambda: logger.error("adapter gone"))
        worker.start()
        worker.join()
        qtbot.waitUntil(lambda: win._report_widget.count() == 1, timeout=2000)
    finally:
        logger.removeHandler(relay.handler)

    assert on_gui_thread == [True]
    assert "can.test: adapter gone" in win._report_widget.item(0).text()
    assert not win._report_dock.isHidden()


def test_report_also_writes_to_the_log(qtbot, caplog) -> None:  # type: ignore[no-untyped-def]
    win = MainWindow(prompt_on_close=False)
    qtbot.addWidget(win)

    with caplog.at_level(logging.INFO, logger="ithildin.report"):
        win.report("Save failed: 100% full", kind="error")

    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "ithildin.report"]
    assert records == [(logging.INFO, "[error] Save failed: 100% full")]


def _run_installed(script: str, log_dir: Path) -> subprocess.CompletedProcess[str]:
    prelude = (
        "import sys\nfrom pathlib import Path\nfrom ithildin.app_state.diagnostics import install\n"
    )
    return subprocess.run(
        [sys.executable, "-c", prelude + script, str(log_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _newest_log(log_dir: Path) -> str:
    return sorted(log_dir.glob("ithildin-*.log"))[-1].read_text(encoding="utf-8")


def test_install_keeps_the_newest_logs(tmp_path: Path) -> None:
    old = [tmp_path / f"ithildin-200001{day:02d}-120000-1.log" for day in range(1, 13)]
    for path in old:
        path.write_text("old\n", encoding="utf-8")

    result = _run_installed("install(Path(sys.argv[1]))\n", tmp_path)

    assert result.returncode == 0, result.stderr
    remaining = sorted(tmp_path.glob("ithildin-*.log"))
    assert len(remaining) == 10
    assert remaining[:9] == old[3:]


_WINDOWED_SCRIPT = """\
import logging
import threading
import warnings

sys.stdout = sys.stderr = None
install(Path(sys.argv[1]))
logging.getLogger("can.pcan").warning("Bus error: heavy")

def cyclic():
    1 / 0

thread = threading.Thread(target=cyclic, name="Cyclic send task for 0x100")
thread.start()
thread.join()
print("hello from print")
warnings.warn("legacy marker")
try:
    raise KeyError("escaped main")
except KeyError:
    sys.excepthook(*sys.exc_info())
"""


def test_windowed_build_routes_everything_to_the_log(tmp_path: Path) -> None:
    result = _run_installed(_WINDOWED_SCRIPT, tmp_path)

    assert result.returncode == 0, result.stderr
    text = _newest_log(tmp_path)
    assert "python-can" in text
    assert "frozen=False" in text
    assert "can.pcan: Bus error: heavy" in text
    assert "Uncaught exception in thread Cyclic send task for 0x100" in text
    assert "ZeroDivisionError" in text
    assert "hello from print" in text
    assert "legacy marker" in text
    assert "KeyError: 'escaped main'" in text


def test_fatal_crash_is_dumped_to_the_log(tmp_path: Path) -> None:
    script = "import faulthandler\ninstall(Path(sys.argv[1]))\nfaulthandler._sigsegv()\n"

    result = _run_installed(script, tmp_path)

    assert result.returncode != 0
    text = _newest_log(tmp_path)
    assert "Fatal Python error" in text or "Windows fatal exception" in text


def test_qt_messages_map_to_log_levels(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.DEBUG, logger="qt"):
        diagnostics._on_qt_message(QtMsgType.QtCriticalMsg, None, "boom")
        diagnostics._on_qt_message(QtMsgType.QtWarningMsg, None, "careful 100%")

    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "qt"]
    assert records == [(logging.ERROR, "boom"), (logging.WARNING, "careful 100%")]


@pytest.mark.qt_no_exception_capture
def test_slot_exception_reaches_excepthook(qtbot, caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys, "excepthook", diagnostics._log_uncaught)

    def raising_slot() -> None:
        raise ValueError("slot boom")

    QTimer.singleShot(0, raising_slot)
    qtbot.waitUntil(
        lambda: any(r.name == "ithildin.uncaught" for r in caplog.records), timeout=2000
    )

    (record,) = [r for r in caplog.records if r.name == "ithildin.uncaught"]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[0] is ValueError


def test_thread_and_unraisable_hooks(caplog) -> None:  # type: ignore[no-untyped-def]
    exc = _raised(ZeroDivisionError("cyclic"))
    thread = threading.Thread(name="Cyclic send task for 0x100")

    with caplog.at_level(logging.WARNING, logger="ithildin.uncaught"):
        diagnostics._log_thread_exception(
            SimpleNamespace(  # type: ignore[arg-type]
                exc_type=ZeroDivisionError,
                exc_value=exc,
                exc_traceback=exc.__traceback__,
                thread=thread,
            )
        )
        diagnostics._log_thread_exception(
            SimpleNamespace(  # type: ignore[arg-type]
                exc_type=SystemExit, exc_value=SystemExit(0), exc_traceback=None, thread=thread
            )
        )
        diagnostics._log_unraisable(
            SimpleNamespace(  # type: ignore[arg-type]
                exc_type=ZeroDivisionError,
                exc_value=exc,
                exc_traceback=exc.__traceback__,
                err_msg=None,
                object="BusABC.__del__",
            )
        )

    records = [r for r in caplog.records if r.name == "ithildin.uncaught"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.ERROR, "Uncaught exception in thread Cyclic send task for 0x100"),
        (logging.WARNING, "Exception ignored in: 'BusABC.__del__'"),
    ]
    assert all(r.exc_info is not None and r.exc_info[1] is exc for r in records)


def test_stall_watchdog_dumps_and_logs(qtbot, caplog, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dump_path = tmp_path / "stall.log"
    with dump_path.open("w", encoding="utf-8") as dump:
        watchdog = StallWatchdog(dump, threshold_s=0.3, interval_ms=50)
        watchdog.start()
        try:
            QTimer.singleShot(0, lambda: time.sleep(1.0))
            qtbot.waitUntil(
                lambda: any("unresponsive" in r.getMessage() for r in caplog.records),
                timeout=5000,
            )
        finally:
            watchdog.stop()

    stalls = [r for r in caplog.records if "unresponsive" in r.getMessage()]
    assert all(r.levelno == logging.ERROR for r in stalls)
    assert "Timeout (" in dump_path.read_text(encoding="utf-8")
