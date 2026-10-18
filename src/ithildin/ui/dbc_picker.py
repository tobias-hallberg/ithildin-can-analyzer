# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Multi-DBC picker for the Create CAN bus dialog.

Pick one or more ``.dbc`` files — via the **+ Add DBC…** button or by dropping
files from the OS file explorer onto the box. Each is parsed off the GUI thread
(via ``DbcLoader`` — the same worker the left dock uses) and previewed inline
with a message count or a parse error. On Create the dialog reads the
successfully-parsed decoders from :meth:`DbcPicker.results` and hands them to
the left dock, which binds them to the new bus — so a database picked here is
never re-parsed downstream, and the blocking cantools parse never runs on the
GUI thread.

Each added file gets its own ``DbcLoader`` + ``QThread`` (parses are few and
short, so concurrency is fine). A module-level registry keeps each
(loader, thread) alive until its thread actually finishes, so a parse still
running when the dialog closes can't leave a ``QThread`` destroyed mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QThread, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ithildin.core.decoder import Decoder
from ithildin.ui.dbc_loader import DbcLoader
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import themed_icon

__all__ = ["DbcPicker"]

# Keep every running (loader, thread) referenced until the thread ends, so a
# parse outliving the dialog can't crash teardown. Discarded on thread.finished.
_INFLIGHT: set[tuple[DbcLoader, QThread]] = set()


@dataclass
class _Entry:
    """Per-file state backing one row. ``state`` is parsing | ok | error."""

    path: Path
    row: QWidget
    status: QLabel
    state: str = "parsing"
    decoder: Decoder | None = field(default=None)


class DbcPicker(QWidget):
    """A header + a list of added DBC files with per-file parse previews."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: dict[Path, _Entry] = {}
        self.setObjectName("DbcDropBox")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self._build()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Databases (DBC)", self)
        label.setObjectName("FieldLabel")
        header.addWidget(label)
        header.addStretch(1)
        self._add_btn = QPushButton("+ Add DBC…", self)
        self._add_btn.setObjectName("LinkButton")
        self._add_btn.setFlat(True)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.clicked.connect(self.add_files)
        header.addWidget(self._add_btn)
        outer.addLayout(header)

        self._hint = QLabel("Drop .dbc files here, or use “+ Add DBC…”.", self)
        self._hint.setObjectName("DbcHint")
        self._hint.setFont(tabular_font(8))
        outer.addWidget(self._hint)

        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(2)
        outer.addLayout(self._rows)

    # -- public API ---------------------------------------------------------

    def add_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Add DBC", "", "DBC files (*.dbc);;All files (*)"
        )
        for path_str in paths:
            self._add_path(Path(path_str))

    def results(self) -> list[tuple[Path, Decoder]]:
        """Successfully-parsed (path, decoder) pairs, in add order."""
        return [
            (e.path, e.decoder)
            for e in self._entries.values()
            if e.state == "ok" and e.decoder is not None
        ]

    def is_ready(self) -> bool:
        """True when nothing is mid-parse and nothing failed."""
        return all(e.state == "ok" for e in self._entries.values())

    def shutdown(self) -> None:
        """Detach from in-flight parses so they can't call back into a
        closing dialog. The threads still finish and self-clean via the
        module registry."""
        for loader, _thread in list(_INFLIGHT):
            for sig, slot in (
                (loader.loaded, self._on_loaded),
                (loader.failed, self._on_failed),
            ):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

    # -- rows + parsing -----------------------------------------------------

    def _add_path(self, path: Path) -> None:
        if path in self._entries:
            return
        self._entries[path] = self._make_row(path)
        self._start_parse(path)
        self._refresh_hint()
        self.changed.emit()

    def _refresh_hint(self) -> None:
        self._hint.setVisible(not self._entries)

    def _make_row(self, path: Path) -> _Entry:
        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        name = QLabel(path.name, row)
        name.setToolTip(str(path))
        rl.addWidget(name)
        rl.addStretch(1)
        status = QLabel("parsing…", row)
        status.setObjectName("DbcStatus")
        status.setFont(tabular_font(8))
        rl.addWidget(status)
        remove = QPushButton(row)
        themed_icon("x", remove.setIcon)
        remove.setToolTip("Remove DBC")
        remove.setObjectName("LinkButton")
        remove.setFlat(True)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.clicked.connect(lambda _checked=False, p=path: self.remove_path(p))
        rl.addWidget(remove)
        self._rows.addWidget(row)
        return _Entry(path=path, row=row, status=status)

    def remove_path(self, path: Path) -> None:
        entry = self._entries.pop(path, None)
        if entry is None:
            return
        entry.row.setParent(None)
        entry.row.deleteLater()
        self._refresh_hint()
        self.changed.emit()

    # -- drag & drop (OS file explorer) -------------------------------------

    @staticmethod
    def _dropped_dbc_paths(mime: QMimeData) -> list[Path]:
        if not mime.hasUrls():
            return []
        out: list[Path] = []
        for url in mime.urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".dbc"):
                out.append(Path(url.toLocalFile()))
        return out

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._dropped_dbc_paths(event.mimeData()):
            self._set_drag_active(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._dropped_dbc_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drag_active(False)
        paths = self._dropped_dbc_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        for path in paths:
            self._add_path(path)
        event.acceptProposedAction()

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def _start_parse(self, path: Path) -> None:
        loader = DbcLoader(path)
        thread = QThread()
        loader.moveToThread(thread)
        thread.started.connect(loader.run)
        loader.loaded.connect(self._on_loaded)
        loader.failed.connect(self._on_failed)
        # DirectConnection so the worker itself triggers quit; a queued slot
        # could target a main thread already tearing down. Same template as
        # AdapterProber / the left dock's DbcLoader.
        loader.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        loader.finished.connect(loader.deleteLater)
        thread.finished.connect(thread.deleteLater)
        pair = (loader, thread)
        _INFLIGHT.add(pair)
        thread.finished.connect(lambda: _INFLIGHT.discard(pair))
        thread.start()

    def _on_loaded(self, path: Path, decoder: object) -> None:
        entry = self._entries.get(path)
        if entry is None or not isinstance(decoder, Decoder):
            return  # removed mid-parse, or unexpected payload
        count = len(decoder.database.messages)
        entry.decoder = decoder
        entry.state = "ok"
        entry.status.setText(f"✓ {count} msg{'' if count == 1 else 's'}")
        entry.status.setToolTip("")
        self.changed.emit()

    def _on_failed(self, path: Path, reason: str) -> None:
        entry = self._entries.get(path)
        if entry is None:
            return
        entry.decoder = None
        entry.state = "error"
        entry.status.setText("✗ parse failed")
        entry.status.setToolTip(reason)
        self.changed.emit()
