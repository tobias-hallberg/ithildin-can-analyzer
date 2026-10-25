# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``workspace.schema`` and ``workspace.loader``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ithildin.workspace.loader import (
    detect_schema_version,
    load_workspace,
    load_workspace_v1,
    save_workspace,
)
from ithildin.workspace.schema import (
    BusConfig,
    DbcRef,
    LegacyV1Workspace,
    ViewState,
    Workspace,
)

_DEMO_V2_TOML = """\
schema_version = 2
name = "Engine workspace"

[[bus]]
name = "CAN_1"
interface = "virtual"
channel = "engine"
bitrate = 500000
fd = false

[[dbc]]
path = "engine.dbc"
buses = ["CAN_1"]

[mcp]
enabled = false
host = "127.0.0.1"
port = 7320
"""

_DEMO_V1_TOML = """\
schema_version = 1
name = "Old workspace"

[[bus]]
name = "Engine"
interface = "virtual"
channel = "engine"
bitrate = 500000

[[mcp.permissions.tx_budget]]
bus = "Engine"
max_frames_per_second = 100

[[mcp.permissions.send]]
bus = "Engine"
allow_messages = ["EngineCommand"]
allow_signals = ["EngineCommand.IdleSetpoint"]
"""


@pytest.fixture
def demo_v2_path(tmp_path: Path) -> Path:
    p = tmp_path / "demo.itp"
    p.write_text(_DEMO_V2_TOML, encoding="utf-8")
    (tmp_path / "engine.dbc").write_text("", encoding="utf-8")
    return p


@pytest.fixture
def demo_v1_path(tmp_path: Path) -> Path:
    p = tmp_path / "old.ithildin"
    p.write_text(_DEMO_V1_TOML, encoding="utf-8")
    return p


def test_load_v2_workspace(demo_v2_path: Path) -> None:
    ws = load_workspace(demo_v2_path)
    assert ws.schema_version == 2
    assert ws.name == "Engine workspace"
    assert [b.name for b in ws.bus] == ["CAN_1"]
    assert ws.bus[0].bitrate == 500000
    assert ws.dbc[0].path.is_absolute()
    assert ws.dbc[0].path.name == "engine.dbc"
    assert ws.mcp.port == 7320


def test_mcp_host_rejects_non_loopback() -> None:
    import pydantic

    from ithildin.workspace.schema import McpConfig

    with pytest.raises(pydantic.ValidationError):
        McpConfig(host="0.0.0.0")
    # Loopback forms are accepted.
    assert McpConfig(host="127.0.0.1").host == "127.0.0.1"
    assert McpConfig(host="localhost").host == "localhost"


def test_v2_without_preferences_loads_with_defaults(demo_v2_path: Path) -> None:
    """Existing v2 .itp files (no preferences block) get default preferences."""
    ws = load_workspace(demo_v2_path)
    assert ws.preferences.default_plot_window_seconds == 12.0


def test_v2_without_listen_only_defaults_to_false(demo_v2_path: Path) -> None:
    """Pre-feature .itp files load with ``listen_only=False`` for every bus."""
    ws = load_workspace(demo_v2_path)
    assert all(b.listen_only is False for b in ws.bus)


def test_listen_only_round_trip(tmp_path: Path) -> None:
    """``listen_only=True`` survives save → reload."""
    ws = Workspace(
        name="ro-test",
        bus=[
            BusConfig(
                name="CAN_1",
                interface="virtual",
                channel="x",
                bitrate=500000,
                listen_only=True,
            ),
        ],
    )
    out = tmp_path / "ro.itp"
    save_workspace(ws, out)
    reloaded = load_workspace(out)
    assert reloaded.bus[0].listen_only is True


@pytest.mark.parametrize("interface", ["pcan", "kvaser", "gs_usb", "virtual"])
def test_listen_only_accepted_for_capable_interfaces(interface: str) -> None:
    cfg = BusConfig(name="ro", interface=interface, channel="0", bitrate=500000, listen_only=True)
    assert cfg.listen_only is True


@pytest.mark.parametrize(("channel", "index"), [("ch:0", 0), (0, 0), ("1", 1)])
def test_cantact_bus_migrates_to_gs_usb(channel: str | int, index: int) -> None:
    """0.1.0 / 0.1.1 saved CANtact buses with ``ch:N`` channels; gs_usb
    addresses the same adapter by index."""
    cfg = BusConfig.model_validate(
        {"name": "CAN_1", "interface": "cantact", "channel": channel, "bitrate": 500000}
    )
    assert (cfg.interface, cfg.channel) == ("gs_usb", index)


