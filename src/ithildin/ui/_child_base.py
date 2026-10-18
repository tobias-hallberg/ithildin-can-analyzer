# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Common base for MDI sub-window content.

Each view kind subclasses ``ChildView`` (a ``QWidget``). The MDI shell
wraps the widget in a ``QMdiSubWindow`` which provides the title bar,
min/max/close buttons, drag-to-move, and corner-resize handling — we
don't reimplement that.

Subclasses set ``KIND`` (matches the project tree leaf identifier),
``DEFAULT_SIZE``, and ``TITLE``. ``bind(ctx)`` is the post-construction
hook for wiring app singletons.

The earlier multi-window iteration of ithildin used a ``ChildWindow``
that subclassed ``QMainWindow`` and lived as a top-level. The current
canonical handoff is single-window MDI; this base reflects that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.ui.view_header import ViewHeaderBar


class ChildView(QWidget):
    """Base class for every MDI view kind."""

    KIND: str = ""
    DEFAULT_SIZE: tuple[int, int] = (640, 480)
    TITLE: str = "View"

    #: The view's top header strip, if it has one. ``WindowManager``
    #: reads this to route maximize/restore/close through the bar.
    #: Subclasses assign it in ``_build_body``.
    header: ViewHeaderBar | None = None

    def __init__(
        self,
        instance_id: str,
        params: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self._instance_id = instance_id
        self._params = dict(params or {})
        self._default_title = self._format_title(self._params)
        self._custom_name: str | None = None
        self.setWindowTitle(self._default_title)
        w, h = self.DEFAULT_SIZE
        self.resize(QSize(w, h))

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def params(self) -> dict[str, object]:
        return dict(self._params)

    def _format_title(self, params: dict[str, object]) -> str:
        # Subclasses override to incorporate channel / db name etc.
        return self.TITLE

    def bind(self, ctx: AppContext) -> None:
        """Wire app singletons after construction. Default: no-op."""

    def set_custom_name(self, name: str | None) -> None:
        """Override the auto-generated window title.

        Empty string or ``None`` reverts to the kind-default title
        (``_format_title(params)`` captured at construction). Calling
        ``setWindowTitle`` here lets every consumer that listens to
        ``windowTitleChanged`` refresh for free.
        """
        self._custom_name = name or None
        self.setWindowTitle(self._custom_name or self._default_title)

    def set_default_title(self, title: str) -> None:
        """Replace the default title (the kind+param fallback).

        Used by ``WindowManager`` to assign per-kind sequence numbers
        (``Trace 1``, ``Trace 2``, …). Has no effect on the visible
        title when a custom name is already set — that takes priority.
        """
        self._default_title = title
        if self._custom_name is None:
            self.setWindowTitle(title)

    def custom_name(self) -> str | None:
        return self._custom_name

    def save_state(self) -> dict[str, object]:
        """Per-view content snapshot for ``.itp`` persistence.

        Subclasses MUST call ``super().save_state()`` and merge their
        own keys into the returned dict — the base contributes the
        custom-name slot. Views with nothing else to round-trip (Frame
        Inspector, Data view) inherit this directly.
        """
        return {"_name": self._custom_name} if self._custom_name else {}

    def restore_state(self, state: dict[str, object]) -> None:
        """Apply a previously-saved state dict.

        Called *after* ``bind(ctx)`` so the view has its singletons.
        Subclasses MUST call ``super().restore_state(state)`` so the
        custom name lands. Implementations must tolerate unknown /
        wrong-typed keys defensively.
        """
        name = state.get("_name")
        if isinstance(name, str):
            self.set_custom_name(name or None)

    def _build_placeholder_body(self) -> None:
        """Default body — subclasses should call this only if they have no own UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(
            QLabel(f"{self.windowTitle()}\n\nPlaceholder — content lands in a later step.")
        )
        layout.addStretch(1)
