# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Pydantic v2 models for the ``.itp`` workspace TOML file.

A project file declares buses, DBC files (each attached to one or more
named buses), and an MCP server config (enable flag + loopback bind).

The MCP allowlist and audit log are **not** stored here — they live in
``~/.ithildin/<project>/`` so the project file stays shareable in git
without leaking per-user security grants.

Cross-references (``DbcRef.buses``) are validated at the workspace level
so a bad name is caught at load time rather than at first use.

Schema versions:
- v2 (current) — `Workspace`. No inline allowlist.
- v1 (legacy) — `LegacyV1Workspace`. Allowlist was inline. Loader detects
  v1 files; ``app_state.project`` performs the one-time migration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The MCP HTTP transport has no transport-level encryption; a bearer token
# gates it (see mcp/auth.py) but binding beyond loopback would expose the
# port to the network. v0.1 only supports a loopback bind.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

SupportedInterface = Literal[
    "pcan",
    "kvaser",
    "gs_usb",
    "socketcan",
    "virtual",
    "ixxat",
]
"""python-can backends ithildin officially supports."""

LISTEN_ONLY_INTERFACES: frozenset[str] = frozenset({"pcan", "kvaser", "gs_usb", "virtual"})
"""Interfaces for which ``listen_only=True`` is honored.

``pcan`` / ``kvaser`` receive a native silent kwarg (see
``core.bus._apply_listen_only_kwargs``); ``gs_usb`` takes
``GsUsbBus(listen_only=True)``, which refuses to open when the adapter's
firmware has no listen-only mode; ``virtual`` has no wire so the
app-level send block alone is sufficient. ``socketcan`` (silent mode is an
OS-level ``ip link ... listen-only`` netlink property, not a python-can
constructor kwarg) and ``ixxat`` (the VCI wrapper hardcodes its opmode) have
no python-can kwarg ithildin can set, so the combo is rejected rather than
degrading to a bus that still ACKs on the wire."""


class BusConfig(BaseModel):
    """One physical or virtual CAN channel."""

    model_config = ConfigDict(extra="forbid")

    name: str
    interface: SupportedInterface
    channel: str | int
    bitrate: int = Field(gt=0)
    fd: bool = False
    listen_only: bool = False
    """Open the bus in receive-only mode.

    The backend is initialised with its native silent/passive kwarg
    (PCAN ``state=PASSIVE``, Kvaser ``driver_mode=SILENT``, gs_usb
    ``GS_CAN_MODE_LISTEN_ONLY``) so the transceiver does not ACK or transmit; the
    app additionally blocks every send path at ``BusReader.send_message``.

    Not all backends expose a silent kwarg: ``virtual`` has no
    transceiver at all (app-level gate covers it). ``socketcan`` (silent
    mode is OS-level only, set via ``ip link ... listen-only``) and
    ``ixxat`` (the python-can VCI wrapper hardcodes its opmode) have no
    kwarg ithildin can set — so for those two, ``listen_only=True`` is
    rejected at schema validation time rather than degrading silently to
    a bus that still ACKs on the wire. The honored set is
    ``LISTEN_ONLY_INTERFACES``.
    """

    connect_on_startup: bool = False
    """Open this bus automatically at app launch (``app.py`` acts on it).

    Device identity is keyed on ``(interface, channel)``. A replug-stable
    ``device_id`` (from a hardware serial) is deferred to a future
    migration.
    """

    @model_validator(mode="before")
    @classmethod
    def _migrate_cantact(cls, data: Any) -> Any:
        # 0.1.0 and 0.1.1 shipped a ``cantact`` backend, with channels saved
        # as "ch:N"; gs_usb drives the same adapters, addressed by index.
        if isinstance(data, dict) and data.get("interface") == "cantact":
            channel = str(data.get("channel", 0)).removeprefix("ch:")
            data = {**data, "interface": "gs_usb", "channel": int(channel)}
        return data

    @model_validator(mode="after")
    def _reject_unsupported_listen_only(self) -> BusConfig:
        if self.listen_only and self.interface not in LISTEN_ONLY_INTERFACES:
            raise ValueError(
                f"{self.interface!r} cannot enforce listen-only mode "
                "(no silent kwarg ithildin can set); open a non-listen-only "
                "bus or use a pcan/kvaser/gs_usb adapter."
            )
        return self


