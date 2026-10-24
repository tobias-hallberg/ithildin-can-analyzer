# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""MDI sub-window registry + layout JSON persistence.

The shell hosts every view inside a single ``QMdiArea``. ``WindowManager``:

- Wraps each ``ChildView`` in a ``QMdiSubWindow`` and adds it to the area.
- Tracks (kind, instance_id, params, subwindow) for layout persistence.
- Enforces single-instance vs multi-instance rules per kind.
- Cascades newly-spawned subwindows by 28 px from the previous one
  (handoff §2.1).
- Atomic + debounced layout JSON writes; geometry is stored relative to
  the MDI viewport (not the screen), so layouts survive monitor topology
  changes trivially.

Cleanup is driven by ``QObject.destroyed`` on the inner view: when the
subwindow closes (the user hit ✕, or the project closes), the registry
entry is removed before any code can reach a dangling Python wrapper.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QMouseEvent, QPixmap, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLineEdit,
    QMdiArea,
    QMdiSubWindow,
    QProxyStyle,
    QStyle,
    QStyleOption,
    QStyleOptionTitleBar,
    QWidget,
)
from shiboken6 import isValid

if TYPE_CHECKING:
    from ithildin.ui._child_base import ChildView
    from ithildin.workspace.schema import ViewState


_BLANK_ICON: QIcon | None = None
_TIGHT_MDI_STYLE: _TightMdiFrameStyle | None = None


class _TightMdiFrameStyle(QProxyStyle):
    """Trim the MDI subwindow frame inset from the platform style.

    The Windows-Vista style's ``PM_MdiSubWindowFrameWidth`` defaults to a
    visible gutter (~4 px) between subwindow chrome and the inner view.
    Overriding it via QSS works but forces the styled paint path, which
    also kills the edge-drag resize hit-test. A ``QProxyStyle`` over the
    pixel metric leaves the native paint path intact while shrinking the
    frame to 2 px — small enough to look flush. QMdiSubWindow's own
    resize hit-test is exactly the frame width, so ``_ResizeGrip``
    widens the grab zone without widening the visible frame.

    Also zeros the title-bar height for maximized subwindows so the
    title bar disappears and the content fills the MDI viewport. Qt's
    built-in title-bar merging would handle this via the main window's
    menu bar, but Ithildin's shell hides the menu bar in favour of a
    toolbar hamburger button; instead the active view's ``ViewHeaderBar``
    surfaces the restore/close controls (``WindowManager`` wires them).
    """

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
    ) -> int:
        if metric == QStyle.PixelMetric.PM_MdiSubWindowFrameWidth:
            return 2
        if (
            metric == QStyle.PixelMetric.PM_TitleBarHeight
            and isinstance(widget, QMdiSubWindow)
            and widget.isMaximized()
        ):
            return 0
        return super().pixelMetric(metric, option, widget)


def _tight_mdi_style() -> _TightMdiFrameStyle:
    global _TIGHT_MDI_STYLE
    if _TIGHT_MDI_STYLE is None:
        _TIGHT_MDI_STYLE = _TightMdiFrameStyle()
    return _TIGHT_MDI_STYLE


_GRIP_PX = 8
_GRIP_TOP_PX = 4
_GRIP_CORNER_PX = 16

_LEFT = Qt.Edge.LeftEdge
_RIGHT = Qt.Edge.RightEdge
_TOP = Qt.Edge.TopEdge
_BOTTOM = Qt.Edge.BottomEdge


