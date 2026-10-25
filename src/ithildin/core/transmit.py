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
   master / per-row semantics. Anything that stops TX behind the user's
   back (bus closed or lost, a task failing) turns the master OFF; nothing
   ever restarts on its own.

The shared :func:`encode_tx_message` helper is also called by the MCP
``send_signal`` tool so the two transmit paths can never drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import can
import cantools.database
from can.broadcastmanager import ThreadBasedCyclicSendTask
from PySide6.QtCore import QObject, Signal

from ithildin.app_state.bus_reader import BusReader
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
    is_float: bool
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


def infer_kind(sig: cantools.database.Signal, message: cantools.database.Message) -> TxSignalKind:
    """Pick a control kind for ``sig`` per handoff §7.2.

    Per-signal overrides on the :class:`TxMessage` win over this — call
    :func:`resolve_kind` when you have a :class:`TxMessage` in hand.
    """
    if sig.is_multiplexer and multiplexer_choices(message, sig):
        return "enum-dd"
    if int(getattr(sig, "length", 0) or 0) == 1:
        return "bool"
    choices = getattr(sig, "choices", None)
    if choices:
        return "enum-seg" if len(choices) <= 4 else "enum-dd"
    if _is_wide_integer(sig):
        return "hex"
    mn, mx = signal_range(sig)
    if bool(getattr(sig, "is_signed", False)) and mn is not None and mx is not None and mn < 0 < mx:
        return "signed"
    autofill = _autofill_attribute(sig)
    if autofill in ("counter", "crc"):
        return "auto"
    if mn is not None and mx is not None and (mx - mn) > 256:
        return "num-lg"
    return "num-sm"


def signal_range(sig: cantools.database.Signal) -> tuple[float | None, float | None]:
    """Physical ``(min, max)`` the signal can actually carry.

    The DBC's declared range, narrowed to what the raw bits can encode.
    DBCs often leave the range as ``[0|0]``, which cantools reads as no
    range, or declare one the bits can't hold, such as ``[0|255]`` on an
    8-bit signal scaled by 0.1. Float signals keep the declared range.
    """
    mn = _maybe_float(getattr(sig, "minimum", None))
    mx = _maybe_float(getattr(sig, "maximum", None))
    length = int(getattr(sig, "length", 0) or 0)
    if bool(getattr(sig, "is_float", False)) or length <= 0:
        return mn, mx
    if bool(getattr(sig, "is_signed", False)):
        raw_lo, raw_hi = -(1 << (length - 1)), (1 << (length - 1)) - 1
    else:
        raw_lo, raw_hi = 0, (1 << length) - 1
    scale = float(getattr(sig, "scale", 1.0) or 1.0)
    offset = float(getattr(sig, "offset", 0.0) or 0.0)
    ends: tuple[float, float]
    if scale.is_integer() and offset.is_integer():
        # Exact ints: as a float, a 64-bit bound rounds past what fits.
        ends = (raw_lo * int(scale) + int(offset), raw_hi * int(scale) + int(offset))
    else:
        ends = (raw_lo * scale + offset, raw_hi * scale + offset)
    lo, hi = min(ends), max(ends)
    narrowed_lo = lo if mn is None else max(lo, mn)
    narrowed_hi = hi if mx is None else min(hi, mx)
    if narrowed_lo > narrowed_hi:
        return lo, hi
    return narrowed_lo, narrowed_hi


def multiplexer_choices(
    message: cantools.database.Message, selector: cantools.database.Signal
) -> dict[int, str]:
    """IDs ``selector`` can take in ``message``, labelled for the editor.

    The DBC's value table names an ID when it has one; otherwise the label
    lists the signals that ID carries.
    """
    carried: dict[int, list[str]] = {}
    for sig in message.signals:
        if sig.multiplexer_signal == selector.name:
            for mux_id in sig.multiplexer_ids or ():
                carried.setdefault(int(mux_id), []).append(str(sig.name))
    names = getattr(selector, "choices", None) or {}
    labels: dict[int, str] = {}
    for mux_id, signals in sorted(carried.items()):
        if mux_id in names:
            labels[mux_id] = str(names[mux_id])
        else:
            more = f" +{len(signals) - 2}" if len(signals) > 2 else ""
            labels[mux_id] = ", ".join(signals[:2]) + more
    return labels


