# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Cross-window UI event bus.

A small ``QObject`` carrying signals that one window emits and one or
more others react to. Used to keep windows from holding direct
references to each other (handoff §"Cross-window data" — children
subscribe to signals, never to peers).

Caches the last value of each signal so a window that spawns *after* the
emitter fires can still pick it up. Frame Inspector uses this so it
shows the selected frame even though the Inspector spawned in response
to the same selection that produced the frame.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ithildin.core.types import FrameRecord


class UiEvents(QObject):
    frame_selected = Signal(object)  # FrameRecord
    buses_changed = Signal()
    """Fires when the user adds / removes a bus declaration. Listeners
    (e.g. the toolbar Connect action) re-derive their state from
    ``project.workspace.bus``. No payload — keeps the signal cheap and
    independent of the project lifecycle."""
    report_requested = Signal(str, str)
    """``(message, kind)`` for the Report dock, from views that hold no
    ``MainWindow`` reference. ``kind`` is ``"info"``, ``"warn"`` or
    ``"error"``, as for ``MainWindow.report``."""

    def __init__(self) -> None:
        super().__init__()
        self._last_frame: FrameRecord | None = None
        self.frame_selected.connect(self._cache_frame)

    @property
    def last_selected_frame(self) -> FrameRecord | None:
        return self._last_frame

    def _cache_frame(self, frame: object) -> None:
        if isinstance(frame, FrameRecord):
            self._last_frame = frame
