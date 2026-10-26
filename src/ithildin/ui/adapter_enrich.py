# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Turn raw ``detect_available_configs`` dicts into rich device metadata.

``core.bus.detect_configs(backend)`` returns one dict per *channel*.
Several backends already fold useful fields into those dicts (kvaser
ships ``device_name`` / ``serial`` / ``dongle_channel``;
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
default dressed up as a fact. Each channel carries the replug-stable
``hw_id`` its backend reports (``schema.hw_id_of``), cleared when two
channels of one probe report the same. No enricher here makes blocking
calls: the kvaser firmware string and card type come from ``"firmware"``
and ``"card_type"`` keys the prober folds into each config via extra
CANlib queries (see ``adapter_prober._add_kvaser_channel_data``);
enrichers just read them.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from ithildin.workspace.schema import (
    PCAN_DEFAULT_DEVICE_IDS,
    SupportedInterface,
    channel_key,
    hw_id_of,
)

ChannelStatus = Literal["free", "in-use", "offline"]
DeviceStatus = Literal["available", "in-use", "offline"]
Transport = Literal["USB", "Kernel", "Software", "Unknown"]

# PCAN channel_condition bit flags (mirrored from PCANBasic to avoid
# importing the backend just to read three constants).
_PCAN_AVAILABLE = 0x01
_PCAN_OCCUPIED = 0x02
# CANlib canHWTYPE_VIRTUAL, the card type of Kvaser Virtual channels.
_KVASER_HWTYPE_VIRTUAL = 0x01


@dataclass(frozen=True)
class ChannelInfo:
    """One openable channel on a device."""

    channel: str | int  # the value passed to python-can as ``channel=``
    label: str
    status: ChannelStatus = "free"
    hw_id: str | None = None  # ``schema.hw_id_of``; None when it identifies nothing


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
        if not self.serial:
            return base
        if self.interface == "pcan":
            # User-assigned in PCAN-View, not a factory serial.
            return f"{base}  (device ID 0x{int(self.serial, 16):X})"
        return f"{base}  (sn {self.serial})"


Enricher = Callable[[str, list[dict[str, object]]], list[DeviceInfo]]

_TRANSPORTS: dict[str, Transport] = {
    "kvaser": "USB",
    "pcan": "USB",
    "gs_usb": "USB",
    "ixxat": "USB",
    "socketcan": "Kernel",
    "virtual": "Software",
}

_VENDORS: dict[str, str] = {
    "kvaser": "Kvaser",
    "pcan": "PEAK-System",
    "gs_usb": "candleLight",
    "ixxat": "IXXAT",
    "socketcan": "Linux SocketCAN",
    "virtual": "Ithildin",
}


def enrich(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """Map raw configs for one backend into devices."""
    return _drop_shared_ids(_ENRICHERS.get(backend, _enrich_default)(backend, configs))


def _drop_shared_ids(devices: list[DeviceInfo]) -> list[DeviceInfo]:
    """Clear an identity two channels of one probe report: it names neither.

    Happens with PCAN adapters given the same device ID, or clone gs_usb
    adapters sharing a serial.
    """
    counts = Counter(
        channel_key(d.interface, ch.channel, ch.hw_id)
        for d in devices
        for ch in d.channels
        if ch.hw_id is not None
    )
    return [
        replace(
            d,
            channels=tuple(
                replace(ch, hw_id=None)
                if counts[channel_key(d.interface, ch.channel, ch.hw_id)] > 1
                else ch
                for ch in d.channels
            ),
        )
        for d in devices
    ]


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
                channels=(
                    ChannelInfo(channel=channel, label=str(channel), hw_id=hw_id_of(backend, cfg)),
                ),
            )
        )
    return out


def _enrich_ixxat(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """Group IXXAT channels by unique hardware id: every device numbers its
    channels from 0, so the channel alone can't tell two devices apart."""
    by_device: OrderedDict[object, list[dict[str, object]]] = OrderedDict()
    for cfg in configs:
        by_device.setdefault(cfg.get("unique_hardware_id"), []).append(cfg)

    out: list[DeviceInfo] = []
    for uhid, group in by_device.items():
        channels: list[ChannelInfo] = []
        for cfg in group:
            channel = cfg.get("channel")
            if isinstance(channel, int):
                channels.append(
                    ChannelInfo(channel=channel, label=f"ch{channel}", hw_id=hw_id_of(backend, cfg))
                )
        if not channels:
            continue
        out.append(
            DeviceInfo(
                interface="ixxat",
                vendor="IXXAT",
                model="CAN interface",
                serial=uhid if isinstance(uhid, str) and uhid else None,
                transport="USB",
                channels=tuple(channels),
            )
        )
    return out


def _enrich_kvaser(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """Group kvaser channels by serial — channels of one dongle share it.

    Virtual channels are left out: buses open Kvaser channels with
    ``accept_virtual=False``, so they could never be opened.
    """
    by_serial: OrderedDict[str, list[dict[str, object]]] = OrderedDict()
    for cfg in configs:
        if cfg.get("card_type") == _KVASER_HWTYPE_VIRTUAL:
            continue
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
            channels.append(ChannelInfo(channel=channel, label=label, hw_id=hw_id_of(backend, cfg)))
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
                    hw_id=hw_id_of(backend, cfg),
                )
            )
        if not channels:
            continue
        # Every adapter nobody numbered shares the factory-default ID.
        numbered = isinstance(dev_id, int) and dev_id not in PCAN_DEFAULT_DEVICE_IDS
        serial = f"{dev_id:08x}" if numbered else None
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


def _enrich_gs_usb(backend: str, configs: list[dict[str, object]]) -> list[DeviceInfo]:
    """One single-channel device per adapter.

    The scan reads only the USB serial, so there is no product name; the
    index in the model keeps two adapters apart, clones sharing a serial too.
    """
    out: list[DeviceInfo] = []
    for cfg in configs:
        channel = cfg.get("channel")
        if not isinstance(channel, int):
            continue
        out.append(
            DeviceInfo(
                interface="gs_usb",
                vendor="candleLight",
                model=f"gs_usb adapter #{channel}",
                transport="USB",
                channels=(ChannelInfo(channel=channel, label="ch0", hw_id=hw_id_of(backend, cfg)),),
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
    "gs_usb": _enrich_gs_usb,
    "ixxat": _enrich_ixxat,
}
