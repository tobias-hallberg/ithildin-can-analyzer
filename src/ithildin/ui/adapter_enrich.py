# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Turn raw ``detect_available_configs`` dicts into rich device metadata.

``can.detect_available_configs(interfaces=[backend])`` returns one dict
per *channel*. Several backends already fold useful fields into those
dicts (kvaser ships ``device_name`` / ``serial`` / ``dongle_channel``;
pcan ships ``device_name`` / ``device_id`` / ``supports_fd`` /
``channel_condition``). The enrichers here parse those into a two-level
model — a :class:`DeviceInfo` owning one or more :class:`ChannelInfo` —
that the Create CAN bus dialog renders as a device dropdown with channel
pills underneath.

Everything degrades honestly: a backend with no specific enricher (or a
config dict missing a field) falls back to ``_enrich_default``, which
exposes only the interface + channel and leaves the rest ``None`` /
unknown. ``status`` and ``capabilities`` stay ``None`` unless a backend
actually probes them (only pcan does today), so the dialog never shows a
default dressed up as a fact. No enricher here makes blocking calls: the
kvaser firmware string comes from a ``"firmware"`` key the prober folds
into each config via an extra CANlib query (see
``adapter_prober._add_kvaser_firmware``); enrichers just read it.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ithildin.workspace.schema import SupportedInterface

ChannelStatus = Literal["free", "in-use", "offline"]
DeviceStatus = Literal["available", "in-use", "offline"]
Transport = Literal["USB", "Kernel", "Software", "Unknown"]

# PCAN channel_condition bit flags (mirrored from PCANBasic to avoid
# importing the backend just to read three constants).
_PCAN_AVAILABLE = 0x01
_PCAN_OCCUPIED = 0x02


@dataclass(frozen=True)
class ChannelInfo:
    """One openable channel on a device."""

    channel: str | int  # the value passed to python-can as ``channel=``
    label: str
    status: ChannelStatus = "free"


@dataclass(frozen=True)
class DeviceInfo:
    """One physical or virtual adapter, with its channels."""

    interface: SupportedInterface
    vendor: str
    model: str
    serial: str | None = None
    firmware: str | None = None
    transport: Transport = "Unknown"
    capabilities: tuple[str, ...] | None = None
    channels: tuple[ChannelInfo, ...] = field(default_factory=tuple)
    status: DeviceStatus | None = None

    @property
    def key(self) -> str:
        """Stable-ish identity for combo de-dup within one probe."""
        tail = (
            self.serial or self.model or (str(self.channels[0].channel) if self.channels else "?")
        )
        return f"{self.interface}:{tail}"

    @property
    def display_label(self) -> str:
        base = f"{self.vendor} · {self.model}"
        if self.serial:
            return f"{base}  (sn {self.serial})"
        return base


Enricher = Callable[[str, list[dict[str, object]]], list[DeviceInfo]]

_TRANSPORTS: dict[str, Transport] = {
    "kvaser": "USB",
    "pcan": "USB",
    "cantact": "USB",
    "ixxat": "USB",
    "socketcan": "Kernel",
    "virtual": "Software",
}

_VENDORS: dict[str, str] = {
    "kvaser": "Kvaser",
    "pcan": "PEAK-System",
    "cantact": "CANtact",
    "ixxat": "IXXAT",
    "socketcan": "Linux SocketCAN",
    "virtual": "Ithildin",
}


def enrich(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """Map raw configs for one backend into devices."""
    return _ENRICHERS.get(backend, _enrich_default)(backend, configs)


def _enrich_default(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """One device per config; only interface + channel are trusted."""
    out: list[DeviceInfo] = []
    for cfg in configs:
        channel = cfg.get("channel")
        if not isinstance(channel, (str, int)):
            continue
        out.append(
            DeviceInfo(
                interface=backend,  # type: ignore[arg-type]
                vendor=_VENDORS.get(backend, backend.title()),
                model=str(channel),
                transport=_TRANSPORTS.get(backend, "Unknown"),
                channels=(ChannelInfo(channel=channel, label=str(channel)),),
            )
        )
    return out


def _enrich_kvaser(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """Group kvaser channels by serial — channels of one dongle share it."""
    by_serial: OrderedDict[str, list[dict[str, object]]] = OrderedDict()
    for cfg in configs:
        serial_raw = cfg.get("serial")
        # CANlib reports 0 for the virtual driver and unkeyed channels.
        serial = str(serial_raw) if serial_raw not in (None, 0) else ""
        by_serial.setdefault(serial, []).append(cfg)

    out: list[DeviceInfo] = []
    for serial, group in by_serial.items():
        first = group[0]
        name = first.get("device_name")
        model = str(name) if isinstance(name, str) and name else "Kvaser device"
        fw = first.get("firmware")
        firmware = str(fw) if fw else None
        channels: list[ChannelInfo] = []
        for cfg in group:
            channel = cfg.get("channel")
            if not isinstance(channel, (str, int)):
                continue
            dongle = cfg.get("dongle_channel")
            label = f"ch{int(dongle) - 1}" if isinstance(dongle, int) else str(channel)
            channels.append(ChannelInfo(channel=channel, label=label))
        if not channels:
            continue
        out.append(
            DeviceInfo(
                interface="kvaser",
                vendor="Kvaser",
                model=model,
                serial=serial or None,
                firmware=firmware,
                transport="USB",
                channels=tuple(channels),
            )
        )
    return out


def _enrich_pcan(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """Group pcan channels by device_id; read FD + per-channel status."""
    by_dev: OrderedDict[object, list[dict[str, object]]] = OrderedDict()
    for cfg in configs:
        by_dev.setdefault(cfg.get("device_id"), []).append(cfg)

    out: list[DeviceInfo] = []
    for dev_id, group in by_dev.items():
        first = group[0]
        name = first.get("device_name")
        model = str(name) if isinstance(name, str) and name else "PCAN device"
        caps: tuple[str, ...] = ("CAN 2.0",)
        if any(bool(cfg.get("supports_fd")) for cfg in group):
            caps = ("CAN 2.0", "CAN FD")
        channels: list[ChannelInfo] = []
        for cfg in group:
            channel = cfg.get("channel")
            if not isinstance(channel, (str, int)):
                continue
            channels.append(
                ChannelInfo(
                    channel=channel,
                    label=str(channel),
                    status=_pcan_status(cfg.get("channel_condition")),
                )
            )
        if not channels:
            continue
        serial = f"{dev_id:08x}" if isinstance(dev_id, int) else None
        device_status: DeviceStatus = (
            "available" if any(c.status == "free" for c in channels) else "in-use"
        )
        out.append(
            DeviceInfo(
                interface="pcan",
                vendor="PEAK-System",
                model=model,
                serial=serial,
                transport="USB",
                capabilities=caps,
                channels=tuple(channels),
                status=device_status,
            )
        )
    return out


def _pcan_status(condition: object) -> ChannelStatus:
    if not isinstance(condition, int):
        return "free"
    if condition & _PCAN_OCCUPIED:
        return "in-use"
    if condition & _PCAN_AVAILABLE:
        return "free"
    return "offline"


_ENRICHERS: dict[str, Enricher] = {
    "kvaser": _enrich_kvaser,
    "pcan": _enrich_pcan,
}
