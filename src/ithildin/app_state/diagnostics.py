# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Process-level diagnostics: log file, crash hooks, Report-dock relay.

``install()`` runs first in ``app.main``. It opens one log file per launch
under ``~/.ithildin/logs``, points ``None`` stdio at it (the windowed frozen
build has no console), and routes warnings, uncaught exceptions on any
thread, unraisable exceptions, fatal crashes and Qt messages into it.

``ReportRelay`` mirrors ERROR and above into the Report dock.
``StallWatchdog`` dumps every thread's stack into the log when the GUI
thread stops servicing its event loop.
"""

from __future__ import annotations

import copy
import ctypes
import faulthandler
import logging
import os
import platform
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO

import can
import PySide6
from PySide6.QtCore import QObject, QTimer, QtMsgType, Signal, qInstallMessageHandler

from ithildin import __version__

LOG_DIR = Path.home() / ".ithildin" / "logs"
_KEEP_LOGS = 10
_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"

_log = logging.getLogger(__name__)
_uncaught = logging.getLogger("ithildin.uncaught")

_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def install(log_dir: Path = LOG_DIR) -> TextIO:
    """Wire process-wide diagnostics into a fresh log file and return it.

    One file per launch, named with the pid, so two running instances
    never share (or rotate) a file. The newest ``_KEEP_LOGS`` are kept.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    for old in sorted(log_dir.glob("ithildin-*.log"))[: -(_KEEP_LOGS - 1)]:
        try:
            old.unlink()
        except OSError:
            # Another running instance holds its file open; Windows refuses.
            pass
    log_file = open(
        log_dir / f"ithildin-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.log",
        "a",
        encoding="utf-8",
        errors="backslashreplace",
        buffering=1,
    )

    console = sys.stderr
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file

    # Root handlers must exist before FastMCP is built: its basicConfig()
    # is then a no-op instead of installing a second console handler.
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_stream_handler(log_file, logging.INFO))
    if console is not None:
        root.addHandler(_stream_handler(console, logging.WARNING))
    logging.captureWarnings(True)

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_exception
    sys.unraisablehook = _log_unraisable
    faulthandler.enable(file=log_file, all_threads=True)
    qInstallMessageHandler(_on_qt_message)

    _log.info(
        "Ithildin %s, Python %s, PySide6 %s, python-can %s, frozen=%s",
        __version__,
        platform.python_version(),
        PySide6.__version__,
        can.__version__,
        getattr(sys, "frozen", False),
    )
    return log_file


def _stream_handler(stream: TextIO, level: int) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(RateLimitFilter(10, 60.0))
    return handler


