# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Commit-zero smoke tests.

Verifies the package imports cleanly on a fresh environment and that the
empty MainWindow constructs without crashing under offscreen Qt.
"""

from __future__ import annotations

import ithildin
from ithildin.ui.main_window import MainWindow


def test_package_has_version() -> None:
    assert ithildin.__version__


def test_main_window_constructs(qtbot) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(prompt_on_close=False)
    qtbot.addWidget(window)
    assert window.windowTitle() == "Ithildin"
