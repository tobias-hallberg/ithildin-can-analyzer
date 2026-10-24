# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""View-launcher toolbar buttons (handoff §3).

One button per view-kind (Trace ▾, Plot ▾, Monitor ▾, Data view ▾).
Each is a split ``QToolButton``:

- Main face: spawns a new instance of the kind.
- Dropdown arrow: lists every open instance (click to focus). Empty
  state shows an italic disabled "No open xxxs".
- Drop target for ``application/x-ithildin-signal``. Dropping a
  signal/message spawns a new instance seeded with that payload. The
  Trace and Record launchers also take bus and DBC drops, since those
  windows subscribe to them too.
- Shows a small parenthesised count when ≥ 1 instance is open.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QMimeData, QPoint, QRect, Qt
from PySide6.QtGui import QAction, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QMenu, QStyle, QStyleOptionToolButton, QToolButton, QWidget

from ithildin.ui import icons
from ithildin.ui._drag_payload import (
    BUS_MIME,
    DBC_MIME,
    SIGNAL_MIME,
    MessagePayload,
    SignalPayload,
    parse_from_mime,
)

if TYPE_CHECKING:
    from ithildin.ui.window_manager import WindowManager

_DEFAULT_ACCEPTED: tuple[str, ...] = (SIGNAL_MIME,)
# Views built on the bus → DBC → message subscription tree also take
# whole buses and DBCs; the signal-level views have no meaning for them.
_TREE_ACCEPTED = (SIGNAL_MIME, DBC_MIME, BUS_MIME)
_ACCEPTED_BY_KIND: dict[str, tuple[str, ...]] = {
    "trace": _TREE_ACCEPTED,
    "record": _TREE_ACCEPTED,
}


def _has_accepted_format(mime: QMimeData, accepted: tuple[str, ...]) -> bool:
    return any(mime.hasFormat(fmt) for fmt in accepted)


class _InstanceMenu(QMenu):
    """QMenu that accepts a payload drop on one of its actions.

    Each open-instance ``QAction`` carries its ``window_id`` in
    ``action.data()``. On drop the menu looks up the action under the
    cursor, hands ``(window_id, payload)`` to the owning button's
    callback, and closes.
    """

    def __init__(
        self,
        parent: QWidget,
        on_drop: Callable[[int, object], None],
        accepted: tuple[str, ...],
    ) -> None:
        super().__init__(parent)
        self._on_drop = on_drop
        self._accepted = accepted
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _has_accepted_format(event.mimeData(), self._accepted):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if not _has_accepted_format(event.mimeData(), self._accepted):
            event.ignore()
            return
        action = self.actionAt(event.position().toPoint())
        if action is not None and action.isEnabled() and isinstance(action.data(), int):
            self.setActiveAction(action)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.close()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        payloads = parse_from_mime(event.mimeData())
        action = self.actionAt(event.position().toPoint())
        wid = action.data() if action is not None else None
        if not payloads or not isinstance(wid, int):
            event.ignore()
            self.close()
            return
        for payload in payloads:
            self._on_drop(wid, payload)
        event.acceptProposedAction()
        self.close()


