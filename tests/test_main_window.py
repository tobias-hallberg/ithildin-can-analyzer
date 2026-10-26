# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Smoke tests for ``ui.main_window.MainWindow`` toolbar wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFileDialog, QMessageBox, QToolBar

from ithildin.app_state.project import Project, new_unsaved
from ithildin.ui.main_window import MainWindow
from ithildin.ui.theme import TOKENS, ThemeManager
from ithildin.workspace.loader import load_workspace, save_workspace
from ithildin.workspace.schema import BusConfig, DbcRef, Workspace
from tests.conftest import make_app_context


def test_toolbar_uses_mellon_style_icon_size(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)

    toolbars = win.findChildren(QToolBar)
    assert toolbars, "expected a toolbar on MainWindow"
    tb = toolbars[0]
    assert tb.iconSize() == QSize(24, 24)
    assert tb.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon


def test_launcher_buttons_carry_icon_metadata(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)

    # Each tuple is (kind, label, icon_name) — the icon_name resolves to
    # a bundled SVG and the button shows a non-null icon once installed.
    expected_kinds = {"trace", "signal_plot", "signal_monitor", "data_view", "transmit", "record"}
    expected_icons = {"message_trace", "plot", "monitor", "data_view", "transmit", "record"}
    seen_kinds = {kind for kind, _label, _icon in win._launcher_buttons}
    seen_icons = {icon for _kind, _label, icon in win._launcher_buttons}
    assert seen_kinds == expected_kinds
    assert seen_icons == expected_icons


def test_pane_toggle_button_hides_and_shows_left_dock(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)

    btn = win._toggle_left_btn
    dock = win._left_dock
    assert dock.isVisible()

    btn.click()
    assert not dock.isVisible()

    btn.click()
    assert dock.isVisible()


def test_bottom_toggle_button_hides_and_shows_report_dock(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)

    btn = win._toggle_bottom_btn
    dock = win._report_dock
    assert not dock.isVisible()

    btn.click()
    assert dock.isVisible()

    btn.click()
    assert not dock.isVisible()


def test_pane_toggle_is_last_toolbar_widget(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)

    tb = win._toolbar
    actions = tb.actions()
    # Pane-toggle QToolButtons sit at the right end of the toolbar:
    # left-pane toggle, then bottom-pane toggle.
    assert tb.widgetForAction(actions[-2]) is win._toggle_left_btn
    assert tb.widgetForAction(actions[-1]) is win._toggle_bottom_btn


def test_save_project_with_no_project_prompts_for_path(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    # Fresh app: project is always set, but unsaved.
    assert isinstance(win._current_project, Project)
    assert not win._current_project.is_saved

    target = tmp_path / "demo.itp"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(target), "Ithildin project (*.itp)"),
    )

    win._save_project()

    assert target.exists()
    workspace = load_workspace(target)
    assert workspace.schema_version == 2
    assert win._current_project.is_saved
    assert win._current_project.path == target.resolve()


def test_save_project_uses_existing_path_without_prompt(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    target = tmp_path / "demo.itp"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(target), "Ithildin project (*.itp)"),
    )
    win._save_project()
    first_mtime = target.stat().st_mtime_ns

    def _refuse(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Save dialog must not appear once project has a path")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", _refuse)
    win._save_project()
    assert target.exists()
    # Re-saved in place — same path.
    assert win._current_project is not None
    assert win._current_project.path == target.resolve()
    # mtime updated (may be equal on coarse-resolution filesystems; stat call
    # at minimum confirms the file is still there).
    assert target.stat().st_mtime_ns >= first_mtime


def test_open_project_loads_dbc_bindings(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    example_dbc_path: Path,
) -> None:
    target = tmp_path / "openme.itp"
    workspace = Workspace(
        name="openme",
        bus=[BusConfig(name="BusA", interface="virtual", channel="vbus", bitrate=500000)],
        dbc=[DbcRef(path=example_dbc_path, buses=["BusA"])],
    )
    save_workspace(workspace, target)

    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(target), "Ithildin project (*.itp *.ithildin)"),
    )

    win._open_project()

    assert win._current_project is not None
    assert win._current_project.path == target.resolve()
    loaded = win._db.loaded_paths()
    assert example_dbc_path in loaded
    assert win._db.buses_for_path(example_dbc_path) == ["BusA"]


