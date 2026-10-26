# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Hot-plug: the dock rescans adapters, tells a failed bus from a stopped one,
connects and disconnects from a row's status dot, pins a bus to an adapter,
and says when a lost adapter is back without ever reopening its bus; the main
window turns Windows device changes into one rescan."""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import can
import pytest
from can.interfaces.pcan import basic
from PySide6.QtCore import QByteArray, QEvent, QItemSelectionModel, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QStandardItem
from PySide6.QtWidgets import QApplication, QMenu

from ithildin.app_state.project import new_unsaved
from ithildin.core import bus as bus_mod
from ithildin.ui import adapter_prober as prober_mod
from ithildin.ui import create_bus_dialog as dialog_mod
from ithildin.ui import left_dock as dock_mod
from ithildin.ui import main_window as main_window_mod
from ithildin.ui.left_dock import BusTreeView, LeftDockWidget, _BusRow
from ithildin.ui.main_window import MainWindow
from ithildin.workspace.schema import BusConfig
from tests.conftest import kill_capture, make_app_context
from tests.fake_bus import use_fake_bus

_BACK = "Adapter for bus Engine is back — click its status dot in Buses to reconnect."


class _Attached:
    """What is plugged in, as ``adapter_prober._detect_configs`` reports it.

    ``configs[backend]`` is read when a probe of that backend starts;
    ``gates[backend]`` then holds the probe until set.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.configs: dict[str, list[dict[str, object]]] = {}
        self.calls: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        monkeypatch.setattr(prober_mod, "_detect_configs", self._detect)

    def _detect(self, backend: str) -> list[dict[str, object]]:
        self.calls.append(backend)
        configs = list(self.configs.get(backend, []))
        gate = self.gates.get(backend)
        if gate is not None:
            gate.wait(5)
        return configs

    def release(self) -> None:
        for gate in self.gates.values():
            gate.set()


@pytest.fixture
def attached(monkeypatch: pytest.MonkeyPatch) -> _Attached:
    monkeypatch.delenv("ITHILDIN_DEV", raising=False)
    return _Attached(monkeypatch)


@pytest.fixture
def make_dock(
    qtbot, tmp_path: Path, attached: _Attached
) -> Iterator[Callable[..., LeftDockWidget]]:  # type: ignore[no-untyped-def]
    def make(*buses: BusConfig, saved: bool = False) -> LeftDockWidget:
        project = new_unsaved(user_root=tmp_path / "users")
        if saved:
            project.set_path(tmp_path / "bench.itp")
        project.workspace.bus = list(buses)
        dock = LeftDockWidget()
        qtbot.addWidget(dock)
        dock.bind(make_app_context(tmp_path, project=project))
        return dock

    yield make
    # pytest-qt has deleted the docks by now, but their probes run on, and
    # must finish before monkeypatch brings back the real vendor detection.
    attached.release()
    qtbot.waitUntil(lambda: not prober_mod._INFLIGHT_PROBES, timeout=10000)


def _rescan(dock: LeftDockWidget, qtbot) -> None:  # type: ignore[no-untyped-def]
    dock.rescan_adapters()
    qtbot.waitUntil(lambda: dock._prober is None, timeout=5000)


def _row(dock: LeftDockWidget, name: str) -> _BusRow:
    assert dock._bus_tree is not None
    row = dock._bus_tree.bus_row(name)
    assert row is not None
    return row


def _item(dock: LeftDockWidget, name: str) -> QStandardItem:
    assert dock._bus_tree is not None
    model = dock._bus_tree._tree_model
    (item,) = [
        model.item(r)
        for r in range(model.rowCount())
        if model.item(r).data(dock_mod._BUS_ROW_ROLE).bus_name == name
    ]
    return item


def _pcan(channel: str, device_id: int) -> dict[str, object]:
    return {
        "interface": "pcan",
        "channel": channel,
        "device_id": device_id,
        "controller_number": 0,
        "device_name": "PCAN-USB",
    }


def _engine(**update: object) -> BusConfig:
    cfg = BusConfig(name="Engine", interface="pcan", channel="PCAN_USBBUS1", bitrate=500_000)
    return cfg.model_copy(update=update)


