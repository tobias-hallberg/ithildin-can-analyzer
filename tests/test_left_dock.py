# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.left_dock`` — drag payload, models, accordion sections."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt

from ithildin.app_state.project import open_or_migrate
from ithildin.core.types import BusHealth
from ithildin.ui._drag_payload import (
    DBC_MIME,
    SIGNAL_MIME,
    DbcPayload,
    MessagePayload,
    SignalPayload,
    pack_into_mime,
    parse_from_mime,
)
from ithildin.ui.left_dock import (
    BusTreeView,
    LeftDockWidget,
    MessagesModel,
    SignalsModel,
)
from tests.conftest import make_app_context


def _make_project(tmp_path: Path, bus_names: list[str], dbc_path: Path | None = None) -> object:
    """Write a minimal v2 .itp with the given buses and open it."""
    body = ["schema_version = 2", 'name = "TestProject"', ""]
    for name in bus_names:
        body += [
            "[[bus]]",
            f'name = "{name}"',
            'interface = "virtual"',
            f'channel = "{name.lower()}"',
            "bitrate = 500000",
            "",
        ]
    if dbc_path is not None:
        body += [
            "[[dbc]]",
            f'path = "{dbc_path.as_posix()}"',
            f"buses = {bus_names}",
            "",
        ]
    p = tmp_path / "test_project.itp"
    p.write_text("\n".join(body), encoding="utf-8")
    project, _report = open_or_migrate(p, user_root=tmp_path / "user-root")
    return project


def test_drag_payload_signal_roundtrip() -> None:
    payload = SignalPayload(
        message="EngineCommand",
        message_id=0x100,
        signal="EngineSpeed",
        unit="rpm",
    )
    mime = pack_into_mime([payload])
    assert mime.hasFormat(SIGNAL_MIME)
    assert parse_from_mime(mime) == [payload]


def test_drag_payload_message_roundtrip() -> None:
    payload = MessagePayload(message="EngineCommand", message_id=0x100)
    assert parse_from_mime(pack_into_mime([payload])) == [payload]


def test_drag_payload_unknown_kind_returns_empty() -> None:
    import json

    from PySide6.QtCore import QMimeData

    mime = QMimeData()
    mime.setData(SIGNAL_MIME, json.dumps([{"kind": "bogus"}]).encode("utf-8"))
    assert parse_from_mime(mime) == []


def test_signals_model_mime_data_packs_payload(qapp) -> None:  # type: ignore[no-untyped-def]
    model = SignalsModel()
    payload = SignalPayload(
        message="EngineCommand", message_id=0x100, signal="EngineSpeed", unit="rpm"
    )
    model.set_rows([(payload, "EngineSpeed", "EngineCommand.EngineSpeed")])
    idx = model.index(0, 0)
    flags = model.flags(idx)
    assert bool(flags & Qt.ItemFlag.ItemIsDragEnabled)
    assert parse_from_mime(model.mimeData([idx])) == [payload]


def test_signals_model_mime_data_packs_multiple(qapp) -> None:  # type: ignore[no-untyped-def]
    """Ctrl/Shift-selecting multiple signals packs them all into one MIME."""
    model = SignalsModel()
    a = SignalPayload(message="M1", message_id=0x1, signal="A")
    b = SignalPayload(message="M2", message_id=0x2, signal="B")
    c = SignalPayload(message="M3", message_id=0x3, signal="C")
    model.set_rows([(p, p.signal, f"{p.message}.{p.signal}") for p in (a, b, c)])
    mime = model.mimeData([model.index(0, 0), model.index(2, 0)])
    assert parse_from_mime(mime) == [a, c]


def test_messages_model_mime_data_packs_payload(qapp) -> None:  # type: ignore[no-untyped-def]
    model = MessagesModel()
    payload = MessagePayload(message="EngineCommand", message_id=0x100)
    model.set_rows([(payload, 8, 100)])
    idx = model.index(0, 0)
    assert parse_from_mime(model.mimeData([idx])) == [payload]


