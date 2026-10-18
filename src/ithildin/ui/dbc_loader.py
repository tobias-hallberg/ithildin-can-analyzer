# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Background-thread DBC parser.

cantools' ``add_dbc_file`` is a blocking parse with no progress
callback. For large DBCs that's several seconds of frozen GUI if run
on the main thread. This module wraps the parse in a ``QObject`` worker
that the dock moves onto a fresh ``QThread`` — same lifecycle template
as ``AdapterProber``: ``started → run``, ``finished`` triggers
``thread.quit`` via ``DirectConnection`` plus signal-driven
``deleteLater`` cleanup.

The worker emits ``loaded(path, decoder)`` on success and
``failed(path, str)`` on exception, then ``finished()`` either way.
The dock owns the lifetime; consumers just connect to the signals.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ithildin.core.decoder import Decoder


class DbcLoader(QObject):
    """Parse one DBC on a worker thread, signal the result."""

    loaded = Signal(Path, object)  # (path, Decoder)
    failed = Signal(Path, str)
    finished = Signal()

    def __init__(self, path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path

    @Slot()
    def run(self) -> None:
        try:
            decoder = Decoder.from_files(self._path)
        except Exception as exc:
            self.failed.emit(self._path, str(exc))
        else:
            self.loaded.emit(self._path, decoder)
        self.finished.emit()
