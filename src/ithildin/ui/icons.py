# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Theme-tinted SVG icon factory.

Bundled SVGs (under ``ithildin.resources.icons``) all paint with
``stroke="currentColor"`` (or ``fill="currentColor"`` for filled glyphs).
At icon-load time we substitute ``currentColor`` with the active theme's
``fg_1`` token and render the resulting SVG to ``QPixmap`` via
``QSvgRenderer`` at a fan of sizes, then cache the resulting ``QIcon``
keyed on ``(name, tint)``.

We subscribe once to ``ThemeManager.theme_changed``. On theme switch:
the cache is dropped, every ``themed_icon`` setter is re-invoked with a
freshly-tinted icon, and every ``themed_callback`` is re-run. Consumers
that hold an icon on a long-lived QAction/QToolButton should register
via ``themed_icon`` (or ``themed_callback`` for state-dependent icons)
rather than calling ``icon()`` directly — otherwise the painted icon
goes stale on theme switch.

Public API:

    icon(name)              -> QIcon  # for the current theme, cached
    toggle_icon(off, on)    -> QIcon  # off/on states for a checkable button
    themed_icon(name, set)            # auto-refreshing subscription
    themed_callback(cb)               # auto-refreshing callback
    refresh()                         # drop the cache
    known_names()           -> list[str]  # everything in the inventory