def test_cantact_listen_only_survives_migration() -> None:
    cfg = BusConfig.model_validate(
        {
            "name": "CAN_1",
            "interface": "cantact",
            "channel": "ch:0",
            "bitrate": 500000,
            "listen_only": True,
        }
    )
    assert cfg.interface == "gs_usb"
    assert cfg.listen_only is True


_CANTACT_BUS_TOML = """\
[[bus]]
name = "CAN_1"
interface = "cantact"
channel = "ch:0"
bitrate = 500000
"""


def test_project_with_cantact_bus_loads_and_saves_as_gs_usb(tmp_path: Path) -> None:
    path = tmp_path / "old.itp"
    path.write_text('schema_version = 2\nname = "Old"\n\n' + _CANTACT_BUS_TOML, encoding="utf-8")

    save_workspace(load_workspace(path), path)
    text = path.read_text(encoding="utf-8")
    assert 'interface = "gs_usb"' in text
    assert "channel = 0" in text


def test_v1_project_with_cantact_bus_migrates(tmp_path: Path) -> None:
    path = tmp_path / "old_v1.itp"
    path.write_text('schema_version = 1\nname = "Old"\n\n' + _CANTACT_BUS_TOML, encoding="utf-8")

    legacy = load_workspace_v1(path)
    assert (legacy.bus[0].interface, legacy.bus[0].channel) == ("gs_usb", 0)


@pytest.mark.parametrize("interface", ["socketcan", "ixxat"])
def test_listen_only_rejected_for_kwargless_interfaces(interface: str) -> None:
    """socketcan/ixxat have no python-can silent kwarg — the combo must be
    rejected rather than opening a bus that still ACKs on the wire."""
    with pytest.raises(ValidationError):
        BusConfig(
            name="ro",
            interface=interface,
            channel="0",
            bitrate=500000,
            listen_only=True,
        )


def test_preferences_round_trip(tmp_path: Path) -> None:
    """A non-default preference survives save → reload."""
    from ithildin.workspace.schema import PreferencesConfig

    ws = Workspace(
        name="prefs-test",
        preferences=PreferencesConfig(default_plot_window_seconds=30.0),
    )
    out = tmp_path / "prefs.itp"
    save_workspace(ws, out)
    reloaded = load_workspace(out)
    assert reloaded.preferences.default_plot_window_seconds == 30.0


def test_preferences_rejects_non_positive() -> None:
    from pydantic import ValidationError as _VE

    from ithildin.workspace.schema import PreferencesConfig

    with pytest.raises(_VE):
        PreferencesConfig(default_plot_window_seconds=0)
    with pytest.raises(_VE):
        PreferencesConfig(default_plot_window_seconds=-5)


def test_duplicate_bus_names_rejected() -> None:
    with pytest.raises(ValidationError):
        Workspace(
            bus=[
                BusConfig(name="A", interface="virtual", channel="a", bitrate=500000),
                BusConfig(name="A", interface="virtual", channel="b", bitrate=500000),
            ]
        )


def test_duplicate_bus_channels_rejected() -> None:
    with pytest.raises(ValidationError):
        Workspace(
            bus=[
                BusConfig(name="A", interface="virtual", channel="a", bitrate=500000),
                BusConfig(name="B", interface="virtual", channel="a", bitrate=500000),
            ]
        )


def test_dbc_referencing_unknown_bus_rejected() -> None:
    with pytest.raises(ValidationError):
        Workspace(
            bus=[
                BusConfig(name="A", interface="virtual", channel="a", bitrate=500000),
            ],
            dbc=[DbcRef(path=Path("x.dbc"), buses=["B"])],
        )


def test_unknown_top_level_key_rejected() -> None:
    with pytest.raises(ValidationError):
        Workspace.model_validate({"name": "x", "bogus": True})


def test_inline_allowlist_rejected_in_v2() -> None:
    """v2 must NOT accept the v1 inline allowlist (`mcp.permissions.send`)."""
    with pytest.raises(ValidationError):
        Workspace.model_validate(
            {
                "schema_version": 2,
                "bus": [{"name": "A", "interface": "virtual", "channel": "a", "bitrate": 500000}],
                "mcp": {
                    "permissions": {
                        "send": [{"bus": "A"}],
                    }
                },
            }
        )


