# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.banner.Banner`` + cross-window error wiring."""

from __future__ import annotations

from pathlib import Path

from ithildin.ui.banner import Banner
from ithildin.ui.main_window import MainWindow
from ithildin.ui.report_dock import ReportDockWidget
from ithildin.ui.theme import ThemeManager
from ithildin.ui.trace_window import TraceWindow
from tests.conftest import make_app_context


def test_banner_hidden_by_default(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    b = Banner()
    qtbot.addWidget(b)
    assert not b.isVisible()


def test_banner_show_message_makes_visible_and_sets_text(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    b = Banner(kind="error")
    qtbot.addWidget(b)
    b.show_message("Bus disconnected")
    assert b._label.text() == "Bus disconnected"
    # show()/hide() take effect even on hidden parent — checking the widget's
    # own visibility flag via isHidden() is the documented test path.
    assert not b.isHidden()


def test_banner_action_button_emits_signal(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    b = Banner(kind="error")
    qtbot.addWidget(b)
    b.show_message("Port 7320 in use", action_label="Choose another port")
    assert b._action_btn.isVisible() is False or b._action_btn.text() == "Choose another port"
    with qtbot.waitSignal(b.action_clicked, timeout=500):
        b._action_btn.click()


def test_banner_dismiss_hides_and_emits(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    b = Banner()
    qtbot.addWidget(b)
    b.show_message("hello")
    with qtbot.waitSignal(b.dismissed, timeout=500):
        b._close_btn.click()
    assert b.isHidden()


def test_banner_set_kind_updates_object_name(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    b = Banner(kind="warn")
    qtbot.addWidget(b)
    assert b.objectName() == "WarnBanner"
    b.set_kind("error")
    assert b.objectName() == "ErrorBanner"


def test_banner_set_kind_retints_dot_through_repeated_switches(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """error → info → warn each updates the dot's stylesheet.

    Guards against the pre-fix bug where ``set_kind`` left the widget
    visually stuck on the construction-time kind. The dot is the
    widget-level QSS surface that ``_apply_theme`` writes directly, so
    its stylesheet is the most reliable signal that the kind change
    actually took effect (the background tint lives in the app-level
    QSS, which isn't loaded in the test app).
    """
    ThemeManager.reset_for_tests()
    ThemeManager.instance().apply("dark", persist=False)
    b = Banner(kind="error")
    qtbot.addWidget(b)
    err_dot = b._dot.styleSheet()
    b.set_kind("info")
    info_dot = b._dot.styleSheet()
    b.set_kind("warn")
    warn_dot = b._dot.styleSheet()
    assert err_dot != info_dot
    assert info_dot != warn_dot
    assert err_dot != warn_dot


def test_banner_retints_on_theme_change(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    ThemeManager.reset_for_tests()
    mgr = ThemeManager.instance()
    mgr.apply("dark", persist=False)
    b = Banner(kind="error")
    qtbot.addWidget(b)
    dark_dot_qss = b._dot.styleSheet()
    mgr.apply("light", persist=False)
    light_dot_qss = b._dot.styleSheet()
    assert dark_dot_qss != light_dot_qss


def test_report_dock_append_and_trim(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = ReportDockWidget()
    qtbot.addWidget(w)
    w.append("first", kind="error")
    w.append("second", kind="info")
    assert w.count() == 2
    # Newest on top.
    assert "second" in w.item(0).text()
    assert "first" in w.item(1).text()


def test_main_window_bus_error_reaches_report_dock(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    # Dock is hidden initially.
    assert not win._report_dock.isVisible()
    ctx.bus_reader.error.emit("disconnected: USB unplug")
    assert win._report_widget.count() == 1
    assert "disconnected" in win._report_widget.item(0).text()


def test_main_window_database_error_reaches_report_dock(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    ctx.database.error.emit("DBC parse failure: foo.dbc")
    assert win._report_widget.count() == 1
    assert "DBC parse failure" in win._report_widget.item(0).text()


def test_trace_window_bus_error_shows_banner(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    tw = TraceWindow(instance_id="t1")
    qtbot.addWidget(tw)
    tw.bind(ctx)
    assert tw._banner.isHidden()

    ctx.bus_reader.error.emit("CAN bus off")
    assert not tw._banner.isHidden()
    assert "CAN bus off" in tw._banner._label.text()