class _ResizeGrip(QWidget):
    """Invisible edge or corner handle that resizes its ``QMdiSubWindow``.

    Overlays the 2 px frame plus the outer edge of the view, so the side
    and bottom grab zones are ``_GRIP_PX`` deep — about what a native
    top-level window offers. Corners are L-shaped (masked) so they don't swallow a square
    of the title-bar buttons or the view underneath.
    """

    def __init__(self, sub: QMdiSubWindow, edges: Qt.Edge) -> None:
        super().__init__(sub)
        self._sub = sub
        self._edges = edges
        self._press: tuple[QPoint, QRect] | None = None
        horizontal = bool(edges & (_LEFT | _RIGHT))
        vertical = bool(edges & (_TOP | _BOTTOM))
        c, g = _GRIP_CORNER_PX, _GRIP_PX
        # The top band overlays the title bar; kept thin so most of the
        # bar still moves the window and its buttons stay clickable.
        depth_y = _GRIP_TOP_PX if edges & _TOP else g
        self._size: tuple[int | None, int | None]
        if horizontal and vertical:
            self._size = (c, c)
            bar_y = 0 if edges & _TOP else c - g
            bar_x = 0 if edges & _LEFT else c - g
            self.setMask(QRegion(0, bar_y, c, depth_y) + QRegion(bar_x, 0, g, c))
            backslash = edges in (_LEFT | _TOP, _RIGHT | _BOTTOM)
            self.setCursor(
                Qt.CursorShape.SizeFDiagCursor if backslash else Qt.CursorShape.SizeBDiagCursor
            )
        elif horizontal:
            self._size = (g, None)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self._size = (None, depth_y)
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        sub.installEventFilter(self)
        self._place()

    def _place(self) -> None:
        # ``None`` = span the edge between the two corner grips.
        w, h = self._sub.width(), self._sub.height()
        c = _GRIP_CORNER_PX
        gw = self._size[0] if self._size[0] is not None else max(w - 2 * c, 0)
        gh = self._size[1] if self._size[1] is not None else max(h - 2 * c, 0)
        e = self._edges
        x = 0 if e & _LEFT else (w - gw if e & _RIGHT else c)
        y = 0 if e & _TOP else (h - gh if e & _BOTTOM else c)
        self.setGeometry(x, y, gw, gh)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The subwindow still sends events while its destructor runs.
        if not isValid(self._sub):
            return False
        if event.type() == QEvent.Type.Resize:
            self._place()
        elif event.type() == QEvent.Type.WindowStateChange:
            self.setVisible(not (self._sub.isMaximized() or self._sub.isMinimized()))
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        area = self._sub.mdiArea()
        if area is not None:
            area.setActiveSubWindow(self._sub)
        self._press = (event.globalPosition().toPoint(), self._sub.geometry())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is None:
            return
        start, geom = self._press
        delta = event.globalPosition().toPoint() - start
        minimum = self._sub.minimumSizeHint().expandedTo(self._sub.minimumSize())
        r = QRect(geom)
        if self._edges & _LEFT:
            r.setLeft(min(geom.left() + delta.x(), geom.right() + 1 - minimum.width()))
        if self._edges & _RIGHT:
            r.setRight(max(geom.right() + delta.x(), geom.left() - 1 + minimum.width()))
        if self._edges & _TOP:
            r.setTop(min(geom.top() + delta.y(), geom.bottom() + 1 - minimum.height()))
        if self._edges & _BOTTOM:
            r.setBottom(max(geom.bottom() + delta.y(), geom.top() - 1 + minimum.height()))
        self._sub.setGeometry(r)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press = None


def _install_resize_grips(sub: QMdiSubWindow) -> None:
    for edges in (
        _LEFT,
        _RIGHT,
        _TOP,
        _BOTTOM,
        _LEFT | _TOP,
        _RIGHT | _TOP,
        _LEFT | _BOTTOM,
        _RIGHT | _BOTTOM,
    ):
        _ResizeGrip(sub, edges)


def _blank_icon() -> QIcon:
    """A 1×1 transparent QIcon, lazily built once.

    Qt's `QMdiSubWindow::initStyleOption` falls back to
    `QStyle::SP_TitleBarMenuButton` (the Qt logo) whenever `windowIcon()`
    is null or has no available sizes. Setting a non-null, sized icon
    short-circuits that fallback; using a transparent pixmap means the
    title bar slot draws nothing visible.
    """
    global _BLANK_ICON
    if _BLANK_ICON is None:
        pm = QPixmap(1, 1)
        pm.fill(Qt.GlobalColor.transparent)
        _BLANK_ICON = QIcon(pm)
    return _BLANK_ICON


