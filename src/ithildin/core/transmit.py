# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Transmit engine — periodic + one-shot TX driven by the TX window.

This module owns two responsibilities:

1. **Data model.** :class:`TxSetup` is the saveable rig of transmits the
   user has composed in the TX window. :class:`TxMessage` is one row;
   :class:`TxSignalSpec` is one signal's metadata used by the editor to
   pick the right control kind.

2. **Engine.** :class:`TxScheduler` reconciles a :class:`TxSetup` against
   live ``python-can`` cyclic tasks via :meth:`BusReader.start_periodic`
   and ``CyclicSendTask.modify_data`` — see handoff §2 for the two-tier
   master / per-row semantics.

The shared :func:`encode_tx_message` helper is also called by the MCP
``send_signal`` tool so the two transmit paths can never drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import can
import cantools.database
from PySide6.QtCore import QObject, Signal

from ithildin.app_state.bus_reader import BusReader, BusReadOnlyError
from ithildin.app_state.database import Database

TxSignalKind = Literal[
    "bool",
    "enum-seg",
    "enum-dd",
    "num-sm",
    "num-lg",
    "signed",
    "hex",
    "auto",
]
"""One of eight control archetypes, per handoff §7. Drives the editor
widget the TX window mounts for each signal."""


TxMode = Literal["one-shot", "periodic"]


@dataclass(frozen=True)
class TxSignalSpec:
    """Cached DBC metadata for one signal in a TX message + its current value."""

    name: str
    kind: TxSignalKind
    value: float | int | bool | str
    length_bits: int
    is_signed: bool
    minimum: float | None
    maximum: float | None
    scale: float
    offset: float
    unit: str
    choices: dict[int, str] | None
    initial: float | None


@dataclass
class TxMessage:
    """One transmit row in the TX window's setup."""

    bus_name: str
    message_id: int
    message_name: str
    mode: TxMode = "one-shot"
    period_ms: int = 100
    active: bool = False
    signal_values: dict[str, float | int | bool | str] = field(default_factory=dict)
    kind_overrides: dict[str, TxSignalKind] = field(default_factory=dict)


@dataclass
class TxSetup:
    """The saveable TX window content."""

    master_enabled: bool = False
    transmits: list[TxMessage] = field(default_factory=list)


# --------------------------------------------------------------------------
# Kind heuristic
# --------------------------------------------------------------------------


def infer_kind(sig: cantools.database.Signal) -> TxSignalKind:
    """Pick a control kind for ``sig`` per handoff §7.2.

    Per-signal overrides on the :class:`TxMessage` win over this — call
    :func:`resolve_kind` when you have a :class:`TxMessage` in hand.
    """
    if int(getattr(sig, "length", 0) or 0) == 1:
        return "bool"
    choices = getattr(sig, "choices", None)
    if choices:
        return "enum-seg" if len(choices) <= 4 else "enum-dd"
    mn = _maybe_float(getattr(sig, "minimum", None))
    mx = _maybe_float(getattr(sig, "maximum", None))
    if bool(getattr(sig, "is_signed", False)) and mn is not None and mx is not None and mn < 0 < mx:
        return "signed"
    autofill = _autofill_attribute(sig)
    if autofill in ("counter", "crc"):
        return "auto"
    if mn is not None and mx is not None and (mx - mn) > 256:
        return "num-lg"
    return "num-sm"


def resolve_kind(tx: TxMessage, sig: cantools.database.Signal) -> TxSignalKind:
    """Per-message override → :func:`infer_kind` fallback."""
    override = tx.kind_overrides.get(sig.name)
    if override is not None:
        return override
    return infer_kind(sig)