def _virtual(name: str, **update: object) -> BusConfig:
    cfg = BusConfig(
        name=name, interface="virtual", channel=f"hp-{uuid.uuid4().hex[:8]}", bitrate=500_000
    )
    return cfg.model_copy(update=update)


def _body(**update: object) -> BusConfig:
    cfg = BusConfig(name="Body", interface="kvaser", channel=0, bitrate=500_000)
    return cfg.model_copy(update=update)


def _kvaser() -> dict[str, object]:
    return {"interface": "kvaser", "channel": 0, "serial": 84621, "dongle_channel": 1}


class _Menus:
    """Stands in for the Buses context menu: records each menu's action texts
    instead of showing it, and triggers the action named ``pick``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.shown: list[list[str]] = []
        self.pick: str | None = None
        menus = self

        class _Menu(QMenu):
            def exec(self, *_args: object) -> None:  # type: ignore[override]
                actions = [a for a in self.actions() if not a.isSeparator()]
                menus.shown.append([a.text() for a in actions])
                for action in actions:
                    if action.text() == menus.pick:
                        action.trigger()

        monkeypatch.setattr(dock_mod, "QMenu", _Menu)


# ---- Rescanning ----


def test_rescan_follows_an_adapter_coming_and_going(qapp, qtbot, attached, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock(_engine())
    dock._rescan_btn.click()
    qtbot.waitUntil(lambda: dock._prober is None, timeout=5000)
    assert _row(dock, "Engine").state == "unavailable"

    attached.configs["pcan"] = [_pcan("PCAN_USBBUS1", 0xFF)]
    _rescan(dock, qtbot)
    assert _row(dock, "Engine").state == "available"

    attached.configs["pcan"] = []
    _rescan(dock, qtbot)
    assert _row(dock, "Engine").state == "unavailable"


def test_rescans_during_a_probe_merge_into_one_more(qapp, qtbot, attached, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock()
    attached.gates["pcan"] = threading.Event()
    dock.rescan_adapters()
    qtbot.waitUntil(lambda: attached.calls.count("pcan") == 1, timeout=2000)

    dock.rescan_adapters()
    dock.rescan_adapters()
    attached.gates["pcan"].set()
    qtbot.waitUntil(lambda: dock._prober is None, timeout=5000)
    assert attached.calls.count("pcan") == 2


def test_a_rescan_never_flashes_a_bus_unavailable(qapp, qtbot, attached, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock(BusConfig(name="Body", interface="kvaser", channel=0, bitrate=500_000))
    attached.configs["kvaser"] = [
        {"interface": "kvaser", "channel": 0, "serial": 84621, "dongle_channel": 1}
    ]
    _rescan(dock, qtbot)
    assert _row(dock, "Body").state == "available"

    # pcan is probed before kvaser; hold the probe there.
    attached.gates["pcan"] = threading.Event()
    dock.rescan_adapters()
    qtbot.waitUntil(lambda: attached.calls.count("pcan") == 2, timeout=2000)
    assert _row(dock, "Body").state == "available"


def test_a_timed_out_backend_is_skipped_from_then_on(
    qapp, qtbot, monkeypatch, attached, make_dock
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(dock_mod, "AdapterProber", partial(prober_mod.AdapterProber, timeout_s=0.1))
    dock = make_dock()
    attached.gates["pcan"] = threading.Event()

    _rescan(dock, qtbot)
    assert dock._hung_backends == {"pcan"}
    assert dock._failed_backends_label is not None
    assert "pcan: timed out — restart to retry" in dock._failed_backends_label.text()

    _rescan(dock, qtbot)
    assert attached.calls.count("pcan") == 1
    assert "pcan: timed out" in dock._failed_backends_label.text()


def test_a_backend_that_hung_in_the_dialog_loses_its_rows(qapp, qtbot, attached, make_dock) -> None:  # type: ignore[no-untyped-def]
    """No probe replaces its rows any more, so they would stay for good."""
    dock = make_dock(_body())
    attached.configs["kvaser"] = [_kvaser()]
    _rescan(dock, qtbot)
    assert _row(dock, "Body").state == "available"

    # What the Create dialog's prober does to the set the dock shares with it.
    dock._hung_backends.add("kvaser")
    _rescan(dock, qtbot)

    assert attached.calls.count("kvaser") == 1
    assert _row(dock, "Body").state == "unavailable"


def test_a_rescan_keeps_the_selected_bus(qapp, qtbot, attached, make_dock) -> None:  # type: ignore[no-untyped-def]
    """Any device change on the PC rescans; the "+ DBC" target must survive it."""
    dock = make_dock(_engine(), _body())
    tree = dock._bus_tree
    tree.selectionModel().select(
        tree.model().index(1, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    assert dock._dbc_load_btn.isEnabled()

    attached.configs["pcan"] = [_pcan("PCAN_USBBUS1", 0xFF)]
    _rescan(dock, qtbot)

    assert _row(dock, "Engine").state == "available"
    assert dock._selected_bus_name == "Body"
    assert dock._dbc_load_btn.isEnabled()


def test_add_bus_shares_the_hung_backends_and_every_key_with_the_dialog(
    qapp, qtbot, monkeypatch, make_dock
) -> None:  # type: ignore[no-untyped-def]
    passed: dict[str, object] = {}

    class _Dialog:
        def __init__(self, **kwargs: object) -> None:
            passed.update(kwargs)
            self.bus_declared = SimpleNamespace(connect=lambda _slot: None)
            self.bus_declared_and_connect = SimpleNamespace(connect=lambda _slot: None)

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(dialog_mod, "CreateCanBusDialog", _Dialog)
    dock = make_dock(_engine(hw_id="0000002a.0"), _body())

    dock._add_bus_btn.click()

    assert passed["hung_backends"] is dock._hung_backends
    assert passed["existing_names"] == {"Engine", "Body"}
    assert passed["existing_keys"] == {
        ("pcan", "0000002a.0", None),
        ("pcan", None, "PCAN_USBBUS1"),
        ("kvaser", None, "0"),
    }


# ---- Failed, stopped ----


def test_a_failed_bus_looks_different_from_a_stopped_one(qapp, qtbot, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock(_virtual("A"), _virtual("B"))
    reader = dock._ctx.bus_reader
    for cfg in dock._ctx.project.workspace.bus:
        with qtbot.waitSignal(reader.bus_started, timeout=2000):
            reader.open(cfg)

    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        kill_capture(reader, "A")
    reader.close("B")

    assert _row(dock, "A").state == "failed"
    assert _item(dock, "A").text().startswith("⊗  A")
    assert reader.failures()["A"] in _item(dock, "A").toolTip()
    assert _row(dock, "B").state == "stopped"
    assert _item(dock, "B").text().startswith("◌  B")


def test_every_failure_is_kept(qapp, qtbot, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock(_virtual("A"), _virtual("B"))
    reader = dock._ctx.bus_reader
    for cfg in dock._ctx.project.workspace.bus:
        with qtbot.waitSignal(reader.bus_started, timeout=2000):
            reader.open(cfg)
    for name in ("A", "B"):
        with qtbot.waitSignal(reader.bus_failed, timeout=2000):
            kill_capture(reader, name)

    assert list(reader.failures()) == ["A", "B"]
    assert [_row(dock, n).state for n in ("A", "B")] == ["failed", "failed"]


# ---- The status dot ----


def _tree_with(  # type: ignore[no-untyped-def]
    qtbot, state: str, dbcs: tuple[Path, ...] = ()
) -> tuple[BusTreeView, list[str], list[str], QPoint, QPoint]:
    tree = BusTreeView()
    qtbot.addWidget(tree)
    tree.resize(320, 160)
    tree.show()
    qtbot.waitExposed(tree)
    tree.set_rows([(_BusRow("A", "virtual", "x", state, accepts_drops=True), list(dbcs))])  # type: ignore[arg-type]
    connects: list[str] = []
    disconnects: list[str] = []
    tree.connect_requested.connect(connects.append)
    tree.disconnect_requested.connect(disconnects.append)
    rect = tree.visualRect(tree.model().index(0, 0))
    dot = QPoint(rect.left() + 4, rect.center().y())
    name = QPoint(rect.left() + 80, rect.center().y())
    return tree, connects, disconnects, dot, name


def test_clicking_the_dot_connects_and_disconnects(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    tree, connects, disconnects, dot, name = _tree_with(qtbot, "available")

    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=name)
    assert connects == []
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=dot)
    assert connects == ["A"]

    tree.set_rows([(_BusRow("A", "virtual", "x", "live", accepts_drops=True), [])])
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=dot)
    assert disconnects == ["A"]


def test_a_closing_bus_ignores_the_dot(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    tree, connects, disconnects, dot, _name = _tree_with(qtbot, "closing")
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=dot)
    assert connects == disconnects == []


def test_the_expand_arrow_expands_and_never_toggles_the_bus(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """Left of the dot, the arrow: ``indexAt`` maps it to the row as well."""
    tree, connects, disconnects, dot, _name = _tree_with(qtbot, "live", (Path("body.dbc"),))
    index = tree.model().index(0, 0)
    arrow = QPoint(tree.visualRect(index).left() // 2, dot.y())
    assert tree.indexAt(arrow) == index
    assert tree.isExpanded(index)

    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=arrow)

    assert not tree.isExpanded(index)
    assert connects == disconnects == []


def test_double_clicking_the_dot_toggles_once(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    tree, connects, _disconnects, dot, _name = _tree_with(qtbot, "stopped")
    viewport = tree.viewport()

    # As the platform delivers it: press, release, double-click, release.
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=dot)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=dot)
    double = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(dot),
        QPointF(viewport.mapToGlobal(dot)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, double)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=dot)

    assert connects == ["A"]


@pytest.mark.parametrize(
    ("state", "actions"),
    [
        ("unavailable", ["Connect"]),
        ("available", ["Connect"]),
        ("stopped", ["Connect"]),
        ("failed", ["Connect"]),
        ("lost", ["Connect"]),
        ("opening", ["Disconnect"]),
        ("live", ["Disconnect"]),
        ("errors", ["Reconnect", "Disconnect"]),
        ("bus_off", ["Reconnect", "Disconnect"]),
        ("closing", []),
    ],
)
def test_the_context_menu_offers_what_the_state_allows(
    qapp, qtbot, monkeypatch, state: str, actions: list[str]
) -> None:  # type: ignore[no-untyped-def]
    menus = _Menus(monkeypatch)
    tree, _connects, _disconnects, _dot, name = _tree_with(qtbot, state)
    tree._on_context_menu(name)
    assert menus.shown == [[*actions, "Remove A"]]


def test_the_context_menu_offers_a_pin(qapp, qtbot, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    menus = _Menus(monkeypatch)
    menus.pick = "Pin to Kvaser Leaf Light v2 ch 1"
    tree, _connects, _disconnects, _dot, name = _tree_with(qtbot, "stopped")
    pins: list[str] = []
    tree.pin_requested.connect(pins.append)
    tree.set_rows(
        [(_BusRow("A", "kvaser", 0, "stopped", accepts_drops=True, pin_label=menus.pick), [])]
    )

    tree._on_context_menu(name)

    assert menus.shown == [["Connect", menus.pick, "Remove A"]]
    assert pins == ["A"]


def test_reconnect_reopens_a_degraded_bus_from_its_workspace_config(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, make_dock
) -> None:
    monkeypatch.setattr(bus_mod, "_HEALTH_TICK_S", 0.1)
    fake = use_fake_bus(monkeypatch)
    menus = _Menus(monkeypatch)
    dock = make_dock(_engine())
    project = dock._ctx.project
    reader = dock._ctx.bus_reader
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(project.workspace.bus[0])
    # Pinned since it opened: the remembered config has no pin.
    project.workspace.bus = [project.workspace.bus[0].model_copy(update={"hw_id": "0000002a.0"})]
    fake.status_code = basic.PCAN_ERROR_BUSPASSIVE
    qtbot.waitUntil(lambda: _row(dock, "Engine").state == "errors", timeout=3000)
    reopened: list[BusConfig] = []
    monkeypatch.setattr(reader, "reopen", reopened.append)

    menus.pick = "Reconnect"
    tree = dock._bus_tree
    tree._on_context_menu(tree.visualRect(tree.model().index(0, 0)).center())

    assert menus.shown == [["Reconnect", "Disconnect", "Remove Engine"]]
    assert reopened == [project.workspace.bus[0]]
    assert reopened[0].hw_id == "0000002a.0"


def test_connect_opens_the_workspace_config_with_its_pin(qapp, qtbot, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock(_virtual("A", hw_id="pin-a"))
    reader = dock._ctx.bus_reader

    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        dock._bus_tree.connect_requested.emit("A")
    cfg = reader.last_config_for("A")
    assert cfg is not None and cfg.hw_id == "pin-a"

    dock._bus_tree.disconnect_requested.emit("A")
    assert reader.bus_names() == []
    assert _row(dock, "A").state == "stopped"


# ---- An adapter coming back ----


@pytest.fixture
def lost_engine(qtbot, monkeypatch, attached, make_dock):  # type: ignore[no-untyped-def]
    """A pinned PCAN bus, open on its adapter, and a way to pull that adapter."""
    monkeypatch.setattr(bus_mod, "_HEALTH_TICK_S", 0.1)
    fake = use_fake_bus(monkeypatch)
    dock = make_dock(_engine(hw_id="0000002a.0"))
    reports: list[tuple[str, str]] = []
    dock._ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))
    attached.configs["pcan"] = [_pcan("PCAN_USBBUS1", 0x2A)]
    _rescan(dock, qtbot)
    reader = dock._ctx.bus_reader
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(dock._ctx.project.workspace.bus[0])

    def pull() -> None:
        attached.configs["pcan"] = []
        with qtbot.waitSignal(reader.bus_failed, timeout=2000):
            fake.status_code = basic.PCAN_ERROR_ILLHW

    return dock, reports, pull


def test_lost_adapter_back_is_reported_once_and_never_reopens_the_bus(  # type: ignore[no-untyped-def]
    qapp, qtbot, attached, lost_engine
) -> None:
    dock, reports, pull = lost_engine
    reader = dock._ctx.bus_reader
    pull()
    # The loss started a rescan; the adapter is gone from it.
    qtbot.waitUntil(lambda: dock._prober is None, timeout=5000)
    assert reports == []
    assert _row(dock, "Engine").state == "lost"

    # Back, on another channel.
    attached.configs["pcan"] = [_pcan("PCAN_USBBUS2", 0x2A)]
    _rescan(dock, qtbot)
    assert reports == [(_BACK, "info")]
    assert "Adapter is back" in _item(dock, "Engine").toolTip()
    assert _row(dock, "Engine").channel == "PCAN_USBBUS2"

    _rescan(dock, qtbot)
    qtbot.wait(300)
    assert reports == [(_BACK, "info")]
    assert reader.bus_names() == []


def test_a_probe_started_before_the_loss_does_not_count(qapp, qtbot, attached, lost_engine) -> None:  # type: ignore[no-untyped-def]
    dock, reports, pull = lost_engine
    attached.gates["pcan"] = threading.Event()
    dock.rescan_adapters()
    # Reads the adapter as attached, then holds.
    qtbot.waitUntil(lambda: attached.calls.count("pcan") == 2, timeout=2000)

    pull()
    attached.gates["pcan"].set()
    qtbot.waitUntil(lambda: dock._prober is None, timeout=5000)
    assert reports == []


def test_a_refused_pinned_bus_waits_for_its_adapter(
    qapp, qtbot, monkeypatch, attached, make_dock
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        can, "detect_available_configs", lambda interfaces: attached.configs.get(interfaces[0], [])
    )
    dock = make_dock(_engine(hw_id="0000002a.0"))
    reports: list[tuple[str, str]] = []
    dock._ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))
    reader = dock._ctx.bus_reader

    with qtbot.waitSignal(reader.bus_failed, timeout=5000):
        dock._bus_tree.connect_requested.emit("Engine")
    assert _row(dock, "Engine").state == "failed"
    qtbot.waitUntil(lambda: dock._prober is None, timeout=5000)
    assert reports == []

    attached.configs["pcan"] = [_pcan("PCAN_USBBUS1", 0x2A)]
    _rescan(dock, qtbot)
    assert reports == [(_BACK, "info")]


def test_a_probe_running_at_the_loss_never_vouches_for_another_backend(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, attached, make_dock
) -> None:
    """That probe puts the Kvaser row from before the loss back; the next
    probe's pcan results, taken first, must not read it as the adapter back."""
    fake = use_fake_bus(monkeypatch)
    dock = make_dock(_body(hw_id="84621.1"))
    reports: list[tuple[str, str]] = []
    dock._ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))
    attached.configs["kvaser"] = [_kvaser()]
    _rescan(dock, qtbot)
    reader = dock._ctx.bus_reader
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(dock._ctx.project.workspace.bus[0])
    attached.gates["kvaser"] = threading.Event()
    dock.rescan_adapters()
    # Reads the adapter as attached, then holds.
    qtbot.waitUntil(lambda: attached.calls.count("kvaser") == 2, timeout=2000)

    attached.configs["kvaser"] = []
    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        fake.recv_exc = can.CanOperationError("Kvaser adapter gone", error_code=-15)
    attached.gates["kvaser"].set()
    qtbot.waitUntil(
        lambda: dock._prober is None and attached.calls.count("kvaser") == 3, timeout=5000
    )

    assert reports == []
    assert _row(dock, "Body").state == "lost"
    assert "Adapter is back" not in _item(dock, "Body").toolTip()