# Single-instance view kinds: only one of each can exist at a time.
SINGLE_INSTANCE_KINDS: frozenset[str] = frozenset(
    {
        # MDI subwindow kinds that are single-instance.
        "replay",
        "record",
        "transmit",
    }
)

# Short, user-facing labels per kind. Drives the numbered default
# titles (``Message trace 1``, ``Plot 2``) and matches the launcher
# button labels in the toolbar.
KIND_LABELS: dict[str, str] = {
    "trace": "Trace",
    # Legacy aliases kept so `.itp` files that recorded these kinds still
    # render a sensible default title until the next save promotes them
    # to ``"trace"``.
    "message_trace": "Trace",
    "bus_trace": "Trace",
    "signal_plot": "Plot",
    "signal_monitor": "Monitor",
    "data_view": "Data view",
    "replay": "Replay",
    "record": "Record",
    "transmit": "Transmit",
}

_DEBOUNCE_MS = 500
_CASCADE_OFFSET_PX = 28


@dataclass
class WindowEntry:
    window_id: int
    kind: str
    instance_id: str
    view: ChildView
    subwindow: QMdiSubWindow
    params: dict[str, object] = field(default_factory=dict)


WindowFactory = Callable[["WindowManager", str, str, dict[str, object]], "ChildView"]
"""Builds a ``ChildView`` of a given kind.

Signature: ``factory(manager, kind, instance_id, params) -> ChildView``.
"""


