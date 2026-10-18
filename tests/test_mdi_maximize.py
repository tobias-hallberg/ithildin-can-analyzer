# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Maximized-subwindow chrome (the in-bar window controls) tracking.

A maximized QMdiSubWindow has its title bar collapsed to 0 px by
``_TightMdiFrameStyle``, so the active view's ``ViewHeaderBar`` restore/close
controls are its only chrome. The invariant: those controls are visible iff
that view's window is maximized, and they target that window. The tricky case
is closing a maximized window — QMdiArea then auto-maximizes the next one, and
the controls must move to that window's header.
"""

from __future__ import annotations

from pathlib import Path

from ithildin.ui.factory import make_factory
from ithildin.ui.main_window import MainWindow
from ithildin.ui.window_manager import WindowManager
from tests.conftest import make_app_context


def _sub_for(wm: WindowManager, window_id: int):  # type: ignore[no-untyped-def]
    return next(e.subwindow for e in wm.list_windows() if e.window_id == window_id)


def _header_for(wm: WindowManager, window_id: int):  # type: ignore[no-untyped-def]
    return next(e.view.header for e in wm.list_windows() if e.window_id == window_id)


def _build(qtbot, tmp_path: Path):  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    wm = WindowManager(win.mdi_area, tmp_path / "layout.json", make_factory(ctx))
    win.set_window_manager(wm)
    win.bind_events(ctx)
    return win, wm


def test_closing_maximized_window_moves_controls_to_next(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    win, wm = _build(qtbot, tmp_path)

    id_a = wm.open("trace")
    id_b = wm.open("trace")
    sub_a = _sub_for(wm, id_a)
    sub_b = _sub_for(wm, id_b)
    header_a = _header_for(wm, id_a)
    header_b = _header_for(wm, id_b)

    with qtbot.waitSignal(wm.subwindow_maximized, timeout=2000):
        sub_a.showMaximized()
    assert win._maximized_sub is sub_a
    assert header_a.window_controls_visible()

    # Close the maximized window; QMdiArea auto-maximizes sub_b. The controls
    # must move to sub_b's header rather than vanishing (which would leave
    # sub_b maximized with no title bar and no way to restore/close it).
    sub_a.close()
    qtbot.waitUntil(lambda: sub_b.isMaximized(), timeout=2000)
    qtbot.waitUntil(lambda: header_b.window_controls_visible(), timeout=2000)


def test_closing_last_maximized_window_hides_controls(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    win, wm = _build(qtbot, tmp_path)

    id_a = wm.open("trace")
    sub_a = _sub_for(wm, id_a)
    header_a = _header_for(wm, id_a)

    with qtbot.waitSignal(wm.subwindow_maximized, timeout=2000):
        sub_a.showMaximized()
    assert header_a.window_controls_visible()

    sub_a.close()
    qtbot.waitUntil(lambda: win._maximized_sub is None, timeout=2000)


def test_restore_hides_controls(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    _win, wm = _build(qtbot, tmp_path)

    id_a = wm.open("trace")
    sub_a = _sub_for(wm, id_a)
    header_a = _header_for(wm, id_a)

    with qtbot.waitSignal(wm.subwindow_maximized, timeout=2000):
        sub_a.showMaximized()
    assert header_a.window_controls_visible()

    with qtbot.waitSignal(wm.subwindow_restored, timeout=2000):
        sub_a.showNormal()
    assert not header_a.window_controls_visible()
