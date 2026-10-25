# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Allowlist + send-path decision engine.

Every MCP send (signal, frame, periodic) goes through
``AllowlistStore.check_*`` once. There is no bypass: the bridge calls
the check, the audit log captures the result, and only allowed sends
reach the bus thread.

Rule shape (handoff §"Allowlist rule schema"):

    AllowRule(
        id,                   # monotonic per project
        message_id,           # CAN ID, e.g. 0x301
        signal,               # signal name OR literal "*"
        rate_hz=None,         # max send rate; None = unlimited
        agent_filter=None,    # match caller name; None = any agent
        bus_name=None,        # None = any bus; else only a request naming it
        enforce_signal_ranges=True,
        created_at,           # ISO-8601 datetime
    )

Resolution order (handoff plan):
  1. Bus has a rule for this CAN ID → else deny ``NO_RULE``.
  2. ``send_signal`` only: signal name in rule's signal (or "*") → else
     deny ``NO_RULE``.
  3. ``send_signal`` only: when ``enforce_signal_ranges``, value within
     DBC min/max → else deny ``OUT_OF_RANGE``.
  4. Per-rule token bucket has a token → else deny ``RATE_LIMITED``.
  5. Allow ``OK``.

Storage: ``~/.ithildin/<project>/allowlist.toml`` as a list of
``[[rule]]`` tables. ``None`` fields persist as empty strings (TOML has
no null).
"""

from __future__ import annotations

import threading
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import tomli_w

WILDCARD_SIGNAL = "*"

DecisionReason = Literal[
    "OK",
    "NO_RULE",
    "OUT_OF_RANGE",
    "RATE_LIMITED",
    "DRY_RUN",
    "BUS_READ_ONLY",
    # Emitted by the write tools rather than the decision engine, but they
    # travel the same ``reason`` field into the audit log and the tool
    # result, so the contract has to cover them.
    "BAD_VALUE",
    "BAD_ID",
    "BAD_DATA",
    "BAD_LENGTH",
    "UNKNOWN_MESSAGE",
    "ENCODE_FAILED",
    "BUS_ERROR",
    "NO_BUS",
    "BUS_UNHEALTHY",
    "INTERNAL_ERROR",
]


@dataclass(frozen=True)
class AllowRule:
    """One allowlist entry."""

    id: int
    message_id: int
    signal: str = WILDCARD_SIGNAL
    rate_hz: float | None = None
    agent_filter: str | None = None
    bus_name: str | None = None
    enforce_signal_ranges: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Decision:
    """Outcome of a single allowlist check."""

    allowed: bool
    reason: DecisionReason
    rule_id: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class SendSignalRequest:
    message_id: int
    signal: str
    value: float
    agent: str = ""
    bus_name: str | None = None


@dataclass(frozen=True)
class SendFrameRequest:
    message_id: int
    agent: str = ""
    bus_name: str | None = None


@dataclass(frozen=True)
class SignalRange:
    """DBC-declared min/max bounds for one signal."""

    minimum: float
    maximum: float


# Callable shape: (message_id, signal_name) -> SignalRange | None.
type RangeLookup = Callable[[int, str], "SignalRange | None"]


class _TokenBucket:
    """Capacity-1 token bucket: one token per ``1 / rate_hz`` seconds.

    Capacity 1 means no burst — each ``consume()`` requires the prior
    one to have been ``1 / rate_hz`` ago. That matches the handoff's
    "max send rate" framing better than a leaky bucket would.
    """

    __slots__ = ("_last", "_rate", "_tokens")

    def __init__(self, rate_hz: float) -> None:
        self._rate = rate_hz
        self._tokens = 1.0
        self._last = time.monotonic()

    def consume(self, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        elapsed = max(0.0, t - self._last)
        self._last = t
        self._tokens = min(1.0, self._tokens + elapsed * self._rate)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class AllowlistStore:
    """Owns the rule list + per-rule rate-limit state.

    Pure data: no Qt, no asyncio. The bridge calls ``check_signal`` /
    ``check_frame``; the dialog calls ``add_rule`` / ``remove_rule`` /
    ``rules``.

    Those two callers live on different threads — checks arrive from the
    MCP worker thread while the dialog mutates from the GUI thread — so a
    lock guards every touch of ``_rules`` / ``_buckets`` / ``_next_id``.
    Without it a ``remove_rule`` landing mid-``sorted()`` can raise.
    """

    def __init__(
        self,
        path: Path,
        rules: Iterable[AllowRule] | None = None,
    ) -> None:
        self._path = path
        self._rules: list[AllowRule] = list(rules or [])
        self._buckets: dict[int, _TokenBucket] = {}
        self._next_id = 1 + max((r.id for r in self._rules), default=0)
        self._lock = threading.Lock()

    # ---- Loading / saving ----

    @classmethod
    def load(cls, path: Path) -> AllowlistStore:
        if not path.exists():
            return cls(path, [])
        with open(path, "rb") as f:
            data = tomllib.load(f)
        rules: list[AllowRule] = []
        for raw in data.get("rule", []):
            rules.append(_rule_from_toml(raw))
        return cls(path, rules)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {"rule": [_rule_to_toml(r) for r in self._rules]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            tomli_w.dump(payload, f)
        tmp.replace(self._path)

    # ---- Mutation ----

    def add_rule(self, rule: AllowRule) -> AllowRule:
        with self._lock:
            # Caller may submit id=0 (a sentinel "auto"); stamp our own id.
            rule_with_id = replace(rule, id=self._next_id)
            self._next_id += 1
            self._rules.append(rule_with_id)
        return rule_with_id

    def remove_rule(self, rule_id: int) -> bool:
        with self._lock:
            for i, rule in enumerate(self._rules):
                if rule.id == rule_id:
                    del self._rules[i]
                    self._buckets.pop(rule_id, None)
                    return True
        return False

    @property
    def rules(self) -> list[AllowRule]:
        with self._lock:
            return list(self._rules)

    # ---- Decision engine ----

    def check_signal(
        self,
        request: SendSignalRequest,
        range_lookup: RangeLookup | None = None,
    ) -> Decision:
        for rule in self._matching(request.message_id, request.bus_name, request.agent):
            if rule.signal != WILDCARD_SIGNAL and rule.signal != request.signal:
                continue
            if rule.enforce_signal_ranges and range_lookup is not None:
                bounds = range_lookup(request.message_id, request.signal)
                if bounds is not None and not (bounds.minimum <= request.value <= bounds.maximum):
                    return Decision(
                        allowed=False,
                        reason="OUT_OF_RANGE",
                        rule_id=rule.id,
                        detail=(
                            f"value {request.value} not in [{bounds.minimum}, {bounds.maximum}]"
                        ),
                    )
            if rule.rate_hz is not None and not self._bucket(rule).consume():
                return Decision(
                    allowed=False,
                    reason="RATE_LIMITED",
                    rule_id=rule.id,
                )
            return Decision(allowed=True, reason="OK", rule_id=rule.id)
        return Decision(allowed=False, reason="NO_RULE")

    def check_frame(self, request: SendFrameRequest) -> Decision:
        for rule in self._matching(request.message_id, request.bus_name, request.agent):
            # Raw frames need a wildcard rule for that CAN ID — there's no
            # signal name in a raw frame to match against. A specific-signal
            # rule is intentionally not enough for raw send.
            if rule.signal != WILDCARD_SIGNAL:
                continue
            if rule.rate_hz is not None and not self._bucket(rule).consume():
                return Decision(allowed=False, reason="RATE_LIMITED", rule_id=rule.id)
            return Decision(allowed=True, reason="OK", rule_id=rule.id)
        return Decision(allowed=False, reason="NO_RULE")

    # ---- Internal ----

    def _matching(
        self,
        message_id: int,
        bus_name: str | None,
        agent: str,
    ) -> list[AllowRule]:
        # Snapshot under the lock, then filter outside it — a generator
        # yielding mid-iteration would hand the lock to the caller.
        # Specific-signal rules first; wildcard last. This way a per-signal
        # rule wins over a "*" if both match for ``check_signal``.
        with self._lock:
            ordered = sorted(
                self._rules,
                key=lambda r: 0 if r.signal != WILDCARD_SIGNAL else 1,
            )
        return [
            rule
            for rule in ordered
            if rule.message_id == message_id
            # A bus-scoped rule needs the request to name its bus: an unnamed
            # target would let send_message pick whichever bus is open then.
            and (rule.bus_name is None or rule.bus_name == bus_name)
            and not (rule.agent_filter is not None and rule.agent_filter != agent)
        ]

    def _bucket(self, rule: AllowRule) -> _TokenBucket:
        with self._lock:
            bucket = self._buckets.get(rule.id)
            if bucket is None:
                assert rule.rate_hz is not None
                bucket = _TokenBucket(rule.rate_hz)
                self._buckets[rule.id] = bucket
        return bucket


# --------------------------------------------------------------------------
# TOML <-> AllowRule helpers
# --------------------------------------------------------------------------


def _rule_to_toml(rule: AllowRule) -> dict[str, Any]:
    """Serialize. ``None``s become empty strings (TOML has no null)."""
    return {
        "id": int(rule.id),
        "message_id": int(rule.message_id),
        "signal": rule.signal,
        "rate_hz": float(rule.rate_hz) if rule.rate_hz is not None else 0.0,
        "agent_filter": rule.agent_filter or "",
        "bus_name": rule.bus_name or "",
        "enforce_signal_ranges": bool(rule.enforce_signal_ranges),
        "created_at": rule.created_at.isoformat(),
    }


def _rule_from_toml(raw: dict[str, Any]) -> AllowRule:
    """Inverse of ``_rule_to_toml``."""
    return AllowRule(
        id=int(raw.get("id", 0)),
        message_id=int(raw.get("message_id", 0)),
        signal=str(raw.get("signal", WILDCARD_SIGNAL)) or WILDCARD_SIGNAL,
        rate_hz=_coerce_optional_float(raw.get("rate_hz")),
        agent_filter=_coerce_optional_str(raw.get("agent_filter")),
        bus_name=_coerce_optional_str(raw.get("bus_name")),
        enforce_signal_ranges=bool(raw.get("enforce_signal_ranges", True)),
        created_at=_coerce_datetime(raw.get("created_at")),
    )


def _coerce_optional_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    f = float(v)
    return None if f <= 0 else f


def _coerce_optional_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)


def _coerce_datetime(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)
