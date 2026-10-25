# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``mcp.permissions.AllowlistStore``."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from ithildin.mcp.permissions import (
    AllowlistStore,
    AllowRule,
    SendFrameRequest,
    SendSignalRequest,
    SignalRange,
)


def _store(tmp_path: Path) -> AllowlistStore:
    return AllowlistStore(tmp_path / "allowlist.toml")


def test_no_rule_denies_signal_send(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0)
    )
    assert decision.allowed is False
    assert decision.reason == "NO_RULE"


def test_specific_signal_rule_allows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rule = store.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    decision = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0)
    )
    assert decision.allowed is True
    assert decision.rule_id == rule.id


def test_wildcard_signal_rule_allows_signal_and_frame(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    sig = store.check_signal(SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0))
    assert sig.allowed is True
    frame = store.check_frame(SendFrameRequest(message_id=0x100))
    assert frame.allowed is True


def test_specific_signal_rule_does_not_authorise_raw_frame(tmp_path: Path) -> None:
    """A rule allowing one signal must not gate raw send_frame on the same ID."""
    store = _store(tmp_path)
    store.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed"))
    frame = store.check_frame(SendFrameRequest(message_id=0x100))
    assert frame.allowed is False
    assert frame.reason == "NO_RULE"


def test_signal_specific_wins_over_wildcard(tmp_path: Path) -> None:
    """When both match, the specific rule decides — used for tighter rate limits."""
    store = _store(tmp_path)
    store.add_rule(AllowRule(id=0, message_id=0x100, signal="*", rate_hz=10.0))
    specific = store.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed", rate_hz=1.0))
    decision = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0)
    )
    assert decision.allowed is True
    assert decision.rule_id == specific.id


def test_out_of_range_blocks_when_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rule = store.add_rule(
        AllowRule(
            id=0,
            message_id=0x100,
            signal="EngineSpeed",
            enforce_signal_ranges=True,
        )
    )

    def lookup(_mid: int, _sig: str) -> SignalRange | None:
        return SignalRange(minimum=0.0, maximum=8000.0)

    too_high = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=9000.0),
        range_lookup=lookup,
    )
    assert too_high.allowed is False
    assert too_high.reason == "OUT_OF_RANGE"
    assert too_high.rule_id == rule.id

    in_range = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=2000.0),
        range_lookup=lookup,
    )
    assert in_range.allowed is True


def test_rate_limit_blocks_second_send_within_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rule = store.add_rule(
        AllowRule(
            id=0,
            message_id=0x100,
            signal="EngineSpeed",
            rate_hz=2.0,  # one per 500 ms
        )
    )
    req = SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0)
    first = store.check_signal(req)
    assert first.allowed is True
    second = store.check_signal(req)
    assert second.allowed is False
    assert second.reason == "RATE_LIMITED"
    assert second.rule_id == rule.id

    # After enough wall time, the bucket refills.
    time.sleep(0.6)
    third = store.check_signal(req)
    assert third.allowed is True


def test_agent_filter_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add_rule(
        AllowRule(
            id=0,
            message_id=0x100,
            signal="EngineSpeed",
            agent_filter="claude-desktop",
        )
    )
    matched = store.check_signal(
        SendSignalRequest(
            message_id=0x100,
            signal="EngineSpeed",
            value=800.0,
            agent="claude-desktop",
        )
    )
    assert matched.allowed is True
    other = store.check_signal(
        SendSignalRequest(
            message_id=0x100,
            signal="EngineSpeed",
            value=800.0,
            agent="some-other-agent",
        )
    )
    assert other.allowed is False
    assert other.reason == "NO_RULE"


def test_bus_name_filter_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add_rule(AllowRule(id=0, message_id=0x100, signal="EngineSpeed", bus_name="CAN_1"))
    on_bus = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0, bus_name="CAN_1")
    )
    assert on_bus.allowed is True
    off_bus = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0, bus_name="CAN_2")
    )
    assert off_bus.allowed is False


def test_bus_scoped_rule_requires_explicit_bus(tmp_path: Path) -> None:
    """With no bus named, the frame goes wherever send_message sends it, so a
    rule for one bus can't vouch for it."""
    store = _store(tmp_path)
    store.add_rule(AllowRule(id=0, message_id=0x100, signal="*", bus_name="CAN_1"))
    unnamed = store.check_frame(SendFrameRequest(message_id=0x100))
    assert unnamed.allowed is False
    assert unnamed.reason == "NO_RULE"
    unnamed_signal = store.check_signal(
        SendSignalRequest(message_id=0x100, signal="EngineSpeed", value=800.0)
    )
    assert unnamed_signal.reason == "NO_RULE"

    any_bus = store.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    decision = store.check_frame(SendFrameRequest(message_id=0x100))
    assert decision.allowed is True
    assert decision.rule_id == any_bus.id


def test_save_load_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rule = store.add_rule(
        AllowRule(
            id=0,
            message_id=0x301,
            signal="ReqCurrent",
            rate_hz=1.0,
            agent_filter="claude-code",
            bus_name="CAN_1",
            enforce_signal_ranges=True,
            created_at=datetime(2026, 4, 25, 12, 30, tzinfo=UTC),
        )
    )
    store.save()

    reloaded = AllowlistStore.load(tmp_path / "allowlist.toml")
    assert len(reloaded.rules) == 1
    r = reloaded.rules[0]
    assert r.id == rule.id
    assert r.message_id == 0x301
    assert r.signal == "ReqCurrent"
    assert r.rate_hz == 1.0
    assert r.agent_filter == "claude-code"
    assert r.bus_name == "CAN_1"
    assert r.created_at == rule.created_at


def test_load_missing_file_returns_empty_store(tmp_path: Path) -> None:
    store = AllowlistStore.load(tmp_path / "absent.toml")
    assert store.rules == []


def test_remove_rule(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rule = store.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    assert store.remove_rule(rule.id) is True
    assert store.rules == []
    assert store.remove_rule(rule.id) is False


def test_concurrent_check_and_mutate_does_not_raise(tmp_path: Path) -> None:
    """Checks run on the MCP worker thread while the GUI mutates rules.

    Without the store's lock, a ``remove_rule`` landing inside
    ``_matching``'s ``sorted()`` can raise mid-iteration.
    """
    import threading

    store = AllowlistStore(tmp_path / "allowlist.toml")
    for i in range(50):
        store.add_rule(AllowRule(id=0, message_id=0x100 + i, signal="*"))

    errors: list[BaseException] = []
    stop = threading.Event()

    def checker() -> None:
        try:
            while not stop.is_set():
                store.check_frame(SendFrameRequest(message_id=0x100))
                store.check_signal(SendSignalRequest(message_id=0x110, signal="X", value=1.0))
        except BaseException as exc:
            errors.append(exc)

    def mutator() -> None:
        try:
            for i in range(200):
                rule = store.add_rule(AllowRule(id=0, message_id=0x100 + (i % 50), signal="*"))
                store.remove_rule(rule.id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=checker), threading.Thread(target=mutator)]
    threads[0].start()
    threads[1].start()
    threads[1].join(timeout=10)
    stop.set()
    threads[0].join(timeout=10)

    assert errors == []
