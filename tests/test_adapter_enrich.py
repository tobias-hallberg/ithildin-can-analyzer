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
    devices = enrich("socketcan", [{"interface": "socketcan", "channel": "can0"}])
    assert len(devices) == 1
    dev = devices[0]
    assert dev.vendor == "Linux SocketCAN"
    assert dev.serial is None
    assert dev.firmware is None
    # Default enricher probes nothing beyond interface + channel.
    assert dev.status is None
    assert dev.capabilities is None
    assert [(c.channel, c.hw_id) for c in dev.channels] == [("can0", None)]


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
    """Without serials the index keeps two identical adapters apart (the
    dialog de-duplicates devices by ``key``)."""
    devices = enrich(
        "gs_usb",
        [
            {"interface": "gs_usb", "channel": 0, "serial": None},
            {"interface": "gs_usb", "channel": 1, "serial": None},
        ],
    )
    assert len({d.key for d in devices}) == 2
    assert [d.vendor for d in devices] == ["candleLight", "candleLight"]
    assert [[(c.channel, c.label) for c in d.channels] for d in devices] == [
        [(0, "ch0")],
        [(1, "ch0")],
    ]
    assert all(d.transport == "USB" for d in devices)
    assert all(d.status is None and d.capabilities is None for d in devices)


# ---- Adapter identity ----


def test_two_ixxat_devices_with_a_channel_0_each_stay_apart() -> None:
    """Each device numbers its channels from 0: the hardware id tells them apart."""
    devices = enrich(
        "ixxat",
        [
            {"interface": "ixxat", "channel": 0, "unique_hardware_id": "HW100001"},
            {"interface": "ixxat", "channel": 1, "unique_hardware_id": "HW100001"},
            {"interface": "ixxat", "channel": 0, "unique_hardware_id": "HW200002"},
        ],
    )
    assert [(d.serial, [(c.channel, c.label, c.hw_id) for c in d.channels]) for d in devices] == [
        ("HW100001", [(0, "ch0", "HW100001"), (1, "ch1", "HW100001")]),
        ("HW200002", [(0, "ch0", "HW200002")]),
    ]
    assert len({d.key for d in devices}) == 2


def test_pcan_channels_carry_device_id_and_controller() -> None:
    devices = enrich(
        "pcan",
        [
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS1",
                "device_id": 0x2A,
                "controller_number": 0,
                "device_name": "PCAN-USB",
            },
        ],
    )
    assert devices[0].channels[0].hw_id == "0000002a.0"
    assert devices[0].display_label == "PEAK-System · PCAN-USB  (device ID 0x2A)"


def test_pcan_factory_default_device_id_identifies_nothing() -> None:
    devices = enrich(
        "pcan",
        [
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS1",
                "device_id": 0xFF,
                "controller_number": 0,
                "device_name": "PCAN-USB",
            },
        ],
    )
    assert devices[0].channels[0].hw_id is None
    assert devices[0].serial is None
    assert devices[0].display_label == "PEAK-System · PCAN-USB"


def test_an_id_two_channels_share_is_dropped() -> None:
    """Two PCAN-USBs given the same device ID: neither can be pinned."""
    devices = enrich(
        "pcan",
        [
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS1",
                "device_id": 5,
                "controller_number": 0,
            },
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS2",
                "device_id": 5,
                "controller_number": 0,
            },
            {
                "interface": "pcan",
                "channel": "PCAN_USBBUS3",
                "device_id": 6,
                "controller_number": 0,
            },
        ],
    )
    ids = {c.channel: c.hw_id for d in devices for c in d.channels}
    assert ids == {"PCAN_USBBUS1": None, "PCAN_USBBUS2": None, "PCAN_USBBUS3": "00000006.0"}


def test_gs_usb_serial_identifies_the_adapter_unless_a_clone_shares_it() -> None:
    devices = enrich(
        "gs_usb",
        [
            {"interface": "gs_usb", "channel": 0, "serial": "CLONE"},
            {"interface": "gs_usb", "channel": 1, "serial": "CLONE"},
            {"interface": "gs_usb", "channel": 2, "serial": "206C3EA1"},
        ],
    )
    assert [d.channels[0].hw_id for d in devices] == [None, None, "206C3EA1"]
    assert len({d.key for d in devices}) == 3
