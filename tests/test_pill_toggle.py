# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.pill_toggle.IthildinPillToggle``."""

from __future__ import annotations

from ithildin.ui.pill_toggle import IthildinPillToggle
from ithildin.ui.theme import ThemeManager


def test_toggled_signal_fires_on_click(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    toggle = IthildinPillToggle()
    qtbot.addWidget(toggle)
    states: list[bool] = []
    toggle.toggled.connect(states.append)

    with qtbot.waitSignal(toggle.toggled):
        toggle.toggle()
    assert toggle.isChecked()
    assert states == [True]


def test_thumb_animates_toward_checked_state(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    toggle = IthildinPillToggle()
    qtbot.addWidget(toggle)
    assert toggle.thumb_pos == 0.0
    toggle.setChecked(True)
    # The animation drives thumb_pos to 1.0; wait for it to settle.
    qtbot.waitUntil(lambda: toggle.thumb_pos == 1.0, timeout=1000)


def test_danger_variant_paints_without_error(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    toggle = IthildinPillToggle(danger=True)
    qtbot.addWidget(toggle)
    toggle.setChecked(True)
    toggle.show()
    toggle.repaint()  # exercises the paint path


def test_theme_change_repaints_safely(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    toggle = IthildinPillToggle()
    qtbot.addWidget(toggle)
    # Emitting theme_changed must not raise; the toggle just schedules an update.
    ThemeManager.instance().theme_changed.emit(ThemeManager.instance().current_tokens())