def implied_selectors(
    message: cantools.database.Message, values: Mapping[str, object]
) -> dict[str, int]:
    """Values for the multiplexer selectors that ``values`` leaves unset.

    Setting a multiplexed signal without its selector means "send that
    signal", so the selector takes the ID that carries it. A selector with
    nothing to follow takes its initial value when that is a valid ID, else
    its lowest valid ID: 0 often isn't one, and cantools refuses to encode
    an unknown selector ID.
    """
    by_name = {sig.name: sig for sig in message.signals}
    implied: dict[str, int] = {}
    for name in values:
        sig = by_name.get(name)
        while sig is not None and sig.multiplexer_signal is not None and sig.multiplexer_ids:
            selector = sig.multiplexer_signal
            if selector in values or selector in implied:
                break
            implied[selector] = int(sig.multiplexer_ids[0])
            sig = by_name.get(selector)
    for sig in message.signals:
        if not sig.is_multiplexer or sig.name in values or sig.name in implied:
            continue
        ids = list(multiplexer_choices(message, sig))
        if not ids:
            continue
        initial = _maybe_float(getattr(sig, "initial", None))
        implied[sig.name] = int(initial) if initial is not None and initial in ids else ids[0]
    return implied


def inactive_signals(message: cantools.database.Message, values: Mapping[str, object]) -> set[str]:
    """Signals a frame built from ``values`` won't carry: other selector branches."""
    selection = {**implied_selectors(message, values), **values}
    by_name = {sig.name: sig for sig in message.signals}
    inactive: set[str] = set()
    for sig in message.signals:
        cur = sig
        while cur.multiplexer_signal is not None:
            if selection.get(cur.multiplexer_signal) not in (cur.multiplexer_ids or ()):
                inactive.add(sig.name)
                break
            cur = by_name[cur.multiplexer_signal]
    return inactive


def resolve_kind(
    tx: TxMessage, sig: cantools.database.Signal, message: cantools.database.Message
) -> TxSignalKind:
    """Per-message override → :func:`infer_kind` fallback."""
    override = tx.kind_overrides.get(sig.name)
    if override is not None:
        return override
    return infer_kind(sig, message)