def test_no_back_notice_for_a_failure_whose_adapter_stayed(  # type: ignore[no-untyped-def]
    qapp, qtbot, monkeypatch, attached, make_dock
) -> None:
    fake = use_fake_bus(monkeypatch)
    dock = make_dock(_engine(hw_id="0000002a.0"))
    reports: list[tuple[str, str]] = []
    dock._ctx.events.report_requested.connect(lambda msg, kind: reports.append((msg, kind)))
    attached.configs["pcan"] = [_pcan("PCAN_USBBUS1", 0x2A)]
    _rescan(dock, qtbot)
    reader = dock._ctx.bus_reader
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(dock._ctx.project.workspace.bus[0])

    with qtbot.waitSignal(reader.bus_failed, timeout=2000):
        fake.recv_exc = OSError("driver crashed")
    assert dock._prober is None
    _rescan(dock, qtbot)

    assert reports == []
    assert _row(dock, "Engine").state == "failed"
    assert "Adapter is back" not in _item(dock, "Engine").toolTip()


# ---- Pinning ----


def test_pin_a_legacy_bus_to_the_adapter_on_its_channel(qapp, qtbot, attached, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock(_engine(), saved=True)
    project = dock._ctx.project
    attached.configs["pcan"] = [_pcan("PCAN_USBBUS1", 0x2A)]
    _rescan(dock, qtbot)
    assert _row(dock, "Engine").pin_label == (
        "Pin to PEAK-System · PCAN-USB  (device ID 0x2A) PCAN_USBBUS1"
    )

    dock._bus_tree.pin_requested.emit("Engine")

    assert project.workspace.bus[0].hw_id == "0000002a.0"
    assert json.loads(project.adapter_pins_path.read_text(encoding="utf-8")) == {
        "Engine": "0000002a.0"
    }
    assert _row(dock, "Engine").pin_label is None


def test_pin_refuses_an_adapter_another_bus_is_pinned_to(qapp, qtbot, attached, make_dock) -> None:  # type: ignore[no-untyped-def]
    body = BusConfig(
        name="Body", interface="pcan", channel="PCAN_USBBUS2", bitrate=500_000, hw_id="0000002a.0"
    )
    dock = make_dock(_engine(), body)
    errors: list[str] = []
    dock._ctx.database.error.connect(errors.append)
    # Body's adapter now enumerates on Engine's channel.
    attached.configs["pcan"] = [_pcan("PCAN_USBBUS1", 0x2A)]
    _rescan(dock, qtbot)

    dock._bus_tree.pin_requested.emit("Engine")

    assert dock._ctx.project.workspace.bus[0].hw_id is None
    assert errors == ["Can't pin bus 'Engine': bus 'Body' is pinned to that adapter."]


def test_declaring_a_bus_on_a_pinned_adapter_is_refused(qapp, qtbot, make_dock) -> None:  # type: ignore[no-untyped-def]
    dock = make_dock(_engine(hw_id="0000002a.0"), saved=True)
    errors: list[str] = []
    dock._ctx.database.error.connect(errors.append)

    declared = dock._on_bus_declared(
        BusConfig(
            name="Body",
            interface="pcan",
            channel="PCAN_USBBUS2",
            bitrate=500_000,
            hw_id="0000002a.0",
        )
    )

    assert not declared
    assert [b.name for b in dock._ctx.project.workspace.bus] == ["Engine"]
    assert errors == ["Bus 'Engine' already uses that pcan channel or adapter; pick another."]
    assert dock._ctx.project.path is not None and not dock._ctx.project.path.exists()


# ---- Device changes ----


@pytest.fixture
def window(qtbot, tmp_path: Path, monkeypatch) -> tuple[MainWindow, list[int]]:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    win = MainWindow(bus_reader=ctx.bus_reader, database=ctx.database, prompt_on_close=False)
    qtbot.addWidget(win)
    win.bind_events(ctx)
    rescans: list[int] = []
    monkeypatch.setattr(win._left_dock_widget, "rescan_adapters", lambda: rescans.append(1))
    return win, rescans


def test_a_burst_of_device_changes_rescans_once(qapp, qtbot, monkeypatch, window) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(main_window_mod, "_DEVICE_SETTLE_MS", 100)
    win, rescans = window

    for _ in range(5):
        win._on_device_change(0x0007)
    # DBT_DEVICEARRIVAL: never sent for USB devices to an unregistered window.
    win._on_device_change(0x8000)
    qtbot.waitUntil(lambda: rescans == [1], timeout=2000)
    qtbot.wait(300)
    assert rescans == [1]


def test_a_device_change_storm_still_rescans(qapp, qtbot, monkeypatch, window) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(main_window_mod, "_DEVICE_SETTLE_MS", 200)
    monkeypatch.setattr(main_window_mod, "_DEVICE_MAX_WAIT_MS", 500)
    win, rescans = window

    started = time.monotonic()
    while not rescans and time.monotonic() - started < 3:
        win._on_device_change(0x0007)
        qtbot.wait(50)
    assert rescans
    assert time.monotonic() - started < 1.5


@pytest.mark.skipif(sys.platform != "win32", reason="WM_DEVICECHANGE is a Windows message")
def test_windows_device_change_message_reaches_the_rescan(qapp, qtbot, monkeypatch, window) -> None:  # type: ignore[no-untyped-def]
    """Through the override itself: offscreen Qt has no native window to send to.
    Qt hands ``message`` over as a ``VoidPtr``, and only takes that back."""
    import ctypes
    from ctypes import wintypes

    from shiboken6 import VoidPtr

    monkeypatch.setattr(main_window_mod, "_DEVICE_SETTLE_MS", 50)
    win, rescans = window
    event_type = QByteArray(b"windows_generic_MSG")
    mouse_move = wintypes.MSG(message=0x0200, wParam=0x0007)
    device_change = wintypes.MSG(message=0x0219, wParam=0x0007)

    assert win.nativeEvent(event_type, VoidPtr(ctypes.addressof(mouse_move))) == (False, 0)
    assert win.nativeEvent(event_type, VoidPtr(ctypes.addressof(device_change))) == (False, 0)

    qtbot.waitUntil(lambda: rescans == [1], timeout=2000)
    qtbot.wait(200)
    assert rescans == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="the hook exists on Windows")
def test_no_native_event_hook_off_windows() -> None:
    assert "nativeEvent" not in MainWindow.__dict__