def build_signal_spec(
    sig: cantools.database.Signal,
    *,
    kind: TxSignalKind,
    current_value: float | int | bool | str | None,
) -> TxSignalSpec:
    """Snapshot one signal's metadata for the editor."""
    initial = _maybe_float(getattr(sig, "initial", None))
    if current_value is not None:
        value: float | int | bool | str = current_value
    elif initial is not None:
        value = initial
    else:
        value = 0
    return TxSignalSpec(
        name=str(sig.name),
        kind=kind,
        value=value,
        length_bits=int(getattr(sig, "length", 0) or 0),
        is_signed=bool(getattr(sig, "is_signed", False)),
        minimum=_maybe_float(getattr(sig, "minimum", None)),
        maximum=_maybe_float(getattr(sig, "maximum", None)),
        scale=float(getattr(sig, "scale", 1.0) or 1.0),
        offset=float(getattr(sig, "offset", 0.0) or 0.0),
        unit=str(getattr(sig, "unit", "") or ""),
        choices=(
            {int(k): str(v) for k, v in raw.items()}
            if (raw := getattr(sig, "choices", None))
            else None
        ),
        initial=initial,
    )


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def encode_tx_message(
    decoder_db: cantools.database.Database,
    message_obj: cantools.database.Message,
    overrides: dict[str, float | int | bool | str],
) -> bytes:
    """Encode a CAN message payload from a sparse overrides dict.

    Unspecified signals fall back to ``signal.initial`` then ``0.0`` —
    same rule MCP's ``send_signal`` uses. Returns the raw bytes; the
    caller wraps them in a :class:`can.Message`.
    """
    signal_values: dict[str, Any] = {}
    for sig in message_obj.signals:
        if sig.name in overrides:
            signal_values[sig.name] = overrides[sig.name]
            continue
        initial = getattr(sig, "initial", None)
        signal_values[sig.name] = float(initial) if initial is not None else 0.0
    return bytes(
        decoder_db.encode_message(
            message_obj.frame_id,
            signal_values,
            scaling=True,
            padding=True,
            strict=False,
        )
    )


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------