def test_save_and_reload_roundtrip(tmp_path: Path, demo_v2_path: Path) -> None:
    ws = load_workspace(demo_v2_path)
    out = tmp_path / "out" / "saved.itp"
    save_workspace(ws, out)
    reloaded = load_workspace(out)
    assert reloaded.name == ws.name
    assert [b.name for b in reloaded.bus] == [b.name for b in ws.bus]
    assert reloaded.mcp.port == ws.mcp.port


def test_detect_schema_version(demo_v1_path: Path, demo_v2_path: Path) -> None:
    assert detect_schema_version(demo_v1_path) == 1
    assert detect_schema_version(demo_v2_path) == 2


def test_load_v1_legacy(demo_v1_path: Path) -> None:
    ws = load_workspace_v1(demo_v1_path)
    assert isinstance(ws, LegacyV1Workspace)
    assert ws.schema_version == 1
    assert ws.mcp.permissions.send[0].allow_signals == [
        "EngineCommand.IdleSetpoint",
    ]
    assert ws.mcp.permissions.tx_budget[0].max_frames_per_second == 100


def test_load_v1_via_v2_loader_fails(demo_v1_path: Path) -> None:
    """Old v1 files cannot be loaded via the v2 path; caller must migrate."""
    with pytest.raises(ValidationError):
        load_workspace(demo_v1_path)


def test_empty_itp_raises_clear_error(tmp_path: Path) -> None:
    """A 0-byte .itp gives a friendly message, not a pydantic v1 error."""
    from ithildin.app_state.project import open_or_migrate

    empty = tmp_path / "bad.itp"
    empty.write_bytes(b"")

    with pytest.raises(ValueError, match="empty"):
        detect_schema_version(empty)

    with pytest.raises(ValueError) as excinfo:
        open_or_migrate(empty)
    assert "LegacyV1Workspace" not in str(excinfo.value)


def test_save_workspace_is_atomic_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed dump leaves the existing file intact and no .tmp behind."""
    from ithildin.workspace import loader as _loader

    out = tmp_path / "ws.itp"
    save_workspace(Workspace(name="original"), out)
    before = out.read_bytes()

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("dump failed")

    monkeypatch.setattr(_loader.tomli_w, "dump", _boom)
    with pytest.raises(RuntimeError):
        save_workspace(Workspace(name="changed"), out)

    assert out.read_bytes() == before
    assert load_workspace(out).name == "original"


def test_views_round_trip(tmp_path: Path) -> None:
    """Workspace.views (and bus list) survive save → reload."""
    ws = Workspace(
        name="with-views",
        bus=[
            BusConfig(name="CAN_1", interface="virtual", channel="vbus", bitrate=500000),
        ],
        views=[
            ViewState(
                kind="signal_plot",
                instance_id="abc123",
                params={"name": "Plot 1"},
                state={"signals": ["EngineCommand.EngineSpeed"]},
            ),
            ViewState(
                kind="message_trace",
                instance_id="def456",
                params={"channel": "CAN_1"},
                state={"filter": "0x1", "paused": True},
            ),
        ],
    )
    out = tmp_path / "views.itp"
    save_workspace(ws, out)
    reloaded = load_workspace(out)
    assert [b.name for b in reloaded.bus] == ["CAN_1"]
    assert [v.kind for v in reloaded.views] == ["signal_plot", "message_trace"]
    assert reloaded.views[0].state == {"signals": ["EngineCommand.EngineSpeed"]}
    assert reloaded.views[1].state == {"filter": "0x1", "paused": True}


def test_bus_profile_connect_on_startup_defaults_off() -> None:
    cfg = BusConfig(name="CAN_1", interface="virtual", channel="v", bitrate=500000)
    assert cfg.connect_on_startup is False


def test_bus_profile_connect_on_startup_round_trip(tmp_path: Path) -> None:
    ws = Workspace(
        name="profile",
        bus=[
            BusConfig(
                name="Powertrain",
                interface="virtual",
                channel="v",
                bitrate=500000,
                connect_on_startup=True,
            )
        ],
    )
    out = tmp_path / "profile.itp"
    save_workspace(ws, out)
    cfg = load_workspace(out).bus[0]
    assert cfg.connect_on_startup is True


def test_legacy_v2_file_without_connect_on_startup_still_loads(tmp_path: Path) -> None:
    """A v2 file written before ``connect_on_startup`` existed must parse,
    with the field falling back to its default (extra='forbid' rejects
    only unknown keys, not missing ones)."""
    out = tmp_path / "old.itp"
    out.write_text(_DEMO_V2_TOML, encoding="utf-8")
    cfg = load_workspace(out).bus[0]
    assert cfg.connect_on_startup is False
