# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.about_window.AboutDialog``."""

from __future__ import annotations

from ithildin import __version__
from ithildin.ui.about_window import AboutDialog, _MoonlitOrb
from ithildin.ui.theme import ThemeManager


def test_about_dialog_renders_version_and_license(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QLabel

    dlg = AboutDialog()
    qtbot.addWidget(dlg)
    label_texts = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel))
    assert __version__ in label_texts
    assert "GPL-3.0" in label_texts
    assert "Ithildin" in label_texts


def test_moonlit_orb_paints_without_crashing(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    ThemeManager.reset_for_tests()
    ThemeManager.instance().apply("dark", persist=False)
    orb = _MoonlitOrb()
    qtbot.addWidget(orb)
    # Force a paint by grabbing into a QPixmap; raises if paintEvent throws.
    pm = orb.grab()
    assert not pm.isNull()
    assert pm.width() == 120 and pm.height() == 120


def test_about_dialog_includes_tolkien_easter_egg(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QLabel

    dlg = AboutDialog()
    qtbot.addWidget(dlg)
    label_texts = " ".join(
        lbl.text() for lbl in dlg.findChildren(QLabel) if isinstance(lbl, QLabel)
    )
    assert "moon-light" in label_texts or "moonlight" in label_texts
