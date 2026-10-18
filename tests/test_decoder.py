# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.decoder.Decoder``."""

from __future__ import annotations

from pathlib import Path

from ithildin.core.decoder import Decoder


def test_decode_known_message(example_dbc_path: Path) -> None:
    decoder = Decoder.from_files(example_dbc_path)
    # EngineSpeed = 800 rpm (factor 0.125 -> raw 6400 = 0x1900),
    # IdleSetpoint = 700 rpm.
    raw_engine_speed = 6400  # 0x1900
    raw_idle = 700  # 0x02BC
    payload = raw_engine_speed.to_bytes(2, "little") + raw_idle.to_bytes(2, "little") + bytes(4)
    decoded = decoder.decode(0x100, payload)
    assert decoded is not None
    assert decoded["EngineSpeed"] == 800
    assert decoded["IdleSetpoint"] == 700


def test_decode_unknown_id_returns_none(example_dbc_path: Path) -> None:
    decoder = Decoder.from_files(example_dbc_path)
    assert decoder.decode(0x7FF, bytes(8)) is None


def test_message_name_lookup(example_dbc_path: Path) -> None:
    decoder = Decoder.from_files(example_dbc_path)
    assert decoder.message_name(0x100) == "EngineCommand"
    assert decoder.message_name(0x200) == "BodyStatus"
    assert decoder.message_name(0x7FF) is None