def _log_uncaught(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _uncaught.error("Uncaught exception", exc_info=(exc_type, exc, tb))


def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
    if issubclass(args.exc_type, SystemExit):
        return
    name = args.thread.name if args.thread is not None else "?"
    _uncaught.error("Uncaught exception in thread %s", name, exc_info=args.exc_value)


def _log_unraisable(unraisable: sys.UnraisableHookArgs) -> None:
    _uncaught.warning(
        "%s %r",
        unraisable.err_msg or "Exception ignored in:",
        unraisable.object,
        exc_info=unraisable.exc_value,
    )


def _on_qt_message(mode: QtMsgType, _context: object, message: str) -> None:
    # The Qt text is the template, so each distinct message is its own
    # rate-limit key.
    logging.getLogger("qt").log(_QT_LEVELS[mode], message)


@dataclass(slots=True)
class _Window:
    start: float
    admitted: int = 1
    suppressed: int = 0


class RateLimitFilter(logging.Filter):
    """Admit at most ``per_key`` records per key per ``window_s`` seconds.

    The key is (logger, level, template, exception site) rather than the
    formatted text, so a flood whose args change still coalesces. The first
    record admitted after a window that suppressed some carries the count,
    on a copy so other handlers see the original record.
    """

    _MAX_KEYS = 512

    def __init__(
        self,
        per_key: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._per_key = per_key
        self._window_s = window_s
        self._clock = clock
        # Handler.handle runs filters outside the handler lock. Re-entrant
        # because a GC pass while it is held can run a finalizer that logs.
        self._lock = threading.RLock()
        self._windows: dict[tuple[object, ...], _Window] = {}

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        key = (record.name, record.levelno, str(record.msg), _exc_site(record))
        now = self._clock()
        with self._lock:
            window = self._windows.get(key)
            if window is not None and now - window.start < self._window_s:
                if window.admitted < self._per_key:
                    window.admitted += 1
                    return True
                window.suppressed += 1
                return False
            if len(self._windows) >= self._MAX_KEYS:
                self._windows.clear()
            self._windows[key] = _Window(now)
        if window is None or not window.suppressed:
            return True
        # Formatting stays in emit, whose handleError catches a bad template;
        # nothing guards a filter, so it would raise into the logging call.
        summary = copy.copy(record)
        summary.msg = f"{record.msg} [{window.suppressed} similar suppressed]"
        return summary


def _exc_site(record: logging.LogRecord) -> tuple[str, str, int] | None:
    exc_type, _, tb = record.exc_info or (None, None, None)
    if exc_type is None:
        return None
    site: tuple[str, int] = ("", 0)
    while tb is not None:
        site = (tb.tb_frame.f_code.co_filename, tb.tb_lineno)
        tb = tb.tb_next
    return (exc_type.__qualname__, *site)


class ReportRelay(QObject):
    """Mirrors ERROR and above from every logger into the Report dock.

    ``app.main`` attaches ``handler`` to the root logger. Records arrive on
    any thread, so ``MainWindow.bind_log_relay`` connects ``message``
    queued. ``can.bcm`` is never mirrored: python-can logs a dying cyclic
    task there and then re-raises, so it reaches the dock once already
    through ``threading.excepthook``.
    """

    message = Signal(str)

    def __init__(self, log_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.handler: logging.Handler = _RelayHandler(self, log_path.name)
        self.handler.addFilter(_mirrored)
        self.handler.addFilter(RateLimitFilter(3, 30.0))


def _mirrored(record: logging.LogRecord) -> bool:
    return record.levelno >= logging.ERROR and record.name != "can.bcm"


class _RelayHandler(logging.Handler):
    def __init__(self, relay: ReportRelay, log_name: str) -> None:
        super().__init__()
        self._relay = relay
        self._log_name = log_name

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = f"{record.name}: {record.getMessage()}"
            exc = record.exc_info[1] if record.exc_info else None
            if exc is not None:
                text += f" — {type(exc).__name__}: {exc} (details: {self._log_name})"
            self._relay.message.emit(text)
        except Exception:
            self.handleError(record)


if sys.platform == "win32":

    def _awake_clock() -> float:
        # time.monotonic is GetTickCount64 here, which keeps counting while
        # the machine sleeps, so every resume would read as a GUI stall.
        ticks = ctypes.c_ulonglong()
        ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(ticks))
        return ticks.value / 1e7

else:
    _awake_clock = time.monotonic


class StallWatchdog(QObject):
    """Dumps every thread's stack when the GUI thread stalls.

    A GUI-thread timer re-arms ``faulthandler.dump_traceback_later`` on
    every tick. If the loop stops ticking for ``threshold_s``, faulthandler's
    own C thread writes the dump, even while an extension holds the GIL;
    the next tick then logs how long the stall lasted, measured on a clock
    that stops while the system sleeps.
    """

    def __init__(
        self,
        log_file: TextIO,
        threshold_s: float = 5.0,
        interval_ms: int = 1000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._log_file = log_file
        self._threshold_s = threshold_s
        self._last_tick = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._on_tick)

    def start(self) -> None:
        self._last_tick = _awake_clock()
        self._arm()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        faulthandler.cancel_dump_traceback_later()

    def _on_tick(self) -> None:
        now = _awake_clock()
        gap = now - self._last_tick
        self._last_tick = now
        if gap > self._threshold_s:
            _log.error(
                "GUI thread was unresponsive for %.1f s; thread stacks were dumped to the log",
                gap,
            )
        self._arm()

    def _arm(self) -> None:
        faulthandler.dump_traceback_later(self._threshold_s, repeat=True, file=self._log_file)
