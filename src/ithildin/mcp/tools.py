# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""MCP tool implementations.

Plain Python functions with simple typed signatures. The bridge layer
wraps these for FastMCP exposure; the dialog uses some of them directly
to render tables.

Threading: tools are called from the asyncio worker thread. App state
(``BusReader``, ``Database``, ``AllowlistStore``) is mutated on the GUI
thread. Reads are eventually-consistent — the worst case for a tool is
returning stale-by-microseconds data, which is fine for an observation
API. ``AllowlistStore`` takes its own lock for the send path, where a
torn read would matter.

Note the ``agent`` argument on the write tools is self-declared by the
caller, so ``AllowRule.agent_filter`` is a convenience for separating
well-behaved agents — never an authentication boundary. The bearer token
is the only real gate.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import can

from ithildin.app_state.bus_reader import BusReader
from ithildin.app_state.database import Database
from ithildin.core.transmit import encode_tx_message, multiplexer_choices, signal_range
from ithildin.core.types import FrameRecord
from ithildin.mcp.audit import AuditWriter
from ithildin.mcp.permissions import (
    AllowlistStore,
    SendFrameRequest,
    SendSignalRequest,
    SignalRange,
)

# Classic CAN limits (v0.1 is not CAN FD). Standard frames carry an 11-bit
# ID, extended frames a 29-bit ID; the payload is at most 8 bytes.
_MAX_STD_ID = 0x7FF
_MAX_EXT_ID = 0x1FFFFFFF
_MAX_CLASSIC_DLC = 8

# Forwards one ``can.Message`` to the named bus (``None`` = the only open
# bus). In production this is ``BusReader.send_message``.
type SendCallable = Callable[[can.Message, str | None], None]


@dataclass(frozen=True)
class McpReadContext:
    """Read-only handles passed to every tool."""

    bus_reader: BusReader
    database: Database
    allowlist: AllowlistStore
    # Where ``AuditWriter`` is writing, so ``get_audit_log`` can tail it.
    # ``None`` disables that tool's output rather than failing.
    audit_path: Path | None = None


@dataclass(frozen=True)
class McpWriteContext:
    """Read context + audit writer + send callable for write tools."""

    read: McpReadContext
    audit: AuditWriter
    send: SendCallable
    dry_run: bool = False


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def list_buses(ctx: McpReadContext) -> list[dict[str, Any]]:
    """Return every open bus with its status and how it was configured."""
    reader = ctx.bus_reader
    out: list[dict[str, Any]] = []
    for name in reader.bus_names():
        cfg = reader.last_config_for(name)
        out.append(
            {
                "name": name,
                "status": "live",
                "interface": cfg.interface if cfg else None,
                "channel": cfg.channel if cfg else None,
                "bitrate": cfg.bitrate if cfg else None,
                "listen_only": reader.is_listen_only(name),
                "dbcs": [
                    str(p)
                    for p in ctx.database.loaded_paths()
                    if name in ctx.database.buses_for_path(p)
                ],
            }
        )
    return out


def list_dbcs(ctx: McpReadContext) -> list[dict[str, Any]]:
    """Loaded DBC files with their bus binding and message count."""
    out: list[dict[str, Any]] = []
    for path in ctx.database.loaded_paths():
        buses = ctx.database.buses_for_path(path)
        decoder = ctx.database.decoder_for_bus(buses[0]) if buses else None
        message_count = len(decoder.database.messages) if decoder is not None else 0
        signal_count = (
            sum(len(m.signals) for m in decoder.database.messages) if decoder is not None else 0
        )
        out.append(
            {
                "path": str(path),
                "buses": list(buses),
                "message_count": message_count,
                "signal_count": signal_count,
            }
        )
    return out