def test_new_project_clears_state(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    win._db.add_dbc(example_dbc_path, ["BusA"])
    assert win._db.loaded_paths()

    win._new_project()

    assert win._db.loaded_paths() == []
    assert isinstance(win._current_project, Project)
    assert not win._current_project.is_saved


def test_open_project_with_bad_path_does_not_crash(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    bogus = tmp_path / "does_not_exist.itp"
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(bogus), "Ithildin project (*.itp *.ithildin)"),
    )

    win._open_project()  # must not raise
    # Open failed; the prior unsaved project is still in place.
    assert isinstance(win._current_project, Project)
    assert not win._current_project.is_saved


def test_save_project_when_open_captures_session_changes(  # type: ignore[no-untyped-def]
    qapp,
    qtbot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    example_dbc_path: Path,
) -> None:
    """Saving an already-open project must round-trip live UI state.

    Regression: the old code wrote ``self._current_project._workspace``
    untouched, so DBCs added to the runtime ``Database`` after open were
    silently dropped on Save.
    """
    target = tmp_path / "evolves.itp"
    # Pre-populate with a declared bus so a later DBC binding to it is
    # consistent with the new "buses are user-curated" rule.
    initial = Workspace(
        name="evolves",
        bus=[BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000)],
    )
    save_workspace(initial, target)

    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(target), "Ithildin project (*.itp *.ithildin)"),
    )
    win._open_project()
    assert win._current_project.is_saved

    win._db.add_dbc(example_dbc_path, ["CAN_1"])

    win._save_project()

    reloaded = load_workspace(target)
    assert [d.path for d in reloaded.dbc] == [example_dbc_path]
    assert reloaded.dbc[0].buses == ["CAN_1"]
    assert [b.name for b in reloaded.bus] == ["CAN_1"]


def test_save_project_round_trips_views(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plotted signals / monitor rows / trace filter survive save → re-open."""
    from ithildin.ui.factory import make_factory
    from ithildin.ui.window_manager import WindowManager

    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)

    wm = WindowManager(win.mdi_area, tmp_path / "layout-A.json", make_factory(ctx))
    win.set_window_manager(wm)
    win.bind_events(ctx)

    plot_id = wm.open("signal_plot", params={"name": "P"})
    plot = next(e.view for e in wm.list_windows() if e.window_id == plot_id)
    plot.add_signal("EngineCommand.EngineSpeed")  # type: ignore[attr-defined]

    target = tmp_path / "withviews.itp"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(target), "Ithildin project (*.itp)"),
    )
    win._save_project()

    saved = load_workspace(target)
    plot_views = [v for v in saved.views if v.kind == "signal_plot"]
    assert plot_views, "expected a signal_plot view in the saved workspace"
    assert plot_views[0].state.get("signals") == ["EngineCommand.EngineSpeed"]


def test_open_project_loads_bus_into_workspace(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved bus configs round-trip into project.workspace.bus on open."""
    target = tmp_path / "withbus.itp"
    saved_bus = BusConfig(name="CAN_1", interface="virtual", channel="virtchan", bitrate=250000)
    save_workspace(Workspace(name="withbus", bus=[saved_bus]), target)

    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(target), "Ithildin project (*.itp *.ithildin)"),
    )
    win._open_project()

    bus = win._current_project.workspace.bus
    assert [b.channel for b in bus] == ["virtchan"]
    assert bus[0].bitrate == 250000