def test_messages_model_mime_data_packs_multiple(qapp) -> None:  # type: ignore[no-untyped-def]
    """Multi-select on the messages list packs every selected row."""
    model = MessagesModel()
    a = MessagePayload(message="M1", message_id=0x1)
    b = MessagePayload(message="M2", message_id=0x2)
    model.set_rows([(a, 8, 100), (b, 4, None)])
    mime = model.mimeData([model.index(0, 0), model.index(1, 0)])
    assert parse_from_mime(mime) == [a, b]


def test_signals_and_messages_views_are_extended_select(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Both dock lists support Ctrl/Shift multi-select."""
    from PySide6.QtWidgets import QAbstractItemView

    ctx = make_app_context(tmp_path)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    assert dock._signals_view.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    assert dock._messages_view.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection


def test_left_dock_populates_from_database(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    from ithildin.workspace.schema import BusConfig

    ctx = make_app_context(tmp_path)
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000)
    ]
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    # Fixture DBC: EngineCommand + BodyStatus + GearState, 5 signals total.
    assert dock._signals_model.rowCount() >= 5
    assert dock._messages_model.rowCount() == 3
    # The DBC lives only as a child row under its bound bus now.
    assert dock._bus_tree.dbcs_for_bus("CAN_1") == [example_dbc_path]


def test_left_dock_filter_narrows_both_lists(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    # "EngineSpeed" only matches the signal — messages list goes empty.
    dock._filter_input.setText("EngineSpeed")
    sig_count = dock._signals_proxy.rowCount()
    msg_count = dock._messages_proxy.rowCount()
    assert sig_count == 1
    assert msg_count == 0


def test_left_dock_case_toggle_flips_proxy_sensitivity(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    assert dock._signals_proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseInsensitive
    assert dock._messages_proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseInsensitive

    # Lowercase search matches case-insensitively.
    dock._filter_input.setText("enginespeed")
    assert dock._signals_proxy.rowCount() == 1

    # Toggle on: the same lowercase pattern no longer matches "EngineSpeed".
    dock._case_action.setChecked(True)
    assert dock._signals_proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseSensitive
    assert dock._messages_proxy.filterCaseSensitivity() == Qt.CaseSensitivity.CaseSensitive
    assert dock._signals_proxy.rowCount() == 0

    # Cased pattern matches under case-sensitive mode.
    dock._filter_input.setText("EngineSpeed")
    assert dock._signals_proxy.rowCount() == 1

    # Toggle off restores the original behaviour.
    dock._case_action.setChecked(False)
    dock._filter_input.setText("enginespeed")
    assert dock._signals_proxy.rowCount() == 1


def test_bus_tree_child_row_emits_dbc_payload(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Dragging a DBC child row from the bus tree produces a ``DbcPayload``."""
    project = _make_project(tmp_path, ["Engine"], dbc_path=example_dbc_path)
    ctx = make_app_context(tmp_path, project=project)
    for ref in project.workspace.dbc:
        ctx.database.add_dbc(ref.path, list(ref.buses))

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    model = dock._bus_tree.model()
    engine_idx = model.index(0, 0)
    child_idx = model.index(0, 0, engine_idx)
    mime = model.mimeData([child_idx])
    assert mime.hasFormat(DBC_MIME)
    parsed = parse_from_mime(mime)
    assert parsed == [DbcPayload(path=str(example_dbc_path))]


def test_drop_dbc_on_bus_row_binds_and_persists(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A bind_requested signal from the tree binds + saves to the .itp."""
    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    ctx.database.import_dbc(example_dbc_path)

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    # Drive the bind signal directly — replicates what dropEvent does
    # when a real Qt drop arrives, without faking a full QDropEvent.
    dock._bus_tree.bind_requested.emit("Engine", str(example_dbc_path))

    assert ctx.database.buses_for_path(example_dbc_path) == ["Engine"]
    project2, _ = open_or_migrate(project.path, user_root=tmp_path / "user-root")
    assert any(
        ref.path.resolve() == example_dbc_path.resolve() and "Engine" in ref.buses
        for ref in project2.workspace.dbc
    )


def test_additive_drop_keeps_existing_bindings(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Dropping the same DBC on a second bus adds to its bindings."""
    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    ctx.database.import_dbc(example_dbc_path)

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    dock._bus_tree.bind_requested.emit("Engine", str(example_dbc_path))
    dock._bus_tree.bind_requested.emit("Body", str(example_dbc_path))

    assert ctx.database.buses_for_path(example_dbc_path) == ["Body", "Engine"]


def test_unbind_via_dock_helper_persists(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """`_on_unbind` mirrors the right-click 'Unbind from <bus>' action."""
    project = _make_project(tmp_path, ["Engine", "Body"], dbc_path=example_dbc_path)
    ctx = make_app_context(tmp_path, project=project)
    for ref in project.workspace.dbc:
        ctx.database.add_dbc(ref.path, list(ref.buses))

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    assert sorted(ctx.database.buses_for_path(example_dbc_path)) == ["Body", "Engine"]
    dock._on_unbind(example_dbc_path, "Engine")
    assert ctx.database.buses_for_path(example_dbc_path) == ["Body"]

    project2, _ = open_or_migrate(project.path, user_root=tmp_path / "user-root")
    refs = [r for r in project2.workspace.dbc if r.path.resolve() == example_dbc_path.resolve()]
    assert refs and refs[0].buses == ["Body"]


def test_buses_section_hint_when_unsaved_project_has_no_buses(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Fresh unsaved project: empty bus tree, hint nudges towards the + button."""
    ctx = make_app_context(tmp_path)  # default fresh, unsaved project
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    assert dock._bus_tree.bus_names() == []
    assert dock._buses_hint is not None
    assert "+" in dock._buses_hint.text()


def test_buses_section_lists_project_buses(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = _make_project(tmp_path, ["Engine", "Body", "Chassis"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    assert dock._bus_tree.bus_names() == ["Engine", "Body", "Chassis"]
    for name in dock._bus_tree.bus_names():
        bus_row = dock._bus_tree.bus_row(name)
        assert bus_row is not None
        assert bus_row.interface == "virtual"
        assert bus_row.channel  # non-empty


def test_bus_row_live_indicator_when_bus_reader_open(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """When BusReader is open on a declared bus, that row goes live."""
    from ithildin.ui.adapter_prober import AdapterChoice

    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    # Inject a detected adapter that matches Engine's (interface, channel).
    # The fixture project uses interface="virtual" and channel=name.lower().
    dock._adapter_rows = [
        AdapterChoice(label="virtual · engine", interface="virtual", channel="engine"),
    ]
    # Fake a live BusReader on Engine via the underlying _controllers
    # dict — bus_names() reads from that map.
    from types import SimpleNamespace

    ctx.bus_reader._controllers = {"Engine": SimpleNamespace(name="Engine")}  # type: ignore[attr-defined]
    ctx.bus_reader._health = {"Engine": (BusHealth.OK, "")}
    dock._refresh_buses()

    row_engine = dock._bus_tree.bus_row("Engine")
    row_body = dock._bus_tree.bus_row("Body")
    assert row_engine is not None and row_engine.state == "live"
    assert row_body is not None and row_body.state == "unavailable"
    ctx.bus_reader._controllers = {}


def test_bus_row_unavailable_when_no_matching_adapter(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Declared bus with no detected adapter → ⊘ unavailable."""
    project = _make_project(tmp_path, ["Engine"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    bus_row = dock._bus_tree.bus_row("Engine")
    assert bus_row is not None and bus_row.state == "unavailable"


def test_detected_adapters_are_not_auto_listed(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Detected adapters never appear as bus rows on their own — buses are user-curated."""
    from ithildin.ui.adapter_prober import VIRTUAL_LOOPBACK, AdapterChoice

    ctx = make_app_context(tmp_path)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    dock._adapter_rows = [
        AdapterChoice(label="kvaser · 0", interface="kvaser", channel=0),
        VIRTUAL_LOOPBACK,
    ]
    dock._refresh_buses()

    assert dock._bus_tree.bus_names() == []
    assert ctx.project.workspace.bus == []


def test_on_bus_declared_appends_to_workspace(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The + handler appends the new BusConfig to project.workspace.bus and refreshes."""
    from ithildin.workspace.schema import BusConfig

    ctx = make_app_context(tmp_path)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    cfg = BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000)
    dock._on_bus_declared(cfg)

    assert [b.name for b in ctx.project.workspace.bus] == ["CAN_1"]
    assert dock._bus_tree.bus_names() == ["CAN_1"]


def test_on_bus_declared_rejects_duplicate_name(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Adding a BusConfig with an already-used name surfaces an error and doesn't dup."""
    from ithildin.workspace.schema import BusConfig

    ctx = make_app_context(tmp_path)
    cfg = BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000)
    ctx.project.workspace.bus = [cfg]

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    errors: list[str] = []
    ctx.database.error.connect(errors.append)
    dock._on_bus_declared(
        BusConfig(name="CAN_1", interface="virtual", channel="other", bitrate=250000)
    )

    assert len(ctx.project.workspace.bus) == 1
    assert errors and "already declared" in errors[0]


def test_on_bus_declared_binds_and_persists_picked_dbcs(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Databases picked in the create dialog are attached, bound to the new
    bus, and mirrored into workspace.dbc."""
    from ithildin.core.decoder import Decoder
    from ithildin.workspace.schema import BusConfig

    ctx = make_app_context(tmp_path)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    decoder = Decoder.from_files(example_dbc_path)
    cfg = BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000)
    dock._on_bus_declared(cfg, [(example_dbc_path, decoder)])

    assert ctx.database.decoder_for_bus("CAN_1") is decoder
    assert "CAN_1" in ctx.database.buses_for_path(example_dbc_path)
    refs = ctx.project.workspace.dbc
    assert [r.path for r in refs] == [example_dbc_path]
    assert refs[0].buses == ["CAN_1"]


def test_on_bus_declared_rejected_name_does_not_bind_dbcs(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """A duplicate-name rejection must not bind picked DBCs to the existing bus."""
    from ithildin.core.decoder import Decoder
    from ithildin.workspace.schema import BusConfig

    ctx = make_app_context(tmp_path)
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000)
    ]
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    decoder = Decoder.from_files(example_dbc_path)
    dup = BusConfig(name="CAN_1", interface="virtual", channel="x", bitrate=250000)
    assert dock._on_bus_declared(dup, [(example_dbc_path, decoder)]) is False
    assert ctx.database.buses_for_path(example_dbc_path) == []


def test_on_remove_bus_drops_bindings_and_row(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Removing a bus also unbinds DBCs that referenced it."""
    project = _make_project(tmp_path, ["Engine"], dbc_path=example_dbc_path)
    ctx = make_app_context(tmp_path, project=project)
    for ref in project.workspace.dbc:
        ctx.database.add_dbc(ref.path, list(ref.buses))

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    assert ctx.database.buses_for_path(example_dbc_path) == ["Engine"]

    dock._on_remove_bus("Engine")

    assert dock._bus_tree.bus_names() == []
    assert ctx.database.buses_for_path(example_dbc_path) == []
    assert [b.name for b in ctx.project.workspace.bus] == []


def test_bus_row_lists_bound_dbcs_as_children(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Each bound DBC appears as a child row under its bus in the tree."""
    project = _make_project(tmp_path, ["Engine", "Body"], dbc_path=example_dbc_path)
    ctx = make_app_context(tmp_path, project=project)
    for ref in project.workspace.dbc:
        ctx.database.add_dbc(ref.path, list(ref.buses))

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    assert dock._bus_tree.dbcs_for_bus("Engine") == [example_dbc_path]
    assert dock._bus_tree.dbcs_for_bus("Body") == [example_dbc_path]


def test_child_row_unbind_last_binding_auto_removes_dbc(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Unbinding the last bus for a DBC removes it from the project entirely.

    Every loaded DBC must live under at least one bus in the new model;
    an unbound DBC would be invisible (no Databases section anymore).
    """
    project = _make_project(tmp_path, ["Engine"], dbc_path=example_dbc_path)
    ctx = make_app_context(tmp_path, project=project)
    for ref in project.workspace.dbc:
        ctx.database.add_dbc(ref.path, list(ref.buses))

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    dock._bus_tree.unbind_requested.emit(example_dbc_path, "Engine")
    assert example_dbc_path not in ctx.database.loaded_paths()

    project2, _ = open_or_migrate(project.path, user_root=tmp_path / "user-root")
    refs = [r for r in project2.workspace.dbc if r.path.resolve() == example_dbc_path.resolve()]
    assert refs == []


def test_bus_tree_collapse_state_survives_database_refresh(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Collapsing 'Engine' must not snap back open when bindings change."""
    project = _make_project(tmp_path, ["Engine", "Body"], dbc_path=example_dbc_path)
    ctx = make_app_context(tmp_path, project=project)
    for ref in project.workspace.dbc:
        ctx.database.add_dbc(ref.path, list(ref.buses))

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    dock._bus_tree.set_expanded_bus("Engine", False)
    assert dock._bus_tree.is_expanded_bus("Engine") is False

    # Trigger a refresh by binding a new DBC. The whole model is rebuilt.
    second_dbc = tmp_path / "second.dbc"
    second_dbc.write_text(example_dbc_path.read_text(encoding="utf-8"), encoding="utf-8")
    ctx.database.add_dbc(second_dbc, ["Engine"])

    # Engine collapsed state survived the rebuild.
    assert dock._bus_tree.is_expanded_bus("Engine") is False
    # Body wasn't touched, so it stays expanded.
    assert dock._bus_tree.is_expanded_bus("Body") is True


def test_unbound_dbc_produces_no_child_rows(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Imported but unbound DBC: no child rows under any bus."""
    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    ctx.database.import_dbc(example_dbc_path)

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    assert dock._bus_tree.dbcs_for_bus("Engine") == []
    assert dock._bus_tree.dbcs_for_bus("Body") == []


def test_unsaved_project_drop_binds_in_memory_without_save(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Drop on a declared bus with an unsaved project: binds, doesn't crash, doesn't try to save."""
    from ithildin.workspace.schema import BusConfig

    ctx = make_app_context(tmp_path)  # default fresh, unsaved project
    ctx.project.workspace.bus = [
        BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000)
    ]
    ctx.database.import_dbc(example_dbc_path)

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    assert dock._bus_tree.bus_names() == ["CAN_1"]
    dock._bus_tree.bind_requested.emit("CAN_1", str(example_dbc_path))
    assert ctx.database.buses_for_path(example_dbc_path) == ["CAN_1"]
    # Unsaved project: nothing was written to disk.
    assert not ctx.project.is_saved


def test_failed_backend_warning_rendered(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Probe failures show as a footer warning under the row list."""
    project = _make_project(tmp_path, ["Engine"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    dock._failed_backends = [("pcan", "timeout")]
    dock._refresh_buses()
    # Footer becomes visible and carries both the backend name and reason.
    label = dock._failed_backends_label
    assert label.isHidden() is False
    text = label.text()
    assert "pcan" in text and "timeout" in text


def test_no_separate_interfaces_section(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The standalone 'Interfaces' section is gone — merged into Buses."""
    ctx = make_app_context(tmp_path)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    # The previous design exposed `_interfaces_label`; make sure no
    # regression brings it back as a separate widget.
    assert not hasattr(dock, "_interfaces_label")


def test_bus_tree_rejects_signal_payload_mime(qapp) -> None:  # type: ignore[no-untyped-def]
    """The Buses tree only accepts the DBC MIME; signal/message MIME goes elsewhere."""
    BusTreeView()  # importability check
    sig_mime = pack_into_mime([SignalPayload(message="M", message_id=1, signal="S")])
    assert sig_mime.hasFormat(SIGNAL_MIME)
    assert not sig_mime.hasFormat(DBC_MIME)


def test_no_separate_databases_section(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The standalone 'Databases' section is gone — DBCs live under buses."""
    ctx = make_app_context(tmp_path)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    assert not hasattr(dock, "_dbc_list_view")
    assert not hasattr(dock, "_dbc_list_model")
    assert not hasattr(dock, "_dbc_empty_hint")


def test_add_dbc_button_disabled_without_bus_selection(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The Add-database button starts disabled and stays so without a selection."""
    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    assert dock._dbc_load_btn.isEnabled() is False
    assert dock._selected_bus_name is None


def test_add_dbc_button_enables_after_selecting_bus_row(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Selecting a bus row in the tree enables the Add-database button."""
    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    engine_idx = dock._bus_tree.model().index(0, 0)
    dock._bus_tree.selectionModel().select(
        engine_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect
    )

    assert dock._selected_bus_name == "Engine"
    assert dock._dbc_load_btn.isEnabled() is True


def test_add_dbc_button_enables_when_child_dbc_row_selected(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """Selecting a child DBC row resolves to its parent bus and enables the button."""
    project = _make_project(tmp_path, ["Engine"], dbc_path=example_dbc_path)
    ctx = make_app_context(tmp_path, project=project)
    for ref in project.workspace.dbc:
        ctx.database.add_dbc(ref.path, list(ref.buses))

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    engine_idx = dock._bus_tree.model().index(0, 0)
    child_idx = dock._bus_tree.model().index(0, 0, engine_idx)
    dock._bus_tree.selectionModel().select(
        child_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect
    )

    assert dock._selected_bus_name == "Engine"
    assert dock._dbc_load_btn.isEnabled() is True


def test_add_dbc_button_disables_after_refresh_clears_selection(
    qapp, qtbot, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A tree rebuild wipes the selection and disables the button again."""
    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    engine_idx = dock._bus_tree.model().index(0, 0)
    dock._bus_tree.selectionModel().select(
        engine_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    assert dock._dbc_load_btn.isEnabled() is True

    dock._refresh_buses()

    assert dock._selected_bus_name is None
    assert dock._dbc_load_btn.isEnabled() is False


def test_load_dbc_short_circuits_when_already_loaded(
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """If the DBC is already loaded, the load button just additively binds.

    No worker is spawned; no file dialog is shown (we stub it to assert
    it's never called).
    """
    from PySide6.QtWidgets import QFileDialog

    project = _make_project(tmp_path, ["Engine", "Body"])
    ctx = make_app_context(tmp_path, project=project)
    ctx.database.add_dbc(example_dbc_path, ["Engine"])

    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)

    body_idx = dock._bus_tree.model().index(1, 0)
    dock._bus_tree.selectionModel().select(
        body_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    assert dock._selected_bus_name == "Body"

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **kw: (str(example_dbc_path), "")),
    )

    dock._on_load_dbc()

    assert dock._loader is None  # no worker thread was started
    assert sorted(ctx.database.buses_for_path(example_dbc_path)) == ["Body", "Engine"]


def _clear_button(line_edit) -> object:  # type: ignore[no-untyped-def]
    """The QToolButton QLineEdit builds for the trailing clear QAction.

    Asserting on ``QAction.isVisible()`` is not enough: QLineEdit shows the
    side-widget button when ``addAction`` runs regardless of the action's
    state, so the action flag can read False while the ✕ is on screen.
    """
    from PySide6.QtWidgets import QToolButton

    buttons = [b for b in line_edit.findChildren(QToolButton) if b.toolTip() == "Clear"]
    assert len(buttons) == 1, f"expected one clear button, found {len(buttons)}"
    return buttons[0]


def test_filter_clear_button_hidden_until_text(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(make_app_context(tmp_path))
    dock.show()
    qapp.processEvents()

    button = _clear_button(dock._filter_input)
    assert button.isVisible() is False, "clear button must not show on an empty filter"

    dock._filter_input.setText("Engine")
    qapp.processEvents()
    assert button.isVisible() is True
    assert not dock._clear_action.icon().isNull()

    dock._clear_action.trigger()
    qapp.processEvents()
    assert dock._filter_input.text() == ""
    assert button.isVisible() is False


def test_bus_row_follows_health(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from ithildin.ui.theme import ThemeManager

    project = _make_project(tmp_path, ["Engine"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    reader = ctx.bus_reader
    tokens = ThemeManager.instance().current_tokens()

    def item():  # type: ignore[no-untyped-def]
        return dock._bus_tree._tree_model.item(0)

    reader._controllers = {"Engine": SimpleNamespace(name="Engine")}  # type: ignore[assignment]
    reader.bus_opening.emit("Engine")
    assert item().text().startswith("◔  Engine")

    reader._health = {"Engine": (BusHealth.ERRORS, "no ACK")}
    reader.bus_health_changed.emit("Engine", "errors", "no ACK")
    row = dock._bus_tree.bus_row("Engine")
    assert row is not None and row.state == "errors"
    assert item().text().startswith("▲  Engine")
    assert item().toolTip().startswith("Bus errors. Check the cable")
    assert item().foreground().color().name() == tokens["warn"]

    # A detail-only change keeps the row as it is.
    reader._health = {"Engine": (BusHealth.ERRORS, "error passive")}
    reader.bus_health_changed.emit("Engine", "errors", "error passive")
    assert dock._bus_tree.bus_row("Engine") is row

    reader._health = {"Engine": (BusHealth.BUS_OFF, "")}
    reader.bus_health_changed.emit("Engine", "bus_off", "")
    assert item().text().startswith("■  Engine")
    assert item().foreground().color().name() == tokens["err"]

    # The adapter went away: the bus closed, LOST stays.
    reader._controllers = {}
    reader._health = {"Engine": (BusHealth.LOST, "")}
    reader.bus_stopped.emit("Engine")
    assert item().text().startswith("✕  Engine")
    assert "Adapter lost" in item().toolTip()


def test_remove_live_bus_closes_and_forgets(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = _make_project(tmp_path, ["Engine"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    with qtbot.waitSignal(ctx.bus_reader.bus_started, timeout=2000):
        ctx.bus_reader.open(ctx.project.workspace.bus[0])

    dock._on_remove_bus("Engine")

    assert not ctx.bus_reader.is_open
    assert ctx.bus_reader.last_config_for("Engine") is None


def test_closing_glyph_for_retiring_bus(qapp, qtbot, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import threading

    from tests.fake_bus import FakeBus, use_fake_bus

    fake = FakeBus()
    fake.block = threading.Event()
    use_fake_bus(monkeypatch, fake)
    project = _make_project(tmp_path, ["Engine"])
    ctx = make_app_context(tmp_path, project=project)
    dock = LeftDockWidget()
    qtbot.addWidget(dock)
    dock.bind(ctx)
    reader = ctx.bus_reader
    with qtbot.waitSignal(reader.bus_started, timeout=2000):
        reader.open(ctx.project.workspace.bus[0])

    def item():  # type: ignore[no-untyped-def]
        return dock._bus_tree._tree_model.item(0)

    try:
        reader.close(wait_ms=50)
        assert item().text().startswith("◐  Engine")
        assert item().toolTip().startswith("Closing")
    finally:
        with qtbot.waitSignal(reader.bus_released, timeout=2000):
            fake.block.set()
    assert item().text().startswith("◌  Engine")