def build_signal_spec(
    sig: cantools.database.Signal,
    message: cantools.database.Message,
    *,
    kind: TxSignalKind,
    values: Mapping[str, float | int | bool | str],
) -> TxSignalSpec:
    """Snapshot one signal's metadata, and its value in ``values``, for the editor.

    A selector missing from ``values`` shows the ID the encoder will send.
    """
    minimum, maximum = signal_range(sig)
    mux_choices = multiplexer_choices(message, sig) if sig.is_multiplexer else None
    raw_choices = mux_choices or getattr(sig, "choices", None)
    current_value = values.get(sig.name)
    if current_value is None and mux_choices:
        current_value = implied_selectors(message, values).get(sig.name)
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
        is_float=bool(getattr(sig, "is_float", False)),
        minimum=minimum,
        maximum=maximum,
        scale=float(getattr(sig, "scale", 1.0) or 1.0),
        offset=float(getattr(sig, "offset", 0.0) or 0.0),
        unit=str(getattr(sig, "unit", "") or ""),
        choices={int(k): str(v) for k, v in raw_choices.items()} if raw_choices else None,
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

    Unspecified multiplexer selectors follow :func:`implied_selectors`;
    other unspecified signals fall back to ``signal.initial`` then ``0.0``.
    Returns the raw bytes; the caller wraps them in a :class:`can.Message`.
    """
    implied = implied_selectors(message_obj, overrides)
    signal_values: dict[str, Any] = {}
    for sig in message_obj.signals:
        if sig.name in overrides:
            signal_values[sig.name] = overrides[sig.name]
            continue
        if sig.name in implied:
            signal_values[sig.name] = implied[sig.name]
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
        accordingly. Cheap to call after every UI change. The scheduler
        keeps the setup object it is given, and mutates its
        ``master_enabled`` when it disarms.
      * :meth:`set_master` flips the global gate. ``False`` stops every
        task but leaves the setup intact; ``True`` re-applies.
      * :meth:`send_one_shot` dispatches a single send via
        :meth:`BusReader.send_message_async`. Refused with an ``error``
        signal when master is OFF (handoff §15 open Q3 — *refuse + toast*).
      * :meth:`shutdown` turns master OFF, which stops every task; the
        TX window calls this on close.

    Invariant: while master is ON, every active periodic row has a live
    task. When that can't hold (a task fails or won't start, or a bus an
    active row targets closes or fails) the scheduler disarms: master
    OFF, every task stopped, queued frames flushed, ``disarmed`` emitted.
    Nothing starts while master is OFF, so a reopened bus or a later edit
    never resumes TX by itself.

    Arming is refused up front, with ``error`` and master left OFF, while
    an active transmit targets a listen-only or closed bus.
    """

    error = Signal(int, str)  # (message_id, message); 0 for non-row errors
    disarmed = Signal(str)  # reason

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
        self._tasks: dict[tuple[str, int], ThreadBasedCyclicSendTask] = {}
        bus_reader.bus_failed.connect(self._on_bus_failed)
        bus_reader.bus_stopped.connect(self._on_bus_stopped)
        bus_reader.cyclic_failed.connect(self._on_cyclic_failed)
        bus_reader.send_failed.connect(self._on_send_failed)

    @property
    def setup(self) -> TxSetup:
        return self._setup

    def apply(self, setup: TxSetup) -> None:
        """Adopt ``setup`` as the new state and reconcile live tasks."""
        self._setup = setup
        self._reconcile()

    def set_master(self, enabled: bool) -> None:
        """Toggle the global TX gate.

        Turning ON checks every active row's bus is open and not
        listen-only; a failing check refuses the toggle and emits
        :attr:`error` so the UI can revert the switch and toast. Turning
        OFF also discards whatever the adapters still have queued.
        """
        if enabled and not (self._check_listen_only_clear() and self._check_buses_open()):
            return
        self._setup.master_enabled = enabled
        self._reconcile()
        if not enabled:
            self._flush_targets()

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
            self._bus_reader.send_message_async(msg, tx.bus_name)
        except RuntimeError as exc:
            self.error.emit(tx.message_id, str(exc))

    def shutdown(self) -> None:
        # Tasks only run while master is ON, and turning it OFF flushed.
        if self._setup.master_enabled:
            self.set_master(False)

    def is_transmitting(self, tx: TxMessage) -> bool:
        """Whether ``tx`` is live right now; what the UI shows as transmitting.

        A periodic row counts only while its task runs, so a task that died
        on a send error stops showing at once, before the disarm arrives.
        """
        if tx.mode == "periodic":
            task = self._tasks.get((tx.bus_name, tx.message_id))
            return task is not None and not task.stopped
        return self._setup.master_enabled and tx.active

    # ---- Internal ----

    def _reconcile(self) -> None:
        """Bring live cyclic tasks into agreement with the setup."""
        failure = self._sync_tasks()
        if failure is not None:
            self._disarm(failure)

    def _sync_tasks(self) -> str | None:
        """Start, stop and modify tasks to match the setup; the failure reason, if any."""
        wanted: dict[tuple[str, int], TxMessage] = {}
        if self._setup.master_enabled:
            for tx in self._setup.transmits:
                if tx.active and tx.mode == "periodic":
                    wanted[(tx.bus_name, tx.message_id)] = tx
        for key, task in list(self._tasks.items()):
            wanted_tx = wanted.get(key)
            # python-can can't change a running task's period: restart it.
            if wanted_tx is None or task.period_ms != wanted_tx.period_ms:
                del self._tasks[key]
                self._bus_reader.stop_periodic(key[0], task)
        for key, tx in wanted.items():
            msg = self._encode(tx)
            if msg is None:
                return f"Periodic 0x{tx.message_id:X} on {tx.bus_name} can't be encoded"
            running = self._tasks.get(key)
            if running is not None:
                running.modify_data(msg)
                continue
            try:
                self._tasks[key] = self._bus_reader.start_periodic(
                    msg, tx.bus_name, tx.period_ms / 1000.0
                )
            except RuntimeError as exc:
                return f"Periodic 0x{tx.message_id:X} on {tx.bus_name} didn't start ({exc})"
        return None

    def _disarm(self, reason: str) -> None:
        """The single disarm path: master OFF, stop everything, flush, tell the UI."""
        if not self._setup.master_enabled:
            return
        self._setup.master_enabled = False
        self._reconcile()
        self._flush_targets()
        self.disarmed.emit(reason)

    def _flush_targets(self) -> None:
        for bus_name in {tx.bus_name for tx in self._setup.transmits}:
            self._bus_reader.flush_tx(bus_name)

    def _targets_actively(self, bus_name: str) -> bool:
        return any(tx.active and tx.bus_name == bus_name for tx in self._setup.transmits)

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

    def _check_buses_open(self) -> bool:
        """Verify every active transmit's bus is connected."""
        open_buses = set(self._bus_reader.bus_names())
        for tx in self._setup.transmits:
            if tx.active and tx.bus_name not in open_buses:
                self.error.emit(
                    tx.message_id,
                    f"bus {tx.bus_name!r} is not connected — connect it first",
                )
                return False
        return True

    def _on_bus_failed(self, bus_name: str, error: str) -> None:
        # Fires just before bus_stopped, so this more specific reason wins.
        if self._targets_actively(bus_name):
            self._disarm(f"Bus {bus_name} lost ({error})")

    def _on_bus_stopped(self, bus_name: str) -> None:
        for key in [key for key in self._tasks if key[0] == bus_name]:
            self._bus_reader.stop_periodic(bus_name, self._tasks.pop(key))
        if self._targets_actively(bus_name):
            self._disarm(f"Bus {bus_name} closed")

    def _on_cyclic_failed(self, bus_name: str, message_id: int, error: str) -> None:
        if (bus_name, message_id) in self._tasks:
            self._disarm(f"Periodic 0x{message_id:X} on {bus_name} failed ({error})")

    def _on_send_failed(self, bus_name: str, message_id: int, error: str) -> None:
        if any(
            tx.bus_name == bus_name and tx.message_id == message_id for tx in self._setup.transmits
        ):
            self.error.emit(message_id, f"send failed: {error}")


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


def _is_wide_integer(sig: cantools.database.Signal) -> bool:
    """An integer signal wider than a float's 53-bit mantissa, scaled by whole numbers.

    A float would round its low bits away, so the editor keeps the value
    as a Python int. cantools converts an int exactly when the factor and
    offset are whole numbers. With a fractional factor it divides in
    floats, so such a signal stays a numeric control and float-limited.
    """
    scale = float(getattr(sig, "scale", 1.0) or 1.0)
    offset = float(getattr(sig, "offset", 0.0) or 0.0)
    return (
        int(getattr(sig, "length", 0) or 0) > 53
        and not bool(getattr(sig, "is_float", False))
        and scale.is_integer()
        and offset.is_integer()
    )


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
    "implied_selectors",
    "inactive_signals",
    "infer_kind",
    "multiplexer_choices",
    "resolve_kind",
    "signal_range",
]
