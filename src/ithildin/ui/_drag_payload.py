# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Drag-and-drop MIME payload formats.

Two MIME types, two payload families:

- ``application/x-ithildin-signal`` carries ``SignalPayload`` (a single
  message·signal pair) or ``MessagePayload`` (a whole message). Both
  use the same MIME type because every existing drop target — Plot,
  Monitor, Frame Inspector, Data view — accepts either, dispatching
  on the ``kind`` discriminator.
- ``application/x-ithildin-dbc`` carries ``DbcPayload`` (a loaded DBC
  file). Bus rows in the left dock are the only drop target. A
  separate MIME type means a Plot won't ever try to interpret a DBC
  drop, and a Bus row won't ever try to bind a Signal drop — the
  routing falls out of `hasFormat()` checks instead of runtime
  `kind` switches.
- ``application/x-ithildin-bus`` carries ``BusPayload`` (a single bus
  row). Used to drag a bus from the left dock's Buses tree onto the
  Bus-trace window.

The wire format is a JSON array of payload dicts. A single-item drag
is still a one-element list; multi-select drags pack every selected
row into the same array. Drop sites either iterate the list (see
``SignalDropArea.dropEvent``) or take the first element for handlers
that are inherently single-target (``BusTreeView.dropEvent``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from PySide6.QtCore import QMimeData

SIGNAL_MIME = "application/x-ithildin-signal"
DBC_MIME = "application/x-ithildin-dbc"
BUS_MIME = "application/x-ithildin-bus"


@dataclass(frozen=True, slots=True)
class SignalPayload:
    kind: Literal["signal"] = "signal"
    message: str = ""
    message_id: int = 0
    signal: str = ""
    unit: str = ""

    def path(self) -> str:
        return f"{self.message}.{self.signal}"


@dataclass(frozen=True, slots=True)
class MessagePayload:
    kind: Literal["message"] = "message"
    message: str = ""
    message_id: int = 0


@dataclass(frozen=True, slots=True)
class DbcPayload:
    kind: Literal["dbc"] = "dbc"
    path: str = ""


@dataclass(frozen=True, slots=True)
class BusPayload:
    kind: Literal["bus"] = "bus"
    bus_name: str = ""


SignalLike = SignalPayload | MessagePayload
Payload = SignalLike | DbcPayload | BusPayload


def pack(payload: Payload) -> bytes:
    return json.dumps(asdict(payload)).encode("utf-8")


def parse(raw: bytes | bytearray | memoryview) -> Payload | None:
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _from_dict(data)


def _from_dict(data: dict[str, Any]) -> Payload | None:
    kind = data.get("kind")
    if kind == "signal":
        try:
            return SignalPayload(
                message=str(data["message"]),
                message_id=int(data.get("message_id", 0)),
                signal=str(data["signal"]),
                unit=str(data.get("unit", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "message":
        try:
            return MessagePayload(
                message=str(data["message"]),
                message_id=int(data.get("message_id", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "dbc":
        try:
            return DbcPayload(path=str(data["path"]))
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "bus":
        try:
            return BusPayload(bus_name=str(data["bus_name"]))
        except (KeyError, TypeError, ValueError):
            return None
    return None


def pack_into_mime(payloads: Sequence[Payload]) -> QMimeData:
    """Serialise a non-empty sequence of payloads onto a fresh ``QMimeData``.

    All payloads must share a MIME family — the function infers the
    target format from the first payload. An empty input produces a
    blank ``QMimeData`` (the caller will typically have already chosen
    not to start a drag in that case).
    """
    mime = QMimeData()
    if not payloads:
        return mime
    raw = json.dumps([asdict(p) for p in payloads]).encode("utf-8")
    first = payloads[0]
    if isinstance(first, DbcPayload):
        mime.setData(DBC_MIME, raw)
    elif isinstance(first, BusPayload):
        mime.setData(BUS_MIME, raw)
    else:
        mime.setData(SIGNAL_MIME, raw)
    return mime


def parse_from_mime(mime: QMimeData) -> list[Payload]:
    """Decode an ithildin MIME packet into a list of payloads.

    Returns ``[]`` when the MIME has no recognised ithildin format or
    when the body is malformed. Individual malformed items inside a
    well-formed JSON list are skipped; the rest still come through.

    Drop targets that only care about one payload family check
    ``mime.hasFormat(SIGNAL_MIME)`` etc. directly before calling.
    """
    for fmt in (SIGNAL_MIME, DBC_MIME, BUS_MIME):
        if mime.hasFormat(fmt):
            return _parse_list(bytes(mime.data(fmt).data()))
    return []


def _parse_list(raw: bytes) -> list[Payload]:
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[Payload] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        payload = _from_dict(item)
        if payload is not None:
            out.append(payload)
    return out
