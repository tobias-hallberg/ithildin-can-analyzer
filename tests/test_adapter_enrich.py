# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.adapter_enrich`` — raw config dicts → device metadata.

The fixture dicts mirror the real shapes ``can.detect_available_configs``
returns for kvaser and pcan (verified against python-can 4.6.1). The
point is that enrichment parses what each backend actually ships and
degrades to ``None`` for anything missing rather than raising.
"""

from __future__ import annotations

from ithildin.ui.adapter_enrich import enrich


def test_kvaser_groups_channels_by_serial() -> None:
    devices = enrich(
        "kvaser",
        [
            {
                "interface": "kvaser",
                "channel": 0,
                "device_name": "Leaf Pro HS v2",
                "serial": 84621,
                "dongle_channel": 1,
            },
            {
                "interface": "kvaser",
                "channel": 1,
                "device_name": "Leaf Pro HS v2",
                "serial": 84621,
                "dongle_channel": 2,
            },
        ],
    )
    assert len(devices) == 1
    dev = devices[0]
    assert dev.vendor == "Kvaser"
    assert dev.model == "Leaf Pro HS v2"
    assert dev.serial == "84621"
    assert dev.transport == "USB"
    assert [c.channel for c in dev.channels] == [0, 1]
    assert [c.label for c in dev.channels] == ["ch0", "ch1"]
    # Unprobed for kvaser — must not fake a default.
    assert dev.status is None
    assert dev.capabilities is None
    assert dev.firmware is None


def test_kvaser_virtual_channels_are_dropped() -> None:
    """A bus opens Kvaser channels with ``accept_virtual=False``, so a
    Virtual channel (card type canHWTYPE_VIRTUAL) is never offered."""
    devices = enrich(
        "kvaser",
        [
            {
                "interface": "kvaser",
                "channel": 0,
                "device_name": "Kvaser Virtual CAN Driver",
                "serial": 0,
                "dongle_channel": 1,
                "card_type": 1,
            },
            {
                "interface": "kvaser",
                "channel": 1,
                "device_name": "Kvaser Virtual CAN Driver",
                "serial": 0,
                "dongle_channel": 2,
                "card_type": 1,
            },
            {
                "interface": "kvaser",
                "channel": 2,
                "device_name": "Kvaser Leaf Light v2",
                "serial": 12345,
                "dongle_channel": 1,
                "card_type": 48,
            },
        ],
    )
    assert [(d.model, [c.channel for c in d.channels]) for d in devices] == [
        ("Kvaser Leaf Light v2", [2])
    ]


def test_kvaser_reads_firmware_folded_in_by_prober() -> None:
    # The prober adds a "firmware" key (see _add_kvaser_channel_data); the
    # enricher just surfaces it.
    devices = enrich(
        "kvaser",
        [
            {
                "interface": "kvaser",
                "channel": 0,
                "device_name": "Leaf Pro HS v2",
                "serial": 84621,
                "dongle_channel": 1,
                "firmware": "3.18.0.123",
            },
        ],
    )
    assert devices[0].firmware == "3.18.0.123"


def test_pcan_reads_fd_capability_and_channel_status() -> None:
    devices = enrich(
        "pcan",
        [
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS1",
                "supports_fd": True,
                "device_id": 7,
                "device_name": "PCAN-USB FD",
                "channel_condition": 0x02,  # occupied
            },
        ],
    )
    assert len(devices) == 1
    dev = devices[0]
    assert dev.vendor == "PEAK-System"
    assert dev.capabilities == ("CAN 2.0", "CAN FD")
    assert dev.serial == "00000007"
    assert dev.channels[0].status == "in-use"
    assert dev.status == "in-use"


def test_pcan_available_channel_is_free() -> None:
    devices = enrich(
        "pcan",
        [
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS1",
                "device_id": 1,
                "channel_condition": 0x01,  # available
            }
        ],
    )
    assert devices[0].channels[0].status == "free"
    assert devices[0].status == "available"


def test_unknown_backend_degrades_to_default() -> None:
    devices = enrich("ixxat", [{"interface": "ixxat", "channel": "0"}])
    assert len(devices) == 1
    dev = devices[0]
    assert dev.vendor == "IXXAT"
    assert dev.serial is None
    assert dev.firmware is None
    # Default enricher probes nothing beyond interface + channel.
    assert dev.status is None
    assert dev.capabilities is None
    assert [c.channel for c in dev.channels] == ["0"]


def test_missing_optional_keys_do_not_raise() -> None:
    # kvaser dict without device_name / serial / dongle_channel.
    devices = enrich("kvaser", [{"interface": "kvaser", "channel": 0}])
    assert len(devices) == 1
    assert devices[0].serial is None
    assert devices[0].model == "Kvaser device"
    assert devices[0].channels[0].label == "0"


def test_config_with_bad_channel_type_is_skipped() -> None:
    assert enrich("pcan", [{"interface": "pcan", "channel": None, "device_id": 1}]) == []


def test_gs_usb_one_device_per_adapter() -> None:
    """The scan reads no descriptors, so the index keeps two identical
    adapters apart (the dialog de-duplicates devices by ``key``)."""
    devices = enrich(
        "gs_usb",
        [{"interface": "gs_usb", "channel": 0}, {"interface": "gs_usb", "channel": 1}],
    )
    assert len({d.key for d in devices}) == 2
    assert [d.vendor for d in devices] == ["candleLight", "candleLight"]
    assert [[(c.channel, c.label) for c in d.channels] for d in devices] == [
        [(0, "ch0")],
        [(1, "ch0")],
    ]
    assert all(d.transport == "USB" for d in devices)
    assert all(d.status is None and d.capabilities is None for d in devices)
