# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui._drag_payload`` — negative paths.

Positive round-trip cases live in ``test_left_dock.py`` since they
verify the same payloads the dock's models hand out. This file covers
the malformed-input paths so any future refactor of ``parse`` can't
regress to silently returning a half-built payload.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QMimeData

from ithildin.ui._drag_payload import (
    DBC_MIME,
    SIGNAL_MIME,
    DbcPayload,
    MessagePayload,
    SignalPayload,
    pack,
    pack_into_mime,
    parse,
    parse_from_mime,
)


def test_parse_invalid_utf8_returns_none() -> None:
    assert parse(b"\xff\xfe\xfd") is None


def test_parse_non_json_returns_none() -> None:
    assert parse(b"not json") is None


def test_parse_top_level_list_returns_none() -> None:
    # Valid JSON but the wrong shape — must not raise.
    assert parse(b'["signal"]') is None


def test_parse_signal_missing_required_field_returns_none() -> None:
    raw = json.dumps({"kind": "signal", "message": "X"}).encode()  # no signal key
    assert parse(raw) is None


def test_parse_signal_non_int_message_id_returns_none() -> None:
    raw = json.dumps(
        {"kind": "signal", "message": "X", "signal": "Y", "message_id": "nope"}
    ).encode()
    assert parse(raw) is None


def test_parse_message_missing_required_field_returns_none() -> None:
    raw = json.dumps({"kind": "message", "message_id": 1}).encode()  # no message
    assert parse(raw) is None


def test_parse_no_kind_returns_none() -> None:
    raw = json.dumps({"message": "X", "signal": "Y"}).encode()
    assert parse(raw) is None


def test_parse_from_mime_without_format_returns_empty() -> None:
    mime = QMimeData()
    mime.setData("application/x-different-format", b"{}")
    assert parse_from_mime(mime) == []


def test_pack_round_trip_preserves_unit_default() -> None:
    # Unit defaults to empty string; pack/parse must keep that.
    payload = SignalPayload(message="M", message_id=1, signal="S")
    parsed = parse(pack(payload))
    assert parsed == payload


def test_pack_into_mime_sets_correct_format() -> None:
    mime = pack_into_mime([MessagePayload(message="M", message_id=2)])
    assert mime.hasFormat(SIGNAL_MIME)
    assert mime.formats() == [SIGNAL_MIME]


def test_pack_into_mime_round_trips_multiple_signals() -> None:
    """Multi-select drag: the array format preserves order and identity."""
    a = SignalPayload(message="M1", message_id=1, signal="A")
    b = SignalPayload(message="M2", message_id=2, signal="B")
    parsed = parse_from_mime(pack_into_mime([a, b]))
    assert parsed == [a, b]


def test_pack_into_mime_empty_sequence_yields_no_format() -> None:
    """A drag with zero selected rows produces a blank QMimeData."""
    mime = pack_into_mime([])
    assert not mime.hasFormat(SIGNAL_MIME)
    assert parse_from_mime(mime) == []


def test_dbc_payload_round_trip() -> None:
    payload = DbcPayload(path=r"C:\projects\engine.dbc")
    parsed = parse(pack(payload))
    assert parsed == payload


def test_dbc_pack_into_mime_uses_dbc_format() -> None:
    """DBC payloads use a separate MIME from signal/message payloads."""
    mime = pack_into_mime([DbcPayload(path="x.dbc")])
    assert mime.hasFormat(DBC_MIME)
    assert not mime.hasFormat(SIGNAL_MIME)


def test_dbc_payload_routed_back_through_parse_from_mime() -> None:
    payload = DbcPayload(path="x.dbc")
    assert parse_from_mime(pack_into_mime([payload])) == [payload]


def test_parse_from_mime_unknown_format_returns_empty() -> None:
    mime = QMimeData()
    mime.setData("application/x-something-else", b"{}")
    assert parse_from_mime(mime) == []


def test_parse_from_mime_legacy_dict_body_returns_empty() -> None:
    """The wire format is a list; a stray dict body must not silently work."""
    mime = QMimeData()
    mime.setData(SIGNAL_MIME, json.dumps({"kind": "bogus"}).encode("utf-8"))
    assert parse_from_mime(mime) == []


def test_parse_from_mime_skips_malformed_items() -> None:
    """A list with one good and one bad payload yields just the good one."""
    raw = json.dumps(
        [
            {"kind": "signal", "message": "M", "message_id": 1, "signal": "A"},
            {"kind": "signal"},  # missing required fields
        ]
    ).encode("utf-8")
    mime = QMimeData()
    mime.setData(SIGNAL_MIME, raw)
    parsed = parse_from_mime(mime)
    assert parsed == [SignalPayload(message="M", message_id=1, signal="A")]


def test_parse_dbc_missing_path_returns_none() -> None:
    raw = json.dumps({"kind": "dbc"}).encode()
    assert parse(raw) is None