def get_recent_messages(
    ctx: McpReadContext,
    count: int = 100,
    message_id: int | None = None,
    bus: str | None = None,
    since_timestamp: float | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``count`` recent frames, newest last.

    Filters: ``message_id`` (raw CAN ID), ``bus`` (name), and
    ``since_timestamp`` (epoch seconds, exclusive). Frames are decoded if
    a DBC is loaded for the originating bus. Multi-bus aware: reads every
    open ring and merges by timestamp.
    """
    rings = ctx.bus_reader.rings
    if bus is not None:
        rings = {name: ring for name, ring in rings.items() if name == bus}
    if not rings:
        return []
    filtered = message_id is not None or since_timestamp is not None
    merged: list[FrameRecord] = []
    for ring in rings.values():
        # Unfiltered, a tail read of ``count`` avoids walking the whole
        # 65 536-entry ring for what is usually a 100-frame question.
        # With a filter we have no choice but to scan.
        start = -1 if filtered or count < 0 else max(-1, ring.total_seq - count - 1)
        frames, _dropped = ring.read_since(start)
        merged.extend(frames)
    merged.sort(key=lambda f: f.timestamp)
    if message_id is not None:
        merged = [f for f in merged if f.arbitration_id == message_id]
    if since_timestamp is not None:
        merged = [f for f in merged if f.timestamp > since_timestamp]
    frames = merged[-count:] if count >= 0 else merged

    out: list[dict[str, Any]] = []
    for frame in frames:
        decoder = ctx.database.decoder_for_bus(frame.bus_name)
        decoded = decoder.decode(frame.arbitration_id, frame.data) if decoder else None
        message_name = decoder.message_name(frame.arbitration_id) if decoder else None
        out.append(
            {
                "bus": frame.bus_name,
                "timestamp": float(frame.timestamp),
                "id": int(frame.arbitration_id),
                "id_hex": f"0x{frame.arbitration_id:X}",
                "is_extended": bool(frame.is_extended_id),
                "is_error": bool(frame.is_error_frame),
                "data": frame.data.hex(),
                "message": message_name,
                "decoded": decoded,
            }
        )
    return out


def get_bus_stats(ctx: McpReadContext) -> dict[str, Any]:
    """Coarse bus stats — open state plus per-bus frame totals.

    Returns ``{"open": False}`` when no bus is connected. Otherwise
    ``{"open": True, "buses": [...]}`` with one entry per open bus —
    multi-bus aware. The legacy single-bus shape (``name``,
    ``total_frames``, ``ring_capacity``) is preserved at the top level
    when exactly one bus is open, for callers that haven't been
    updated yet.
    """
    reader = ctx.bus_reader
    rings = reader.rings
    if not rings:
        return {"open": False}
    buses = [
        {
            "name": name,
            "total_frames": int(ring.total_seq),
            "ring_capacity": int(ring.capacity),
        }
        for name, ring in rings.items()
    ]
    out: dict[str, Any] = {"open": True, "buses": buses}
    if len(buses) == 1:
        single = buses[0]
        out["name"] = single["name"]
        out["total_frames"] = single["total_frames"]
        out["ring_capacity"] = single["ring_capacity"]
    return out


def list_allowlist(ctx: McpReadContext) -> list[dict[str, Any]]:
    """Current allowlist rules in dict form."""
    out: list[dict[str, Any]] = []
    for r in ctx.allowlist.rules:
        out.append(
            {
                "id": r.id,
                "message_id": r.message_id,
                "message_id_hex": f"0x{r.message_id:X}",
                "signal": r.signal,
                "rate_hz": r.rate_hz,
                "agent_filter": r.agent_filter,
                "bus_name": r.bus_name,
                "enforce_signal_ranges": r.enforce_signal_ranges,
                "created_at": r.created_at.isoformat(),
            }
        )
    return out


def list_messages(ctx: McpReadContext, bus: str | None = None) -> list[dict[str, Any]]:
    """Every message in every loaded DBC — the entry point for discovery.

    Optionally restrict to the DBCs bound to ``bus``.
    """
    out: list[dict[str, Any]] = []
    for path, bus_name, decoder in _iter_decoders(ctx.database):
        if bus is not None and bus_name != bus:
            continue
        for msg in decoder.database.messages:
            out.append(
                {
                    "name": msg.name,
                    "id": int(msg.frame_id),
                    "id_hex": f"0x{msg.frame_id:X}",
                    "is_extended": bool(getattr(msg, "is_extended_frame", False)),
                    "length": int(getattr(msg, "length", 0)),
                    "signal_count": len(msg.signals),
                    "comment": getattr(msg, "comment", None),
                    "bus": bus_name,
                    "dbc_path": str(path),
                }
            )
    return out


def list_signals(ctx: McpReadContext, message: str) -> list[dict[str, Any]]:
    """Full signal metadata for one DBC message, by message name.

    Returns ``[]`` when no loaded DBC has that message.
    """
    for path, bus_name, decoder in _iter_decoders(ctx.database):
        try:
            msg = decoder.database.get_message_by_name(message)
        except KeyError:
            continue
        return [_signal_dict(msg, sig, bus_name, path) for sig in msg.signals]
    return []


def describe_signal(ctx: McpReadContext, signal_path: str) -> dict[str, Any] | None:
    """DBC metadata for ``Message.Signal``.

    Returns ``None`` if no loaded DBC contains the signal.
    """
    if "." not in signal_path:
        return None
    message_name, signal_name = signal_path.split(".", 1)
    for path, bus, decoder in _iter_decoders(ctx.database):
        try:
            msg = decoder.database.get_message_by_name(message_name)
        except KeyError:
            continue
        for sig in msg.signals:
            if sig.name == signal_name:
                return _signal_dict(msg, sig, bus, path)
    return None


def get_signal_value(ctx: McpReadContext, signal_path: str) -> dict[str, Any] | None:
    """Most recently observed value of ``Message.Signal``.

    ``None`` when the signal is unknown. When it's known but nothing has
    been captured yet, ``value`` is ``None`` and ``timestamp`` is absent.
    """
    meta = describe_signal(ctx, signal_path)
    if meta is None:
        return None
    _message_name, signal_name = signal_path.split(".", 1)
    message_id = int(meta["message_id"])
    bus = str(meta["bus"])
    out: dict[str, Any] = {
        "signal": signal_name,
        "message": meta["message"],
        "bus": bus,
        "unit": meta["unit"],
        "value": None,
    }
    ring = ctx.bus_reader.rings.get(bus)
    decoder = ctx.database.decoder_for_bus(bus)
    if ring is None or decoder is None:
        return out
    frames, _dropped = ring.read_since(-1)
    # Newest first: the first frame carrying this signal wins, so a busy
    # ring costs one decode rather than one per frame.
    for frame in reversed(frames):
        if frame.arbitration_id != message_id:
            continue
        decoded = decoder.decode(frame.arbitration_id, frame.data)
        if not decoded or signal_name not in decoded:
            continue
        out["value"] = decoded[signal_name]
        out["timestamp"] = float(frame.timestamp)
        return out
    return out


def get_audit_log(
    ctx: McpReadContext,
    count: int = 50,
    agent: str | None = None,
) -> list[dict[str, Any]]:
    """Tail of the send-decision audit log, oldest first.

    Lets an agent see why its own send was denied. Optionally filter to
    one ``agent`` name. Returns ``[]`` when no log exists yet.
    """
    if ctx.audit_path is None or not ctx.audit_path.exists():
        return []
    lines = _tail_lines(ctx.audit_path, count if agent is None else max(count * 20, count))
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if agent is not None and event.get("agent") != agent:
            continue
        out.append(event)
    return out[-count:] if count >= 0 else out


def _tail_lines(path: Path, count: int, block: int = 64 * 1024) -> list[str]:
    """Last ``count`` lines of ``path``, read backwards from the end.

    The audit log rotates at 50 MB; slurping it to answer "last 50" is
    not an option.
    """
    if count <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        chunks: list[bytes] = []
        newlines = 0
        while end > 0 and newlines <= count:
            size = min(block, end)
            end -= size
            f.seek(end)
            chunk = f.read(size)
            newlines += chunk.count(b"\n")
            chunks.append(chunk)
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return text.splitlines()[-count:]


def _iter_decoders(database: Database) -> Iterator[tuple[Path, str, Any]]:
    """Yield ``(dbc_path, bus_name, decoder)`` for every loaded binding."""
    for path in database.loaded_paths():
        for bus in database.buses_for_path(path):
            decoder = database.decoder_for_bus(bus)
            if decoder is not None:
                yield path, bus, decoder


def _signal_dict(msg: Any, sig: Any, bus: str, path: Path) -> dict[str, Any]:
    # The range is what the bits can carry, narrowed by the DBC's declared
    # range: the same bounds send_signal enforces. A multiplexer selector's
    # choices are the IDs its message defines.
    minimum, maximum = signal_range(sig)
    choices = multiplexer_choices(msg, sig) if sig.is_multiplexer else None
    if not choices and getattr(sig, "choices", None):
        choices = {int(k): str(v) for k, v in sig.choices.items()}
    return {
        "message": msg.name,
        "message_id": int(msg.frame_id),
        "message_id_hex": f"0x{msg.frame_id:X}",
        "signal": sig.name,
        "unit": getattr(sig, "unit", "") or "",
        "minimum": minimum,
        "maximum": maximum,
        "length_bits": int(getattr(sig, "length", 0)),
        "scale": _maybe_float(getattr(sig, "scale", None)),
        "offset": _maybe_float(getattr(sig, "offset", None)),
        "is_signed": bool(getattr(sig, "is_signed", False)),
        "choices": choices or None,
        "is_multiplexer": bool(sig.is_multiplexer),
        "multiplexer_signal": sig.multiplexer_signal,
        "multiplexer_ids": [int(i) for i in sig.multiplexer_ids] if sig.multiplexer_ids else None,
        "bus": bus,
        "dbc_path": str(path),
    }


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Write tools
# --------------------------------------------------------------------------


def send_signal(
    ctx: McpWriteContext,
    message: str,
    signal: str,
    value: int | float,
    agent: str = "",
    bus: str | None = None,
) -> dict[str, Any]:
    """Encode + send one signal value, gated by ``AllowlistStore``.

    ``bus`` names the target bus; omit it when exactly one is open.
    Returns a dict with ``ok`` plus, on failure, a ``reason``.
    Every attempt — allowed or denied — is appended to the audit log.
    An int ``value`` stays an int all the way to the encoder, so a 64-bit
    signal keeps the low bits a float would round away.
    """
    bus_name = bus if bus is not None else ctx.read.bus_reader.bus_name
    if bus_name is not None and ctx.read.bus_reader.is_listen_only(bus_name):
        return _audit_and_return(
            ctx,
            call="send_signal",
            args={"message": message, "signal": signal, "value": value, "bus": bus_name},
            agent=agent,
            ok=False,
            reason="BUS_READ_ONLY",
            rule_id=None,
            detail=f"bus {bus_name!r} is listen-only",
        )
    if isinstance(value, float) and not math.isfinite(value):
        return _audit_and_return(
            ctx,
            call="send_signal",
            args={"message": message, "signal": signal, "value": value, "bus": bus_name},
            agent=agent,
            ok=False,
            reason="BAD_VALUE",
            rule_id=None,
            detail=f"value {value!r} is not finite",
        )
    decoder, message_obj = _resolve_message(ctx.read.database, bus_name, message)
    if message_obj is None:
        return _audit_and_return(
            ctx,
            call="send_signal",
            args={"message": message, "signal": signal, "value": value, "bus": bus_name},
            agent=agent,
            ok=False,
            reason="UNKNOWN_MESSAGE",
            rule_id=None,
        )

    request = SendSignalRequest(
        message_id=int(message_obj.frame_id),
        signal=signal,
        value=value,
        agent=agent,
        bus_name=bus_name,
    )

    def range_lookup(_mid: int, sig_name: str) -> SignalRange | None:
        for sig in message_obj.signals:
            if sig.name != sig_name:
                continue
            mn, mx = signal_range(sig)
            if mn is not None and mx is not None:
                return SignalRange(minimum=mn, maximum=mx)
        return None

    decision = ctx.read.allowlist.check_signal(request, range_lookup=range_lookup)
    if not decision.allowed:
        return _audit_and_return(
            ctx,
            call="send_signal",
            args={"message": message, "signal": signal, "value": value, "bus": bus_name},
            agent=agent,
            ok=False,
            reason=decision.reason,
            rule_id=decision.rule_id,
            detail=decision.detail,
        )

    if ctx.dry_run:
        return _audit_and_return(
            ctx,
            call="send_signal",
            args={"message": message, "signal": signal, "value": value, "bus": bus_name},
            agent=agent,
            ok=True,
            reason="DRY_RUN",
            rule_id=decision.rule_id,
        )

    try:
        assert decoder is not None
        data = encode_tx_message(decoder.database, message_obj, {signal: value})
    except Exception as exc:
        return _audit_and_return(
            ctx,
            call="send_signal",
            args={"message": message, "signal": signal, "value": value, "bus": bus_name},
            agent=agent,
            ok=False,
            reason="ENCODE_FAILED",
            rule_id=decision.rule_id,
            detail=str(exc),
        )

    can_msg = can.Message(
        arbitration_id=int(message_obj.frame_id),
        data=bytes(data),
        is_extended_id=bool(getattr(message_obj, "is_extended_frame", False)),
    )
    try:
        ctx.send(can_msg, bus_name)
    except Exception as exc:
        return _audit_and_return(
            ctx,
            call="send_signal",
            args={"message": message, "signal": signal, "value": value, "bus": bus_name},
            agent=agent,
            ok=False,
            reason="BUS_ERROR",
            rule_id=decision.rule_id,
            detail=str(exc),
        )

    return _audit_and_return(
        ctx,
        call="send_signal",
        args={"message": message, "signal": signal, "value": value, "bus": bus_name},
        agent=agent,
        ok=True,
        reason="OK",
        rule_id=decision.rule_id,
    )


def send_frame(
    ctx: McpWriteContext,
    message_id: int,
    data_hex: str,
    is_extended: bool = False,
    agent: str = "",
    bus: str | None = None,
) -> dict[str, Any]:
    """Send a raw frame. Gated by a wildcard-signal allowlist rule for the CAN ID.

    A specific-signal rule does NOT authorise raw send (handoff safety).
    ``bus`` names the target bus; omit it when exactly one is open.
    """
    bus_name = bus if bus is not None else ctx.read.bus_reader.bus_name
    args = {
        "message_id": message_id,
        "data": data_hex,
        "is_extended": is_extended,
        "bus": bus_name,
    }
    if bus_name is not None and ctx.read.bus_reader.is_listen_only(bus_name):
        return _audit_and_return(
            ctx,
            call="send_frame",
            args=args,
            agent=agent,
            ok=False,
            reason="BUS_READ_ONLY",
            rule_id=None,
            detail=f"bus {bus_name!r} is listen-only",
        )
    max_id = _MAX_EXT_ID if is_extended else _MAX_STD_ID
    if not (0 <= message_id <= max_id):
        return _audit_and_return(
            ctx,
            call="send_frame",
            args=args,
            agent=agent,
            ok=False,
            reason="BAD_ID",
            rule_id=None,
            detail=f"id {message_id} out of range for a {'29' if is_extended else '11'}-bit frame",
        )
    try:
        data = bytes.fromhex(data_hex.replace(" ", ""))
    except ValueError as exc:
        return _audit_and_return(
            ctx,
            call="send_frame",
            args=args,
            agent=agent,
            ok=False,
            reason="BAD_DATA",
            rule_id=None,
            detail=str(exc),
        )
    if len(data) > _MAX_CLASSIC_DLC:
        return _audit_and_return(
            ctx,
            call="send_frame",
            args=args,
            agent=agent,
            ok=False,
            reason="BAD_LENGTH",
            rule_id=None,
            detail=f"{len(data)} bytes exceeds the classic-CAN max of {_MAX_CLASSIC_DLC}",
        )

    request = SendFrameRequest(
        message_id=int(message_id),
        agent=agent,
        bus_name=bus_name,
    )
    decision = ctx.read.allowlist.check_frame(request)
    if not decision.allowed:
        return _audit_and_return(
            ctx,
            call="send_frame",
            args=args,
            agent=agent,
            ok=False,
            reason=decision.reason,
            rule_id=decision.rule_id,
            detail=decision.detail,
        )

    if ctx.dry_run:
        return _audit_and_return(
            ctx,
            call="send_frame",
            args=args,
            agent=agent,
            ok=True,
            reason="DRY_RUN",
            rule_id=decision.rule_id,
        )

    can_msg = can.Message(
        arbitration_id=int(message_id),
        data=data,
        is_extended_id=bool(is_extended),
    )
    try:
        ctx.send(can_msg, bus_name)
    except Exception as exc:
        return _audit_and_return(
            ctx,
            call="send_frame",
            args=args,
            agent=agent,
            ok=False,
            reason="BUS_ERROR",
            rule_id=decision.rule_id,
            detail=str(exc),
        )

    return _audit_and_return(
        ctx,
        call="send_frame",
        args=args,
        agent=agent,
        ok=True,
        reason="OK",
        rule_id=decision.rule_id,
    )


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _resolve_message(
    database: Database, bus_name: str | None, message_name: str
) -> tuple[Any, Any]:
    """Find a DBC message by name, scanning all loaded buses."""
    target_buses = (
        [bus_name]
        if bus_name is not None
        else [b for p in database.loaded_paths() for b in database.buses_for_path(p)]
    )
    for bus in target_buses:
        decoder = database.decoder_for_bus(bus)
        if decoder is None:
            continue
        try:
            msg = decoder.database.get_message_by_name(message_name)
        except KeyError:
            continue
        return decoder, msg
    return None, None


def _audit_and_return(
    ctx: McpWriteContext,
    *,
    call: str,
    args: dict[str, Any],
    agent: str,
    ok: bool,
    reason: str,
    rule_id: int | None,
    detail: str = "",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "call": call,
        "args": args,
        "agent": agent,
        "decision": "ALLOW" if ok else "DENY",
        "reason": reason,
    }
    if rule_id is not None:
        event["rule_id"] = rule_id
    if detail:
        event["detail"] = detail
    if ctx.dry_run and ok:
        event["dry_run"] = True
    ctx.audit.append(event)
    out: dict[str, Any] = {"ok": ok, "reason": reason}
    if rule_id is not None:
        out["rule_id"] = rule_id
    if detail:
        out["detail"] = detail
    if ctx.dry_run and ok:
        out["dry_run"] = True
    return out