class ViewLauncherButton(QToolButton):
    """One launcher button bound to a view kind + WindowManager."""

    def __init__(
        self,
        kind: str,
        label: str,
        wm: WindowManager,
        parent: object | None = None,
        *,
        icon_name: str | None = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._kind = kind
        self._label = label
        self._wm = wm
        self._accepted = _ACCEPTED_BY_KIND.get(kind, _DEFAULT_ACCEPTED)
        self._menu = _InstanceMenu(self, self._handle_drop_on_item, self._accepted)
        self.setMenu(self._menu)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        # QToolButtons added to a QToolBar via addWidget()/insertWidget()
        # don't inherit the toolbar's toolButtonStyle (only auto-created
        # buttons for QActions do). Set it explicitly so the icon appears
        # above the label, matching the toolbar's mellon-style chrome.
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setText(label)
        if icon_name is not None:
            icons.themed_icon(icon_name, self.setIcon)
        self._reserve_min_width_for_count_suffix()
        self.setAcceptDrops(True)
        self.clicked.connect(lambda: self._wm.open(self._kind))
        self._refresh_menu()
        wm.window_opened.connect(lambda *_: self._refresh_menu())
        wm.window_closed.connect(lambda *_: self._refresh_menu())
        wm.window_renamed.connect(lambda *_: self._refresh_menu())

    @property
    def kind(self) -> str:
        return self._kind

    def _reserve_min_width_for_count_suffix(self) -> None:
        # Worst-case "(99)" suffix; without this the button — and every
        # neighbour on the toolbar — shifts each time the count changes.
        original = self.text()
        self.setText(f"{self._label} (99)")
        self.setMinimumWidth(self.sizeHint().width())
        self.setText(original)

    # ---- Drop target ----

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _has_accepted_format(event.mimeData(), self._accepted):
            event.acceptProposedAction()
            self.setDown(True)
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if not _has_accepted_format(event.mimeData(), self._accepted):
            event.ignore()
            return
        event.acceptProposedAction()
        # Hovering over the arrow during a drag pops the menu so the user
        # can see existing instances before committing. Hovering back over
        # the main face closes it again.
        over_arrow = self._menu_button_rect().contains(event.position().toPoint())
        if over_arrow and not self._menu.isVisible():
            self._menu.popup(self.mapToGlobal(QPoint(0, self.height())))
        elif not over_arrow and self._menu.isVisible():
            self._menu.close()

    def dragLeaveEvent(self, event):  # type: ignore[no-untyped-def]
        self.setDown(False)
        # Don't close the menu here — the cursor may be moving from the
        # arrow region into the menu itself. The menu owns its own
        # close-on-leave behaviour.
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setDown(False)
        if self._menu.isVisible():
            self._menu.close()
        payloads = parse_from_mime(event.mimeData())
        if not payloads:
            event.ignore()
            return
        wid = self._wm.open(self._kind)
        for payload in payloads:
            self._seed(wid, payload)
        event.acceptProposedAction()

    def _menu_button_rect(self) -> QRect:
        """Sub-control rect of the menu indicator (the dropdown arrow)."""
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_ToolButton,
            opt,
            QStyle.SubControl.SC_ToolButtonMenu,
            self,
        )

    # ---- Menu ----

    def _refresh_menu(self) -> None:
        try:
            self._menu.clear()
        except RuntimeError:
            return
        open_entries = [e for e in self._wm.list_windows() if e.kind == self._kind]
        if open_entries:
            for entry in open_entries:
                title = entry.view.windowTitle() or self._label
                action = QAction(title, self._menu)
                action.setData(entry.window_id)
                action.triggered.connect(
                    lambda _checked=False, wid=entry.window_id: self._activate(wid)
                )
                self._menu.addAction(action)
            self.setText(f"{self._label} ({len(open_entries)})")
        else:
            empty_action = QAction(f"No open {self._label.lower()}s", self._menu)
            empty_action.setEnabled(False)
            font = empty_action.font()
            font.setItalic(True)
            empty_action.setFont(font)
            self._menu.addAction(empty_action)
            self.setText(self._label)

    def _activate(self, window_id: int) -> None:
        for entry in self._wm.list_windows():
            if entry.window_id == window_id:
                sub = entry.subwindow
                if sub.isMinimized():
                    # The WindowManager hides minimized subwindows so the
                    # WindowTray is the canonical representation. Calling
                    # show() with the minimized flag still set surfaces
                    # Qt's tiny title-bar widget at the bottom of the MDI
                    # area, which is the visual glitch. Clear only the
                    # minimized bit so a previously-maximized subwindow
                    # stays maximized on restore.
                    sub.setWindowState(sub.windowState() & ~Qt.WindowState.WindowMinimized)
                sub.show()
                sub.raise_()
                self._wm.area.setActiveSubWindow(sub)
                return

    def _handle_drop_on_item(self, window_id: int, payload: object) -> None:
        """Route a drop onto an existing menu item to that instance."""
        self._seed(window_id, payload)
        self._activate(window_id)

    def _seed(self, window_id: int, payload: object) -> None:
        for entry in self._wm.list_windows():
            if entry.window_id != window_id:
                continue
            view = entry.view
            if hasattr(view, "add_payload"):
                view.add_payload(payload)
            elif isinstance(payload, SignalPayload) and hasattr(view, "add_signal"):
                view.add_signal(payload.path())
            elif isinstance(payload, MessagePayload) and hasattr(view, "add_message"):
                view.add_message(payload.message)
            return
