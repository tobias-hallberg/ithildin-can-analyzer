# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.theme`` — tokens, QSS, ThemeManager."""

from __future__ import annotations

import re

from PySide6.QtCore import QSettings

from ithildin.ui.theme import (
    TOKENS,
    ThemeManager,
    pyqtgraph_palette,
    resolve,
    stylesheet_for,
    tokens_for,
)

_HEX = re.compile(r"^#[0-9a-f]{6}$")

# Names every theme must define.
_REQUIRED_TOKENS = [
    "bg_0",
    "bg_1",
    "bg_2",
    "bg_3",
    "bg_hover",
    "bg_selected",
    "bg_selected_strong",
    "bg_titlebar",
    "bg_statusbar",
    "border_0",
    "border_1",
    "border_2",
    "fg_0",
    "fg_1",
    "fg_2",
    "fg_3",
    "accent",
    "accent_dim",
    "accent_bg",
    "warn_bg",
    "info_bg",
    "err_bg",
    "ok",
    "warn",
    "err",
    "info",
    "rx",
    "tx",
]


def test_tokens_dark_complete_and_hex() -> None:
    for token in _REQUIRED_TOKENS:
        value = TOKENS["dark"][token]
        assert _HEX.match(value), f"dark.{token}={value!r}"


def test_tokens_light_complete_and_hex() -> None:
    for token in _REQUIRED_TOKENS:
        value = TOKENS["light"][token]
        assert _HEX.match(value), f"light.{token}={value!r}"


def test_resolve_explicit_choices_pass_through() -> None:
    assert resolve("dark") == "dark"
    assert resolve("light") == "light"


def test_tokens_for_returns_resolved_palette() -> None:
    assert tokens_for("dark")["bg_1"] == TOKENS["dark"]["bg_1"]
    assert tokens_for("light")["bg_1"] == TOKENS["light"]["bg_1"]


def test_stylesheet_substitutes_all_tokens() -> None:
    qss = stylesheet_for(TOKENS["dark"])
    # str.format would have raised KeyError on any unsubstituted placeholder,
    # so a non-empty return means all required tokens were present.
    assert qss
    assert TOKENS["dark"]["bg_1"] in qss
    assert TOKENS["dark"]["accent"] in qss


def test_stylesheet_covers_expected_widgets() -> None:
    qss = stylesheet_for(TOKENS["dark"])
    # Spot-check the widget classes mellon-depth QSS is supposed to style.
    for selector in (
        "QMainWindow",
        "QMenuBar",
        "QMenu",
        "QToolBar",
        "QToolButton",
        "QStatusBar",
        "QDockWidget",
        "QTreeView",
        "QTableView",
        "QListView",
        "QHeaderView::section",
        "QLineEdit",
        "QPushButton",
        "QMdiArea",
    ):
        assert selector in qss, f"missing selector: {selector}"


def test_pyqtgraph_palette_has_curve_cycle() -> None:
    pal = pyqtgraph_palette(TOKENS["dark"])
    assert _HEX.match(pal["background"])  # type: ignore[arg-type]
    assert _HEX.match(pal["foreground"])  # type: ignore[arg-type]
    curves = pal["curves"]
    assert isinstance(curves, list) and len(curves) >= 4
    for c in curves:
        assert _HEX.match(c)


def test_theme_manager_singleton_is_stable() -> None:
    ThemeManager.reset_for_tests()
    a = ThemeManager.instance()
    b = ThemeManager.instance()
    assert a is b


def test_theme_manager_emits_on_apply(qapp) -> None:  # type: ignore[no-untyped-def]
    ThemeManager.reset_for_tests()
    mgr = ThemeManager.instance()

    received: list[dict[str, str]] = []
    mgr.theme_changed.connect(received.append)

    mgr.apply("light", persist=False)
    assert mgr.current_choice() == "light"
    assert received and received[-1]["bg_1"] == TOKENS["light"]["bg_1"]

    mgr.apply("dark", persist=False)
    assert mgr.current_choice() == "dark"
    assert received[-1]["bg_1"] == TOKENS["dark"]["bg_1"]


def test_theme_manager_apply_writes_palette(qapp) -> None:  # type: ignore[no-untyped-def]
    ThemeManager.reset_for_tests()
    mgr = ThemeManager.instance()
    mgr.apply("dark", persist=False)
    # Stylesheet propagated to QApplication.
    assert TOKENS["dark"]["accent"] in qapp.styleSheet()


def test_load_choice_default_is_system(qapp) -> None:  # type: ignore[no-untyped-def]
    """First-launch default (no persisted value) resolves to ``"system"``."""
    ThemeManager.reset_for_tests()
    settings = QSettings(ThemeManager.SETTINGS_ORG, ThemeManager.SETTINGS_APP)
    settings.remove(ThemeManager.SETTINGS_KEY)
    settings.sync()

    mgr = ThemeManager.instance()
    assert mgr.load_choice() == "system"


def test_stylesheet_styles_spinbox_steppers(qapp) -> None:  # type: ignore[no-untyped-def]
    """Styling the spin-box body kills Qt's native steppers.

    Once QSS touches QDoubleSpinBox, Qt draws the sub-controls itself and
    the up/down buttons render as unhittable slivers with no glyph. The
    template must give them explicit geometry and an arrow image.
    """
    qss = stylesheet_for(TOKENS["dark"])
    for selector in (
        "QDoubleSpinBox::up-button",
        "QDoubleSpinBox::down-button",
        "QDoubleSpinBox::up-arrow",
        "QDoubleSpinBox::down-arrow",
    ):
        assert selector in qss, f"missing selector: {selector}"
    # Arrow images resolve to real files, not an empty url().
    assert 'image: url("")' not in qss


def test_stylesheet_arrows_follow_the_passed_palette(qapp) -> None:  # type: ignore[no-untyped-def]
    """Arrow PNGs are tinted from the token dict handed in, not the live theme."""
    import re

    dark = set(re.findall(r'image: url\("([^"]+)"\)', stylesheet_for(TOKENS["dark"])))
    light = set(re.findall(r'image: url\("([^"]+)"\)', stylesheet_for(TOKENS["light"])))
    assert dark and light
    assert dark.isdisjoint(light), "dark and light must not share a tinted arrow PNG"