class DbcRef(BaseModel):
    """A DBC file reference, attached to one or more named buses."""

    model_config = ConfigDict(extra="forbid")

    path: Path
    buses: list[str] = Field(default_factory=list)
    signal_centric: bool = True
    """Hide the parent message name when this DBC's signals are rendered.

    Professional / OEM DBCs treat the CAN message as an arbitrary carrier
    for unrelated signals — the message name is editorial noise. Hobbyist
    DBCs (OpenDBC etc.) group signals by function, where the message name
    is meaningful; flip this to ``False`` for those.
    """


class McpConfig(BaseModel):
    """MCP server configuration (v2).

    Permissions (``AllowRule`` rows) live in
    ``~/.ithildin/<project>/allowlist.toml`` — not here.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=7320, ge=1, le=65535)
    dry_run: bool = False

    @field_validator("host")
    @classmethod
    def _reject_non_loopback(cls, v: str) -> str:
        if v not in LOOPBACK_HOSTS:
            raise ValueError(
                f"MCP host must be one of {sorted(LOOPBACK_HOSTS)}; got {v!r}. "
                "The MCP transport has no transport-level encryption; binding "
                "beyond loopback would expose the CAN send tools to the network."
            )
        return v


class PreferencesConfig(BaseModel):
    """Project-scoped UI preferences. Surfaced via the Settings dialog."""

    model_config = ConfigDict(extra="forbid")

    default_plot_window_seconds: float = Field(default=12.0, gt=0.0, le=3600.0)


class ViewState(BaseModel):
    """One open MDI view's identity + content.

    ``state`` is a free-form dict per view kind (signal lists, filter
    text, replay file path, …). Each view defines its own shape and
    must tolerate unknown keys / wrong types defensively — the schema
    deliberately does not enumerate kinds so adding a view kind doesn't
    require a schema migration.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    instance_id: str
    params: dict[str, object] = Field(default_factory=dict)
    state: dict[str, object] = Field(default_factory=dict)


class Workspace(BaseModel):
    """Root model for a ``.itp`` file (schema v2)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    name: str = "Untitled"
    bus: list[BusConfig] = Field(default_factory=list)
    dbc: list[DbcRef] = Field(default_factory=list)
    mcp: McpConfig = Field(default_factory=McpConfig)
    preferences: PreferencesConfig = Field(default_factory=PreferencesConfig)
    views: list[ViewState] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_cross_references(self) -> Workspace:
        bus_names = [b.name for b in self.bus]
        if len(set(bus_names)) != len(bus_names):
            raise ValueError("bus names must be unique within a workspace")
        channels = [(b.interface, b.channel) for b in self.bus]
        if len(set(channels)) != len(channels):
            raise ValueError("two buses cannot share the same interface and channel")
        known = set(bus_names)
        for dbc in self.dbc:
            for b in dbc.buses:
                if b not in known:
                    raise ValueError(f"DBC {dbc.path!s} references unknown bus {b!r}")
        return self


# --------------------------------------------------------------------------
# Legacy v1 — preserved for one-time migration only. Do not use for new
# projects; the loader detects v1 and ``app_state.project`` rewrites the
# file as v2 with allowlist rows lowered to the external file.
# --------------------------------------------------------------------------


class _LegacyTxBudgetV1(BaseModel):
    bus: str
    max_frames_per_second: int = Field(gt=0)


class _LegacyMcpSendRuleV1(BaseModel):
    bus: str
    allow_messages: list[str] = Field(default_factory=list)
    allow_signals: list[str] = Field(default_factory=list)
    allow_raw_ids: list[int] = Field(default_factory=list)
    enforce_signal_ranges: bool = True
    allow_periodic: bool = False


class _LegacyMcpPermissionsV1(BaseModel):
    allow_replay: bool = False
    allow_capture_control: bool = False
    tx_budget: list[_LegacyTxBudgetV1] = Field(default_factory=list)
    send: list[_LegacyMcpSendRuleV1] = Field(default_factory=list)


class _LegacyMcpConfigV1(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=7117, ge=1, le=65535)
    auth_token: str = ""
    dry_run: bool = False
    permissions: _LegacyMcpPermissionsV1 = Field(default_factory=_LegacyMcpPermissionsV1)


class LegacyV1Workspace(BaseModel):
    """Schema v1 — read-only, used during migration to v2."""

    schema_version: Literal[1]
    name: str = "Untitled"
    bus: list[BusConfig] = Field(default_factory=list)
    dbc: list[DbcRef] = Field(default_factory=list)
    mcp: _LegacyMcpConfigV1 = Field(default_factory=_LegacyMcpConfigV1)