"""

from __future__ import annotations

import tempfile
import weakref
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Final

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from ithildin.ui.theme import ThemeManager

_PIXMAP_SIZES: Final[tuple[int, ...]] = (16, 24, 32, 48, 64, 72, 96)

# Aliases — names callers expect that map to a bundled SVG of a different
# name. Keep this short; prefer adding distinct SVGs over piling up aliases.
_ALIASES: Final[dict[str, str]] = {
    "open": "folder",
    "new": "plus",
}

# Inventory of bundled SVG names (without alias entries). Keeps
# ``known_names()`` deterministic and lets tests iterate.
_INVENTORY: Final[tuple[str, ...]] = (
    "bell",
    "bot",
    "bus",
    "bus_plus",
    "bus_trace",
    "check",
    "chev",
    "chip",
    "cpu",
    "data_view",
    "database",
    "database_plus",
    "dot",
    "down",
    "eye",
    "eye-off",
    "file",
    "filter",
    "folder",
    "link",
    "lock",
    "match_case",
    "match_case_on",
    "menu",
    "message_trace",
    "minus",
    "monitor",
    "pane_bottom_off",
    "pane_bottom_on",
    "pane_left_off",
    "pane_left_on",
    "pause",
    "play",
    "plot",
    "plug",
    "plus",
    "record",
    "save",
    "search",
    "send",
    "settings",
    "shield",
    "stop",
    "transmit",
    "tree",
    "unplug",
    "up",
    "window_close",
    "window_restore",
    "x",
)

_cache: dict[tuple[str, ...], QIcon] = {}
_subscribed = False
# Temp dir holding PNGs rendered for QSS ``url()`` use; created on demand.
_image_dir: Path | None = None


class _WeakBound:
    """Weak handle to a bound method that survives SIP/Shiboken wrappers.

    ``weakref.WeakMethod`` requires ``__func__``, which SIP-wrapped PySide6
    methods don't always expose. Holding the owning object weakly and
    re-binding the method by name on each call sidesteps that.
    """

    __slots__ = ("_attr", "_obj_ref")

    def __init__(self, bound: Callable[..., object]) -> None:
        owner = getattr(bound, "__self__", None)
        attr = getattr(bound, "__name__", None)
        if owner is None or attr is None:
            raise TypeError("expected a bound method with __self__ and __name__")
        self._obj_ref: weakref.ReferenceType[object] = weakref.ref(owner)
        self._attr: str = attr

    def __call__(self, *args: object) -> None:
        owner = self._obj_ref()
        if owner is None:
            raise ReferenceError("owner dead")
        getattr(owner, self._attr)(*args)

    @property
    def alive(self) -> bool:
        return self._obj_ref() is not None


# Themed-icon subscriptions: a weak handle to a bound setter, plus the icon
# name. When the underlying widget is destroyed the subscription drops.
_themed_setters: list[tuple[_WeakBound, str]] = []
# Theme-change callbacks (e.g. for state-dependent icons), held weakly.
_themed_callbacks: list[_WeakBound] = []


def _resolve(name: str) -> str:
    return _ALIASES.get(name, name)


def _svg_text(name: str) -> str:
    resolved = _resolve(name)
    path = files("ithildin.resources.icons").joinpath(f"{resolved}.svg")
    return path.read_text(encoding="utf-8")


def _render(svg_bytes: bytes, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_bytes))
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pm


def _ensure_subscribed() -> None:
    global _subscribed
    if _subscribed:
        return
    ThemeManager.instance().theme_changed.connect(lambda *_: _on_theme_changed())
    _subscribed = True


def _on_theme_changed() -> None:
    refresh()
    survivors_set: list[tuple[_WeakBound, str]] = []
    for handle, name in _themed_setters:
        if not handle.alive:
            continue
        try:
            handle(icon(name))
        except (RuntimeError, ReferenceError):
            continue
        survivors_set.append((handle, name))
    _themed_setters[:] = survivors_set

    survivors_cb: list[_WeakBound] = []
    for handle in _themed_callbacks:
        if not handle.alive:
            continue
        try:
            handle()
        except (RuntimeError, ReferenceError):
            continue
        survivors_cb.append(handle)
    _themed_callbacks[:] = survivors_cb


def icon(name: str) -> QIcon:
    """Return a ``QIcon`` for ``name``, tinted for the current theme.

    Unknown names return an empty ``QIcon`` (rendered as nothing) rather
    than raising, so a typo in a toolbar definition cannot crash the app.
    """
    _ensure_subscribed()

    tint = ThemeManager.instance().current_tokens()["fg_1"]
    key = (name, tint)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        svg_text = _svg_text(name)
    except FileNotFoundError:
        empty = QIcon()
        _cache[key] = empty
        return empty

    svg_bytes = svg_text.replace("currentColor", tint).encode("utf-8")

    qicon = QIcon()
    for size in _PIXMAP_SIZES:
        qicon.addPixmap(_render(svg_bytes, size), QIcon.Mode.Normal, QIcon.State.Off)
    _cache[key] = qicon
    return qicon


def toggle_icon(off_name: str, on_name: str) -> QIcon:
    """Return a ``QIcon`` whose Off/On states show two different glyphs.

    A checkable ``QToolButton`` with this icon flips automatically based
    on its ``isChecked()`` state.
    """
    _ensure_subscribed()

    tint = ThemeManager.instance().current_tokens()["fg_1"]
    key = (off_name, on_name, tint)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        off_svg = _svg_text(off_name).replace("currentColor", tint).encode("utf-8")
        on_svg = _svg_text(on_name).replace("currentColor", tint).encode("utf-8")
    except FileNotFoundError:
        empty = QIcon()
        _cache[key] = empty
        return empty

    qicon = QIcon()
    for size in _PIXMAP_SIZES:
        qicon.addPixmap(_render(off_svg, size), QIcon.Mode.Normal, QIcon.State.Off)
        qicon.addPixmap(_render(on_svg, size), QIcon.Mode.Normal, QIcon.State.On)
    _cache[key] = qicon
    return qicon


def stylesheet_image_path(name: str, tint: str, size: int = 10) -> str:
    """Path to a PNG of ``name`` painted in ``tint``, for a QSS ``url()``.

    Qt stylesheets read images from the filesystem or a compiled resource
    — no ``data:`` URIs — so sub-control glyphs that only QSS can place
    (the spin-box steppers) need a real file on disk. Rendered once per
    ``(name, tint, size)`` into a process-lifetime temp dir, with an
    ``@2x`` companion so HiDPI screens stay crisp.

    ``tint`` is passed explicitly rather than read from the live theme:
    the caller may be building a stylesheet for a palette that isn't the
    active one.

    Returns ``""`` — which makes the QSS rule a no-op — for an unknown
    name, or when no ``QApplication`` exists yet to render into.
    """
    global _image_dir
    if QApplication.instance() is None:
        return ""
    if _image_dir is None:
        _image_dir = Path(tempfile.mkdtemp(prefix="ithildin-qss-"))
    stem = f"{_resolve(name)}-{size}-{tint.lstrip('#')}"
    target = _image_dir / f"{stem}.png"
    if target.exists():
        return target.as_posix()
    try:
        svg_bytes = _svg_text(name).replace("currentColor", tint).encode("utf-8")
    except FileNotFoundError:
        return ""
    _render(svg_bytes, size).save(str(target))
    _render(svg_bytes, size * 2).save(str(_image_dir / f"{stem}@2x.png"))
    return target.as_posix()


def refresh() -> None:
    """Drop cached icons. Consumers should re-call ``icon(name)`` after."""
    _cache.clear()


def themed_icon(name: str, setter: Callable[[QIcon], None]) -> None:
    """Apply the tinted icon for ``name`` to ``setter`` now and on theme switch.

    ``setter`` must be a bound method (e.g. ``btn.setIcon`` or
    ``action.setIcon``) so the registry can hold it weakly. When the
    underlying widget is destroyed the subscription drops automatically.
    For state-dependent icons (``pane_left_on`` / ``pane_left_off``), use
    ``themed_callback`` with a real method instead.
    """
    _ensure_subscribed()
    setter(icon(name))
    _themed_setters.append((_WeakBound(setter), name))


def themed_callback(callback: Callable[[], None]) -> None:
    """Run ``callback`` now and on every theme switch.

    ``callback`` must be a bound method so the registry can hold it weakly.
    Use this for state-dependent icons or for code that needs to re-render
    based on the live theme tokens.
    """
    _ensure_subscribed()
    callback()
    _themed_callbacks.append(_WeakBound(callback))


def known_names() -> list[str]:
    """All names the factory recognises (inventory + aliases)."""
    return [*_INVENTORY, *_ALIASES.keys()]
