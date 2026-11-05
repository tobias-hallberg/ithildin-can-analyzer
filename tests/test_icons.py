# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.icons`` — SVG inventory + theme invalidation."""

from __future__ import annotations

from PySide6.QtGui import QIcon

from ithildin.ui import icons
from ithildin.ui.theme import ThemeManager


def test_every_inventory_name_returns_non_null_icon(qapp) -> None:  # type: ignore[no-untyped-def]
    icons.refresh()
    for name in icons.known_names():
        qicon = icons.icon(name)
        assert isinstance(qicon, QIcon)
        # An SVG-backed icon must produce at least one non-null pixmap.
        pm = qicon.pixmap(24, 24)
        assert not pm.isNull(), f"icon({name!r}) produced null pixmap"


def test_unknown_name_returns_empty_icon(qapp) -> None:  # type: ignore[no-untyped-def]
    qicon = icons.icon("definitely-not-a-real-icon")
    assert isinstance(qicon, QIcon)
    # Empty QIcon — no pixmap data; .isNull() is the documented signal.
    assert qicon.isNull()


def test_aliases_resolve_to_real_svgs(qapp) -> None:  # type: ignore[no-untyped-def]
    # "open" → "folder", "new" → "plus" — both must yield non-null pixmaps.
    icons.refresh()
    for alias in ("open", "new"):
        pm = icons.icon(alias).pixmap(24, 24)
        assert not pm.isNull()


def test_cache_returns_same_qicon_until_refresh(qapp) -> None:  # type: ignore[no-untyped-def]
    icons.refresh()
    a = icons.icon("plot")
    b = icons.icon("plot")
    assert a is b
    icons.refresh()
    c = icons.icon("plot")
    assert c is not a


def test_theme_switch_invalidates_cache(qapp) -> None:  # type: ignore[no-untyped-def]
    ThemeManager.reset_for_tests()
    mgr = ThemeManager.instance()
    mgr.apply("dark", persist=False)
    icons.refresh()
    a = icons.icon("plot")
    # Theme change drops the cache; subsequent fetch returns a fresh QIcon.
    mgr.apply("light", persist=False)
    b = icons.icon("plot")
    assert a is not b


def _opaque_colors(qicon: QIcon, mode: QIcon.Mode, state: QIcon.State) -> set[str]:
    image = qicon.pixmap(24, 24, mode, state).toImage()
    return {
        image.pixelColor(x, y).name()
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() == 255
    }


def test_disabled_icons_use_the_disabled_text_color(qapp) -> None:  # type: ignore[no-untyped-def]
    ThemeManager.reset_for_tests()
    mgr = ThemeManager.instance()
    mgr.apply("dark", persist=False)
    tokens = mgr.current_tokens()
    icons.refresh()

    plain = icons.icon("export")
    assert _opaque_colors(plain, QIcon.Mode.Normal, QIcon.State.Off) == {tokens["fg_1"]}
    assert _opaque_colors(plain, QIcon.Mode.Disabled, QIcon.State.Off) == {tokens["fg_3"]}

    toggle = icons.toggle_icon("eye", "eye-off")
    for state in (QIcon.State.Off, QIcon.State.On):
        assert _opaque_colors(toggle, QIcon.Mode.Disabled, state) == {tokens["fg_3"]}


def test_toggle_icon_provides_off_and_on_states(qapp) -> None:  # type: ignore[no-untyped-def]
    icons.refresh()
    qicon = icons.toggle_icon("eye", "eye-off")
    off_pm = qicon.pixmap(24, 24, QIcon.Mode.Normal, QIcon.State.Off)
    on_pm = qicon.pixmap(24, 24, QIcon.Mode.Normal, QIcon.State.On)
    assert not off_pm.isNull()
    assert not on_pm.isNull()