class TxScheduler(QObject):
    """Owns the live cyclic tasks for a :class:`TxSetup`.

    Lifecycle:
      * :meth:`apply` is the single reconciliation point — diff the new
        setup against currently-running tasks and start / stop / modify
        accordingly. Cheap to call after every UI change.
      * :meth:`set_master` flips the global gate. ``False`` stops every
        task but leaves the setup intact; ``True`` re-applies.
      * :meth:`send_one_shot` dispatches a single send via
        :meth:`BusReader.send_message`. Refused with an ``error`` signal
        when master is OFF (handoff §15 open Q3 — *refuse + toast*).
      * :meth:`shutdown` stops every task; the TX window calls this on
        close.

    Listen-only buses are honoured up front: enabling master while any
    active transmit targets a listen-only bus emits ``error`` and the
    master stays OFF.
    """

    error = Signal(int, str)  # (message_id, message); 0 for non-row errors

    def __init__(
        self,
        bus_reader: BusReader,
        database: Database,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bus_reader = bus_reader
        self._database = database
        self._setup = TxSetup()
        self._tasks: dict[int, object] = {}
        self._bus_reader.bus_stopped.connect(self._on_bus_stopped)

    @property
    def setup(self) -> TxSetup:
        return self._setup

    def apply(self, setup: TxSetup) -> None:
        """Adopt ``setup`` as the new state and reconcile live tasks."""
        self._setup = setup
        self._reconcile()

    def set_master(self, enabled: bool) -> None:
        """Toggle the global TX gate.

        Turning ON checks every active row's bus for listen-only; any
        match refuses the toggle and emits :attr:`error` so the UI can
        revert the switch and toast.
        """
        if enabled and not self._check_listen_only_clear():
            return
        self._setup.master_enabled = enabled
        self._reconcile()

    def toggle_active(self, idx: int) -> None:
        if not (0 <= idx < len(self._setup.transmits)):
            return
        self._setup.transmits[idx].active = not self._setup.transmits[idx].active
        self._reconcile()

    def send_one_shot(self, idx: int) -> None:
        """Encode + send a single frame for transmit ``idx``.

        Refuses with an ``error`` emission when master is OFF — the
        handoff treats one-shot as a *send now* button that the master
        gate also gates, so user expectations stay consistent.
        """
        if not (0 <= idx < len(self._setup.transmits)):
            return
        if not self._setup.master_enabled:
            self.error.emit(0, "TX Master is OFF — enable master to send")
            return
        tx = self._setup.transmits[idx]
        msg = self._encode(tx)
        if msg is None:
            return
        try:
            self._bus_reader.send_message(msg, tx.bus_name)
        except BusReadOnlyError as exc:
            self.error.emit(tx.message_id, str(exc))
        except Exception as exc:  # pragma: no cover — defensive
            self.error.emit(tx.message_id, f"send failed: {exc}")

    def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            self._bus_reader.stop_periodic(task)
        self._tasks.clear()

    # ---- Internal ----

    def _reconcile(self) -> None:
        """Bring live cyclic tasks into agreement with the setup."""
        target_ids: set[int] = set()
        if self._setup.master_enabled:
            for tx in self._setup.transmits:
                if not tx.active or tx.mode != "periodic":
                    continue
                target_ids.add(tx.message_id)
        # Stop tasks no longer wanted.
        for mid in list(self._tasks):
            if mid not in target_ids:
                self._bus_reader.stop_periodic(self._tasks.pop(mid))
        # Start or update tasks we want.
        for tx in self._setup.transmits:
            if tx.message_id not in target_ids:
                continue
            msg = self._encode(tx)
            if msg is None:
                continue
            existing = self._tasks.get(tx.message_id)
            if existing is None:
                try:
                    task = self._bus_reader.start_periodic(msg, tx.bus_name, tx.period_ms / 1000.0)
                except BusReadOnlyError as exc:
                    self.error.emit(tx.message_id, str(exc))
                    continue
                except Exception as exc:
                    self.error.emit(tx.message_id, f"start_periodic failed: {exc}")
                    continue
                self._tasks[tx.message_id] = task
            else:
                modify = getattr(existing, "modify_data", None)
                if callable(modify):
                    try:
                        modify(msg)
                    except Exception as exc:
                        self.error.emit(tx.message_id, f"modify_data failed: {exc}")

    def _encode(self, tx: TxMessage) -> can.Message | None:
        decoder = self._database.decoder_for_bus(tx.bus_name)
        if decoder is None:
            self.error.emit(tx.message_id, f"no DBC bound to bus {tx.bus_name!r}")
            return None
        try:
            msg_obj = decoder.database.get_message_by_frame_id(tx.message_id)
        except KeyError:
            self.error.emit(tx.message_id, f"message 0x{tx.message_id:X} not in DBC")
            return None
        try:
            data = encode_tx_message(decoder.database, msg_obj, tx.signal_values)
        except Exception as exc:
            self.error.emit(tx.message_id, f"encode failed: {exc}")
            return None
        return can.Message(
            arbitration_id=int(msg_obj.frame_id),
            data=data,
            is_extended_id=bool(getattr(msg_obj, "is_extended_frame", False)),
        )

    def _check_listen_only_clear(self) -> bool:
        """Verify no active transmit targets a listen-only bus."""
        for tx in self._setup.transmits:
            if not tx.active:
                continue
            if self._bus_reader.is_listen_only(tx.bus_name):
                self.error.emit(
                    tx.message_id,
                    f"bus {tx.bus_name!r} is listen-only — TX disabled",
                )
                return False
        return True

    def _on_bus_stopped(self, bus_name: str) -> None:
        """Tear down tasks bound to a bus that just closed."""
        for tx in list(self._setup.transmits):
            if tx.bus_name != bus_name:
                continue
            task = self._tasks.pop(tx.message_id, None)
            if task is not None:
                self._bus_reader.stop_periodic(task)


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _autofill_attribute(sig: cantools.database.Signal) -> str | None:
    """Best-effort lookup of a vendor-specific ``autofill`` attribute.

    Some DBC tool chains tag rolling counters and CRCs via a custom
    signal attribute. cantools exposes raw attributes via ``sig.dbc``
    or ``sig.attributes`` depending on version — try both, return
    ``None`` when neither yields a string.
    """
    for attr_dict_name in ("attributes", "dbc"):
        attrs = getattr(sig, attr_dict_name, None)
        if not isinstance(attrs, dict):
            continue
        val = attrs.get("autofill")
        if isinstance(val, str):
            return val
    return None


__all__ = [
    "TxMessage",
    "TxMode",
    "TxScheduler",
    "TxSetup",
    "TxSignalKind",
    "TxSignalSpec",
    "build_signal_spec",
    "encode_tx_message",
    "infer_kind",
    "resolve_kind",
]