class WindowManager(QObject):
    """Owns one ``QMdiArea`` and the subwindow registry over it."""

    window_opened = Signal(int, str)  # (window_id, kind)
    window_closed = Signal(int)
    window_renamed = Signal(int)  # window_id, after user rename
    layout_changed = Signal()
    # Emitted when a subwindow enters / leaves the maximized MDI mode.
    # The shell consumes these to swap in a merged-style restore/close
    # control pair on the toolbar (since the native title-bar merging
    # path needs a visible QMenuBar, which Ithildin doesn't have).
    subwindow_maximized = Signal(QMdiSubWindow)
    subwindow_restored = Signal()

    def __init__(
        self,
        area: QMdiArea,
        layout_path: Path,
        factory: WindowFactory,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._area = area
        self._layout_path = layout_path
        self._factory = factory
        self._next_id = 1
        self._entries: dict[int, WindowEntry] = {}
        self._inline_edits: dict[int, QLineEdit] = {}
        # The subwindow last reported as maximized to the shell, or None.
        # Only one can be maximized at a time (Qt enforces this), so a single
        # field is enough — it records the last emitted state so the handlers
        # below only fire on a real net transition.
        self._maximized_sub: QMdiSubWindow | None = None
        # When the active maximized subwindow closes, QMdiArea swaps the next
        # one into the existing maximized *mode* without emitting a
        # windowStateChanged on it — so per-window state signals miss the
        # transition. subWindowActivated always fires on the active swap;
        # reconcile against the settled state (deferred) to keep the maxbar
        # pointing at whatever is actually maximized.
        self._area.subWindowActivated.connect(
            lambda _sub=None: QTimer.singleShot(0, self._reconcile_maximized)
        )
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save_now)

    # --- Public API ---

    @property
    def area(self) -> QMdiArea:
        return self._area

    @property
    def layout_path(self) -> Path:
        return self._layout_path

    def set_layout_path(self, path: Path) -> None:
        """Re-target which ``layout.json`` future writes go to.

        Called when File → Open switches to a different project; without
        this, geometry would be persisted into the *previous* project's
        ``~/.ithildin/<slug>/layout.json`` (or ``_no_project/`` when the
        app was launched without one).
        """
        self._layout_path = path

    def _next_default_seq(self, kind: str) -> int:
        """Smallest positive integer not currently shown as ``<label> N``.

        Scans displayed titles (``view.windowTitle()``), so a closed
        window frees its slot for the next open, and a custom name that
        happens to look like a default (e.g., user typed ``Plot 5``)
        still claims slot 5.
        """
        label = KIND_LABELS.get(kind, kind.replace("_", " ").title())
        prefix = f"{label} "
        used: set[int] = set()
        for entry in self._entries.values():
            if entry.kind != kind:
                continue
            title = entry.view.windowTitle()
            if title.startswith(prefix):
                tail = title[len(prefix) :]
                if tail.isdigit():
                    used.add(int(tail))
        n = 1
        while n in used:
            n += 1
        return n

    def open(
        self,
        kind: str,
        params: dict[str, object] | None = None,
        instance_id: str | None = None,
    ) -> int:
        """Spawn or focus a sub-window. Returns its window_id."""
        if kind in SINGLE_INSTANCE_KINDS:
            existing = self._find_singleton(kind)
            if existing is not None:
                self._raise(existing)
                return existing.window_id

        iid = instance_id or uuid.uuid4().hex[:12]
        view = self._factory(self, kind, iid, params or {})
        view.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        # Numbered default title — "Trace 1", "Plot 2", etc. Single-
        # instance kinds keep the unsuffixed label since there's only
        # ever one. Multi-instance kinds get the smallest unused slot
        # so closing a window frees its number for the next open.
        label = KIND_LABELS.get(kind, kind.replace("_", " ").title())
        if kind in SINGLE_INSTANCE_KINDS:
            view.set_default_title(label)
        else:
            view.set_default_title(f"{label} {self._next_default_seq(kind)}")

        sub = QMdiSubWindow()
        sub.setStyle(_tight_mdi_style())
        sub.setWidget(view)
        sub.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        sub.setWindowTitle(view.windowTitle())
        sub.setWindowIcon(_blank_icon())
        # After setWidget so the grips stack above the view.
        _install_resize_grips(sub)
        sub.installEventFilter(self)
        # Keep the sub-window title bar in sync with view renames.
        view.windowTitleChanged.connect(sub.setWindowTitle)
        self._area.addSubWindow(sub)

        # Cascade from the previously-active subwindow's top-left.
        self._cascade(sub, view)
        sub.show()

        wid = self._next_id
        self._next_id += 1
        entry = WindowEntry(
            window_id=wid,
            kind=kind,
            instance_id=iid,
            view=view,
            subwindow=sub,
            params=dict(params or {}),
        )
        self._entries[wid] = entry
        view.destroyed.connect(lambda _obj=None, w=wid: self._on_destroyed(w))
        sub.windowStateChanged.connect(lambda _old, new, sw=sub: self._on_state_changed(sw, new))

        # When the view carries a header bar, route its maximized-only
        # restore/close controls to this subwindow and keep the bar's
        # title in sync with renames while maximized.
        if view.header is not None:
            view.header.restore_requested.connect(sub.showNormal)
            view.header.close_requested.connect(sub.close)
            view.windowTitleChanged.connect(view.header.set_maximized_title)

        self.window_opened.emit(wid, kind)
        self._schedule_save()
        return wid

    def start_inline_rename(self, window_id: int) -> None:
        """Show an in-place editor over the sub-window's title bar text.

        The QLineEdit is parented to the QMdiSubWindow and geometry-fitted
        to the title bar's label sub-control rect. Enter / focus-out
        commits; Escape cancels (handled in ``eventFilter``).
        """
        entry = self._entries.get(window_id)
        if entry is None:
            return
        if window_id in self._inline_edits:
            self._inline_edits[window_id].setFocus()
            return
        sub = entry.subwindow
        rect = self._title_bar_label_rect(sub)
        if not rect.isValid() or rect.width() < 32 or rect.height() < 12:
            # Style returned something unusable — fall back to the dialog.
            self.prompt_rename(window_id)
            return

        # Blank the sub-window's title while editing so Qt's CC_TitleBar
        # painter doesn't render the original text underneath our overlay.
        # On commit, the view's set_custom_name → windowTitleChanged →
        # sub.setWindowTitle chain restores it; cancel restores manually.
        sub.setWindowTitle("")

        edit = QLineEdit(sub)
        edit.setText(entry.view.windowTitle())
        edit.setGeometry(rect)
        # QMdiSubWindow paints its title with QApplication's
        # "QMdiSubWindowTitleBar" font, NOT the widget's font — typically
        # one or two points smaller. Use the same font so the rendered
        # glyph size matches the regular title. PySide6's stubs don't
        # expose the (className: str) overload of QApplication.font, but
        # it exists at runtime.
        edit.setFont(QApplication.font("QMdiSubWindowTitleBar"))  # type: ignore[call-overload]
        edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        edit.setFrame(False)
        edit.setTextMargins(0, 0, 0, 0)
        # Override the project's global QSS for QLineEdit
        # (dark background + 1 px border + 3 px / 6 px padding) so the
        # title bar's blue tint stays visible and the box doesn't grow.
        edit.setStyleSheet(
            "QLineEdit { background: transparent; padding: 0; margin: 0; "
            "border: 0; color: palette(highlighted-text); }"
        )
        edit.selectAll()
        self._inline_edits[window_id] = edit
        # Commit on Enter; the eventFilter handles focus-out (which
        # QLineEdit's editingFinished signal misses when the user clicks
        # away without changing the text).
        edit.returnPressed.connect(lambda wid=window_id: self._commit_inline_rename(wid))
        edit.installEventFilter(self)
        edit.show()
        edit.raise_()
        edit.setFocus()

    def _commit_inline_rename(self, window_id: int) -> None:
        edit = self._inline_edits.pop(window_id, None)
        if edit is None:
            return
        text = edit.text().strip()
        entry = self._entries.get(window_id)
        if entry is not None:
            if text != entry.view.windowTitle():
                entry.view.set_custom_name(text or None)
                self.window_renamed.emit(window_id)
            # Always re-sync the sub-window title — start_inline_rename
            # blanked it, and the windowTitleChanged → setWindowTitle
            # chain skips no-op writes (Qt deduplicates setWindowTitle
            # when the new value equals the current one).
            entry.subwindow.setWindowTitle(entry.view.windowTitle())
        edit.deleteLater()

    def _cancel_inline_rename(self, window_id: int) -> None:
        edit = self._inline_edits.pop(window_id, None)
        if edit is None:
            return
        # Restore the sub-window title (blanked on start so the original
        # title text wouldn't peek through the transparent overlay).
        entry = self._entries.get(window_id)
        if entry is not None:
            entry.subwindow.setWindowTitle(entry.view.windowTitle())
        edit.deleteLater()

    def prompt_rename(self, window_id: int) -> None:
        """Fallback dialog rename. Used when the inline editor can't fit."""
        entry = self._entries.get(window_id)
        if entry is None:
            return
        current = entry.view.custom_name() or ""
        new, ok = QInputDialog.getText(entry.view, "Rename window", "Name:", text=current)
        if not ok:
            return
        entry.view.set_custom_name(new.strip() or None)
        self.window_renamed.emit(window_id)

    def _title_bar_label_rect(self, sub: QMdiSubWindow) -> QRect:
        """Geometry of the title bar's text label, in sub-coordinates.

        Built from a hand-populated ``QStyleOptionTitleBar`` mirroring what
        QMdiSubWindow itself passes to ``QStyle::drawComplexControl``, so
        the rect tracks whatever the active style draws (including
        platform-specific button widths on the right).
        """
        opt = QStyleOptionTitleBar()
        opt.initFrom(sub)
        title_h = sub.style().pixelMetric(QStyle.PixelMetric.PM_TitleBarHeight, opt, sub)
        opt.rect = QRect(0, 0, sub.width(), title_h)
        opt.text = sub.windowTitle()
        opt.titleBarFlags = sub.windowFlags()
        opt.titleBarState = int(sub.windowState().value)
        opt.subControls = QStyle.SubControl.SC_All
        opt.activeSubControls = QStyle.SubControl.SC_None
        return sub.style().subControlRect(
            QStyle.ComplexControl.CC_TitleBar,
            opt,
            QStyle.SubControl.SC_TitleBarLabel,
            sub,
        )

    def close(self, window_id: int) -> None:
        entry = self._entries.get(window_id)
        if entry is None:
            return
        entry.subwindow.close()

    def list_windows(self) -> list[WindowEntry]:
        return list(self._entries.values())

    def find_by_kind(self, kind: str) -> list[WindowEntry]:
        return [e for e in self._entries.values() if e.kind == kind]

    def flush(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
        self._save_now()

    # --- Restore ---

    def snapshot_views(self) -> list[dict[str, object]]:
        """Capture every open view's content for ``.itp`` persistence.

        Each entry is ``{"kind", "instance_id", "params", "state"}``.
        ``state`` comes from each view's ``save_state`` hook; geometry
        is *not* included here — that stays in ``layout.json``.
        """
        return [
            {
                "kind": e.kind,
                "instance_id": e.instance_id,
                "params": dict(e.params),
                "state": e.view.save_state(),
            }
            for e in self._entries.values()
        ]

    def restore_from_workspace(self, views: list[ViewState]) -> None:
        """Re-create every view recorded in ``Workspace.views``.

        ``open`` invokes the factory (which calls ``bind(ctx)``); we
        then apply ``restore_state`` after construction. Order matters:
        some views (Plot) need ``_ctx`` set to resolve their content.

        Unknown kinds — e.g. ``frame_inspector`` entries from pre-0.1
        projects, since that kind is now an embedded pane inside Trace
        — are silently dropped rather than raised.
        """
        for v in views:
            try:
                wid = self.open(v.kind, params=dict(v.params), instance_id=v.instance_id)
            except KeyError:
                continue
            entry = self._entries.get(wid)
            if entry is not None:
                entry.view.restore_state(dict(v.state))

    def restore_from_disk(self) -> int:
        """Apply per-window geometry from ``layout.json``.

        Workspace ``views[]`` (loaded earlier via ``restore_from_workspace``)
        is authoritative for *which* windows exist and what they contain.
        This pass only restores their geometry by matching ``instance_id``;
        ``layout.json`` entries that don't correspond to a registered
        window are dropped (orphans from a stale layout file).
        """
        if not self._layout_path.exists():
            return 0
        try:
            data = json.loads(self._layout_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        windows = data.get("windows", [])
        by_iid = {e.instance_id: e for e in self._entries.values()}
        restored = 0
        for w in windows:
            kind = w.get("kind")
            if not isinstance(kind, str):
                continue
            iid = w.get("instance_id")
            if not isinstance(iid, str):
                continue
            entry = by_iid.get(iid)
            if entry is None:
                continue
            geom = w.get("geometry")
            if isinstance(geom, dict):
                self._apply_geometry(entry.window_id, geom)
            restored += 1
        return restored

    # --- Internal ---

    def _find_singleton(self, kind: str) -> WindowEntry | None:
        for entry in self._entries.values():
            if entry.kind == kind:
                return entry
        return None

    def _raise(self, entry: WindowEntry) -> None:
        sub = entry.subwindow
        sub.show()
        self._area.setActiveSubWindow(sub)

    def _cascade(self, sub: QMdiSubWindow, view: ChildView) -> None:
        vp = self._area.viewport().size()
        desired = view.size().expandedTo(view.minimumSizeHint())
        w = max(desired.width(), 320)
        h = max(desired.height(), 200)
        # Cap size to the visible viewport so the new window can never be
        # taller or wider than what's actually shown — otherwise a
        # 640×480 default plonked into a short MDI area (e.g. when the
        # bottom report dock is open) extends below the visible region.
        if vp.width() > 0:
            w = min(w, vp.width())
        if vp.height() > 0:
            h = min(h, vp.height())

        active = self._area.activeSubWindow()
        if active is not None and active is not sub:
            base = active.geometry().topLeft()
            x = base.x() + _CASCADE_OFFSET_PX
            y = base.y() + _CASCADE_OFFSET_PX
        else:
            x = 0
            y = 0
        # Wrap cascade when it would push past the viewport edge, then
        # clamp (the active window may itself have been moved off-screen,
        # e.g. after a layout restore onto a smaller monitor topology).
        if vp.width() > 0 and x + w > vp.width():
            x = 0
        if vp.height() > 0 and y + h > vp.height():
            y = 0
        if vp.width() > 0:
            x = max(0, min(x, vp.width() - w))
        if vp.height() > 0:
            y = max(0, min(y, vp.height() - h))

        sub.setGeometry(QRect(x, y, w, h))

    def _on_destroyed(self, window_id: int) -> None:
        entry = self._entries.pop(window_id, None)
        if entry is None:
            return
        if self._maximized_sub is entry.subwindow:
            # The maximized subwindow just went away. Don't assume "restored":
            # QMdiArea auto-maximizes the next subwindow when the maximized one
            # closes, and that transition may not arrive as a clean
            # windowStateChanged. Reconcile against the settled MDI state next
            # event-loop turn. We keep the (now-dead) reference until then so
            # _reconcile_maximized sees the prev→current transition and emits
            # the right signal — identity comparison against a deleted wrapper
            # is safe, and its entry is already popped so header lookups no-op.
            QTimer.singleShot(0, self._maximize_next_after_close)
        try:
            self.window_closed.emit(window_id)
            self._schedule_save()
        except RuntimeError:
            pass

    def _view_for_sub(self, sub: QMdiSubWindow) -> ChildView | None:
        for entry in self._entries.values():
            if entry.subwindow is sub:
                return entry.view
        return None

    def _show_header_controls(self, sub: QMdiSubWindow) -> None:
        view = self._view_for_sub(sub)
        if view is not None and view.header is not None:
            view.header.enter_maximized(sub.windowTitle())

    def _hide_header_controls(self, sub: QMdiSubWindow) -> None:
        # ``sub`` may already be destroyed (close-then-auto-maximize); the
        # lookup returns None in that case, so this is a safe no-op.
        view = self._view_for_sub(sub)
        if view is not None and view.header is not None:
            view.header.exit_maximized()

    def _maximize_next_after_close(self) -> None:
        # The maximized window closed. QMdiArea's implicit "auto-maximize the
        # next subwindow" doesn't fire on every platform (notably offscreen),
        # so enforce the invariant ourselves: whichever window the area
        # activates next stays maximized. Explicitly maximizing it routes
        # through _on_state_changed, which updates tracking and moves the
        # header controls. If none remain, reconcile clears tracking.
        if not isValid(self._area):
            return
        remaining = [s for s in self._area.subWindowList() if isValid(s)]
        if not remaining:
            self._reconcile_maximized()
            return
        target = self._area.activeSubWindow() or remaining[-1]
        if target.isMaximized():
            self._reconcile_maximized()
        else:
            target.showMaximized()

    def _reconcile_maximized(self) -> None:
        # Level-triggered sync of the header controls to ground truth — emit
        # only the net transition. Touches only the live ``active`` window,
        # never the one that was just destroyed.
        active = self._area.activeSubWindow()
        current = active if (active is not None and active.isMaximized()) else None
        if current is self._maximized_sub:
            return
        prev = self._maximized_sub
        self._maximized_sub = current
        if prev is not None:
            self._hide_header_controls(prev)
        if current is not None:
            style = current.style()
            style.unpolish(current)
            style.polish(current)
            self._show_header_controls(current)
            self.subwindow_maximized.emit(current)
        else:
            self.subwindow_restored.emit()

    def _on_state_changed(self, sub: QMdiSubWindow, new_state: Qt.WindowState) -> None:
        # Qt's default minimized QMdiSubWindow rendering is a cramped
        # mini-titlebar inside the MDI area; the WindowTray button is the
        # canonical representation, so hide the subwindow when minimized.
        # showNormal() (the tray's restore path) clears the minimized
        # flag and re-shows it in one call. The hide is deferred via
        # QTimer because calling hide() inside the windowStateChanged
        # handler is reverted by Qt's own state-transition processing.
        if new_state & Qt.WindowState.WindowMinimized:
            QTimer.singleShot(0, sub.hide)

        # Maximize transitions drive the in-bar window controls hosted on
        # the active view's header. The proxy style's PM_TitleBarHeight
        # branch keys on ``isMaximized()``, but Qt caches pixel metrics
        # per-widget until the next polish — force a re-polish so the
        # native title bar collapses (and re-expands) immediately.
        is_max = bool(new_state & Qt.WindowState.WindowMaximized)
        if is_max and self._maximized_sub is not sub:
            self._maximized_sub = sub
            style = sub.style()
            style.unpolish(sub)
            style.polish(sub)
            self._show_header_controls(sub)
            self.subwindow_maximized.emit(sub)
        elif not is_max and self._maximized_sub is sub:
            self._maximized_sub = None
            style = sub.style()
            style.unpolish(sub)
            style.polish(sub)
            self._hide_header_controls(sub)
            self.subwindow_restored.emit()

    def _schedule_save(self) -> None:
        try:
            self.layout_changed.emit()
        except RuntimeError:
            return
        self._save_timer.start()

    def _save_now(self) -> None:
        snapshot = self._snapshot()
        self._layout_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._layout_path.with_suffix(self._layout_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._layout_path)

    def _snapshot(self) -> dict[str, object]:
        return {
            "windows": [
                {
                    "kind": e.kind,
                    "instance_id": e.instance_id,
                    "params": e.params,
                    "geometry": _record_geometry(e.subwindow),
                }
                for e in self._entries.values()
            ]
        }

    def _apply_geometry(self, window_id: int, geom: dict[str, object]) -> None:
        entry = self._entries.get(window_id)
        if entry is None:
            return
        rect = _resolve_rect(geom)
        if rect is None:
            return
        entry.subwindow.setGeometry(rect)
        if geom.get("maximized"):
            entry.subwindow.showMaximized()
        elif geom.get("minimized"):
            entry.subwindow.showMinimized()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Inline rename — Escape cancels, focus-out commits. We can't
        # rely on QLineEdit.editingFinished because that signal only
        # fires on focus-out when the text changed; clicking away with
        # unchanged text would leave the QLineEdit alive and the
        # sub-window title blanked.
        if isinstance(watched, QLineEdit):
            if (
                event.type() == QEvent.Type.KeyPress
                and isinstance(event, QKeyEvent)
                and event.key() == Qt.Key.Key_Escape
            ):
                for wid, edit in list(self._inline_edits.items()):
                    if edit is watched:
                        self._cancel_inline_rename(wid)
                        return True
            elif event.type() == QEvent.Type.FocusOut:
                for wid, edit in list(self._inline_edits.items()):
                    if edit is watched:
                        self._commit_inline_rename(wid)
                        break
        # Double-click on the title-bar label area opens the inline editor.
        if (
            event.type() == QEvent.Type.MouseButtonDblClick
            and isinstance(watched, QMdiSubWindow)
            and isinstance(event, QMouseEvent)
            and self._title_bar_label_rect(watched).contains(event.position().toPoint())
        ):
            sub_wid = self._wid_for_sub(watched)
            if sub_wid is not None:
                self.start_inline_rename(sub_wid)
                return True
        if event.type() in (
            QEvent.Type.Move,
            QEvent.Type.Resize,
            QEvent.Type.WindowStateChange,
        ):
            self._schedule_save()
        return super().eventFilter(watched, event)

    def _wid_for_sub(self, sub: QMdiSubWindow) -> int | None:
        for entry in self._entries.values():
            if entry.subwindow is sub:
                return entry.window_id
        return None


def _record_geometry(sub: QMdiSubWindow) -> dict[str, object]:
    g = sub.geometry()
    return {
        "x": int(g.x()),
        "y": int(g.y()),
        "w": int(g.width()),
        "h": int(g.height()),
        "maximized": bool(sub.isMaximized()),
        "minimized": bool(sub.isMinimized()),
    }


def _resolve_rect(geom: dict[str, object]) -> QRect | None:
    try:
        x = _as_int(geom["x"])
        y = _as_int(geom["y"])
        w = _as_int(geom["w"])
        h = _as_int(geom["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return QRect(x, y, max(w, 80), max(h, 60))


def _as_int(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise TypeError(f"cannot coerce to int: {v!r}")
    return int(v)