def test_connect_action_disabled_with_no_buses(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """Fresh app: workspace.bus is empty, so the Connect action is disabled."""
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    assert win._connect_action.isEnabled() is False
    assert win._connect_action.text() == "Connect"
    assert "Add a bus" in win._connect_action.toolTip()


def test_open_project_propagates_workspace_to_left_dock(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: opening a saved project must surface its bus in the dock.

    Reassigning ``self._current_project = project`` left ``AppContext.project``
    (frozen) pointing at the original launch-time Project, so the left
    dock kept rendering the *old* (empty) workspace and Connect opened
    the wrong bus. ``Project.adopt`` mutates the existing instance so
    every consumer sees the loaded workspace.
    """
    target = tmp_path / "saved.itp"
    saved_bus = BusConfig(name="MyBus", interface="virtual", channel="vchan", bitrate=500000)
    save_workspace(Workspace(name="saved", bus=[saved_bus]), target)

    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    # Sanity: before opening the project, the dock has no buses (the
    # default unsaved project's workspace.bus is empty).
    assert win._left_dock_widget._bus_tree.bus_names() == []
    assert win._connect_action.isEnabled() is False

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(target), "Ithildin project (*.itp *.ithildin)"),
    )
    win._open_project()

    # After open, the dock must show the saved bus, Connect must be
    # enabled, and the same Project instance must be visible through
    # both ``ctx.project`` and ``win._current_project``.
    assert win._left_dock_widget._bus_tree.bus_names() == ["MyBus"]
    assert win._connect_action.isEnabled() is True
    assert ctx.project is win._current_project
    assert [b.name for b in ctx.project.workspace.bus] == ["MyBus"]


def test_connect_action_toggles_on_open_close(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """With a declared bus: Connect opens it; clicking again closes."""
    project = new_unsaved()
    project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="virtchan", bitrate=500000)
    ]
    ctx = make_app_context(tmp_path, project=project)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    assert win._connect_action.isEnabled() is True
    assert win._connect_action.text() == "Connect"

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._on_connect_toggled()

    assert ctx.bus_reader.is_open
    assert win._connect_action.text() == "Disconnect"

    with qtbot.waitSignal(ctx.bus_reader.bus_stopped, timeout=2000):
        win._on_connect_toggled()

    assert not ctx.bus_reader.is_open
    assert win._connect_action.text() == "Connect"


# ---- Close prompt for unsaved changes ----


def _build_window_for_close_test(qtbot, tmp_path: Path) -> MainWindow:  # type: ignore[no-untyped-def]
    # prompt_on_close stays True here: these tests exercise the save prompt
    # itself (QMessageBox.question is monkeypatched, so no modal appears).
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    return win


def test_close_clean_does_not_prompt(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Fresh app with no changes: closeEvent runs without a save prompt."""
    win = _build_window_for_close_test(qtbot, tmp_path)
    assert win._workspace_is_dirty() is False

    prompted: list[bool] = []

    def _refuse(*_args, **_kwargs) -> int:  # type: ignore[no-untyped-def]
        prompted.append(True)
        raise AssertionError("Save prompt must not appear when nothing changed")

    import unittest.mock

    with unittest.mock.patch.object(QMessageBox, "question", side_effect=_refuse):
        event = QCloseEvent()
        win.closeEvent(event)
    assert prompted == []
    assert event.isAccepted()


def test_close_dirty_save_persists_and_quits(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dirty workspace + Save: file is written, close completes."""
    win = _build_window_for_close_test(qtbot, tmp_path)
    win._left_dock_widget._on_bus_declared(
        BusConfig(name="CAN_1", interface="virtual", channel="vchan", bitrate=500000)
    )
    assert win._workspace_is_dirty() is True

    target = tmp_path / "auto-save.itp"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_a, **_k: (str(target), "Ithildin project (*.itp)"),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Save,
    )

    event = QCloseEvent()
    win.closeEvent(event)

    assert target.exists()
    reloaded = load_workspace(target)
    assert [b.name for b in reloaded.bus] == ["CAN_1"]
    assert event.isAccepted()
    assert win._workspace_is_dirty() is False


def test_close_dirty_discard_skips_save(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dirty workspace + Discard: no file written, close completes."""
    win = _build_window_for_close_test(qtbot, tmp_path)
    win._left_dock_widget._on_bus_declared(
        BusConfig(name="CAN_1", interface="virtual", channel="vchan", bitrate=500000)
    )

    saves_attempted: list[object] = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_a, **_k: saves_attempted.append(True) or ("", ""),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Discard,
    )

    event = QCloseEvent()
    win.closeEvent(event)

    assert saves_attempted == []
    assert event.isAccepted()


def test_close_dirty_cancel_aborts_close(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dirty workspace + Cancel: window stays open, no save."""
    win = _build_window_for_close_test(qtbot, tmp_path)
    win._left_dock_widget._on_bus_declared(
        BusConfig(name="CAN_1", interface="virtual", channel="vchan", bitrate=500000)
    )

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Cancel,
    )

    event = QCloseEvent()
    win.closeEvent(event)

    assert not event.isAccepted()
    assert win._workspace_is_dirty() is True


def test_close_dirty_save_canceled_aborts_close(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dirty workspace + Save but Save-As cancelled: close aborts."""
    win = _build_window_for_close_test(qtbot, tmp_path)
    win._left_dock_widget._on_bus_declared(
        BusConfig(name="CAN_1", interface="virtual", channel="vchan", bitrate=500000)
    )

    # Save-As dialog returns empty path → _save_project bails, workspace
    # stays dirty → closeEvent must abort.
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: ("", ""))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Save,
    )

    event = QCloseEvent()
    win.closeEvent(event)

    assert not event.isAccepted()
    assert win._workspace_is_dirty() is True


def test_save_resets_dirty_state(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After save, the workspace is no longer considered dirty."""
    win = _build_window_for_close_test(qtbot, tmp_path)
    win._left_dock_widget._on_bus_declared(
        BusConfig(name="CAN_1", interface="virtual", channel="vchan", bitrate=500000)
    )
    assert win._workspace_is_dirty() is True

    target = tmp_path / "save-reset.itp"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_a, **_k: (str(target), "Ithildin project (*.itp)"),
    )
    win._save_project()
    assert win._workspace_is_dirty() is False


def test_no_prompt_skips_dirty_dialog(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """``prompt_on_close=False``: dirty workspace closes without a dialog."""
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    win._left_dock_widget._on_bus_declared(
        BusConfig(name="CAN_1", interface="virtual", channel="vchan", bitrate=500000)
    )
    assert win._workspace_is_dirty() is True

    import unittest.mock

    def _refuse(*_args, **_kwargs) -> int:  # type: ignore[no-untyped-def]
        raise AssertionError("Save prompt must not appear with prompt_on_close=False")

    with unittest.mock.patch.object(QMessageBox, "question", side_effect=_refuse):
        event = QCloseEvent()
        win.closeEvent(event)
    assert event.isAccepted()


def test_hamburger_theme_submenu_switches_theme(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """Triggering the Light action in the hamburger Theme submenu flips the theme."""
    ThemeManager.reset_for_tests()
    ThemeManager.instance().apply("dark", persist=False)

    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)

    # Initial check state reflects the current ThemeManager choice.
    assert win._theme_actions["dark"].isChecked()
    assert not win._theme_actions["light"].isChecked()

    win._theme_actions["light"].trigger()

    mgr = ThemeManager.instance()
    assert mgr.current_choice() == "light"
    assert win._theme_actions["light"].isChecked()
    assert not win._theme_actions["dark"].isChecked()
    assert TOKENS["light"]["accent"] in qapp.styleSheet()

    # Reset so subsequent tests don't inherit a "light" QApplication palette.
    mgr.apply("dark", persist=False)


def test_no_prompt_skips_capture_dialog_and_closes_bus(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    """``prompt_on_close=False``: active capture is closed silently, no dialog."""
    import uuid

    project = new_unsaved()
    project.workspace.bus = [
        BusConfig(
            name="CAN_1",
            interface="virtual",
            channel=f"np-{uuid.uuid4().hex[:8]}",
            bitrate=500000,
        )
    ]
    ctx = make_app_context(tmp_path, project=project)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._on_connect_toggled()
    assert ctx.bus_reader.is_open

    import unittest.mock

    def _refuse(*_args, **_kwargs) -> int:  # type: ignore[no-untyped-def]
        raise AssertionError("Capture prompt must not appear with prompt_on_close=False")

    with unittest.mock.patch.object(QMessageBox, "question", side_effect=_refuse):
        event = QCloseEvent()
        win.closeEvent(event)
    assert event.isAccepted()
    assert not ctx.bus_reader.is_open


def test_status_bar_and_report_follow_bus_health(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, tmp_path: Path
) -> None:
    from can.interfaces.pcan import basic

    from ithildin.core import bus as bus_module
    from tests.fake_bus import use_fake_bus

    monkeypatch.setattr(bus_module, "_HEALTH_TICK_S", 0.1)
    fake = use_fake_bus(monkeypatch)
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    tokens = ThemeManager.instance().current_tokens()

    def tinted(text: str, token: str) -> str:
        return f'<span style="color: {tokens[token]}">{text}</span>'

    ctx.bus_reader.open(BusConfig(name="CAN_1", interface="pcan", channel="X", bitrate=500000))
    assert win._status_left.text() == "CAN_1 · opening"
    qtbot.waitUntil(lambda: win._status_left.text() == "CAN_1 · LIVE", timeout=2000)

    with qtbot.waitSignal(ctx.bus_reader.bus_health_changed, timeout=2000):
        fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
    assert win._status_left.text() == tinted("CAN_1 · bus errors (error passive)", "warn")
    assert "Bus CAN_1: bus errors (error passive)" in win._report_widget.item(0).text()
    # Info only: a flapping bus must not keep popping the dock open.
    assert win._report_dock.isHidden()
    # A new detail alone adds no entry.
    reported = win._report_widget.count()
    ctx.bus_reader.bus_health_changed.emit("CAN_1", "errors", "error warning")
    assert win._report_widget.count() == reported

    with qtbot.waitSignal(ctx.bus_reader.bus_health_changed, timeout=2000):
        fake.status_code = basic.PCAN_ERROR_BUSOFF
    assert win._status_left.text() == tinted("CAN_1 · bus-off", "err")
    assert "Bus CAN_1: bus-off" in win._report_widget.item(0).text()
    assert not win._report_dock.isHidden()

    with qtbot.waitSignal(ctx.bus_reader.bus_failed, timeout=2000):
        fake.status_code = basic.PCAN_ERROR_ILLHW
    assert win._status_left.text() == tinted("CAN_1 · adapter lost", "err")
    texts = [win._report_widget.item(i).text() for i in range(win._report_widget.count())]
    assert "adapter disconnected" in texts[0]
    assert not any("adapter lost" in text for text in texts)


def test_connect_action_resets_after_timed_out_close(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, tmp_path: Path
) -> None:
    import threading

    from tests.fake_bus import FakeBus, use_fake_bus

    fake = FakeBus()
    fake.block = threading.Event()
    use_fake_bus(monkeypatch, fake)
    project = new_unsaved()
    project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="hung", bitrate=500000)
    ]
    ctx = make_app_context(tmp_path, project=project)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._on_connect_toggled()
    # Hung in recv: a stop requested before that lets the worker exit cleanly.
    qtbot.waitUntil(lambda: fake.recv_calls > 0, timeout=2000)

    try:
        ctx.bus_reader.close(wait_ms=50)
        assert win._connect_action.text() == "Connect"
        tokens = ThemeManager.instance().current_tokens()
        assert win._status_left.text() == (
            f'<span style="color: {tokens["warn"]}">CAN_1 · closing</span>'
        )
    finally:
        with qtbot.waitSignal(ctx.bus_reader.bus_released, timeout=2000):
            fake.block.set()
    assert win._status_left.text() == "Disconnected"


def test_status_bar_keeps_forgotten_closing_bus(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, tmp_path: Path
) -> None:
    import threading

    from tests.fake_bus import FakeBus, use_fake_bus

    fake = FakeBus()
    fake.block = threading.Event()
    use_fake_bus(monkeypatch, fake)
    project = new_unsaved()
    project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="hung", bitrate=500000)
    ]
    ctx = make_app_context(tmp_path, project=project)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._on_connect_toggled()
    # Hung in recv: a stop requested before that lets the worker exit cleanly.
    qtbot.waitUntil(lambda: fake.recv_calls > 0, timeout=2000)

    try:
        ctx.bus_reader.close(wait_ms=50)
        win._new_project()
        assert ctx.bus_reader.last_configs() == []
        tokens = ThemeManager.instance().current_tokens()
        assert win._status_left.text() == (
            f'<span style="color: {tokens["warn"]}">CAN_1 · closing</span>'
        )
    finally:
        with qtbot.waitSignal(ctx.bus_reader.bus_released, timeout=2000):
            fake.block.set()
    assert win._status_left.text() == "Disconnected"


def test_new_project_closes_and_forgets_buses(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path
) -> None:
    import uuid

    project = new_unsaved()
    project.workspace.bus = [
        BusConfig(
            name="CAN_1",
            interface="virtual",
            channel=f"np-{uuid.uuid4().hex[:8]}",
            bitrate=500000,
        )
    ]
    ctx = make_app_context(tmp_path, project=project)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        win._on_connect_toggled()

    win._new_project()

    assert not ctx.bus_reader.is_open
    assert ctx.bus_reader.last_configs() == []
    assert win._status_left.text() == "Disconnected"
    assert win._connect_action.text() == "Connect"


def test_fan_out_skip_reported_as_warning(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)

    ctx.bus_reader.frames_dropped.emit("CAN_1", 1808)
    assert (
        win._report_widget.item(0)
        .text()
        .endswith(
            "Plots, monitors and data views skipped 1808 frames on CAN_1 to catch up after a stall"
        )
    )
    assert not win._report_dock.isHidden()
