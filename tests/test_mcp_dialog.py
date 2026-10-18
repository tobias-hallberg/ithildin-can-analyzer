# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``ui.mcp_panel.McpDialog`` — bind, allowlist refresh, audit tail."""

from __future__ import annotations

import json
from pathlib import Path

from ithildin.mcp.permissions import AllowRule
from ithildin.ui.mcp_panel import AddRuleDialog, McpDialog
from tests.conftest import make_app_context


def test_dialog_builds_without_binding(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dlg = McpDialog()
    qtbot.addWidget(dlg)
    assert "MCP server stopped" in dlg._status_text.text()
    assert dlg._rules_table.rowCount() == 0


def test_bind_populates_allowlist(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    ctx.allowlist.add_rule(AllowRule(id=0, message_id=0x301, signal="ReqCurrent"))
    ctx.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*", rate_hz=1.0))

    dlg = McpDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(ctx)

    assert dlg._rules_table.rowCount() == 2
    assert dlg._rules_table.item(0, 0).text() == "0x301"
    assert dlg._rules_table.item(1, 2).text() == "*"
    assert dlg._rules_table.item(1, 3).text() == "1"
    # rate_hz=None now reads as "unlimited" rather than an em dash.
    assert dlg._rules_table.item(0, 3).text() == "unlimited"


def test_audit_tail_picks_up_appended_event(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    # Pre-seed the audit file before binding so it has content to read.
    ctx.audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ctx.audit_path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-04-25T12:00:00+00:00",
                    "call": "send_signal",
                    "decision": "ALLOW",
                    "reason": "OK",
                    "rule_id": 1,
                }
            )
            + "\n"
        )

    dlg = McpDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(ctx)

    assert dlg._audit_list.count() == 1
    text = dlg._audit_list.item(0).text()
    assert "ALLOW" in text
    assert "send_signal" in text
    assert "OK" in text


def test_remove_rule_persists(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    rule = ctx.allowlist.add_rule(AllowRule(id=0, message_id=0x100, signal="*"))
    ctx.allowlist.save()

    dlg = McpDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(ctx)

    # Bypass the QMessageBox confirm by removing directly.
    ctx.allowlist.remove_rule(rule.id)
    ctx.allowlist.save()
    dlg._refresh_rules()
    assert dlg._rules_table.rowCount() == 0


def test_add_rule_dialog_builds_signal_rule(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """Modal form fields → AllowRule via build_rule(), unbound (raw ID path)."""
    dlg = AddRuleDialog()
    qtbot.addWidget(dlg)
    dlg._message_combo.setCurrentText("0x301")
    dlg._signal_combo.setCurrentText("EngineSpeed")
    dlg._rate_spin.setValue(2.5)
    dlg._agent_edit.setText("claude-sonnet")
    dlg._bus_combo.setCurrentText("CAN_1")
    dlg._enforce_ranges.setChecked(False)

    rule = dlg.build_rule()
    assert rule is not None
    assert rule.message_id == 0x301
    assert rule.signal == "EngineSpeed"
    assert rule.rate_hz == 2.5
    assert rule.agent_filter == "claude-sonnet"
    assert rule.bus_name == "CAN_1"
    assert rule.enforce_signal_ranges is False


def test_add_rule_dialog_rejects_empty_message(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    dlg = AddRuleDialog()
    qtbot.addWidget(dlg)
    dlg._message_combo.setCurrentText("")
    assert dlg.build_rule() is None


def test_rate_spin_is_live_on_open_and_steps(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """The stepper arrows must work immediately.

    The old form disabled the spin box until a "Limit rate" checkbox was
    ticked, so its arrows were inert on open and read as broken.
    """
    dlg = AddRuleDialog()
    qtbot.addWidget(dlg)
    assert dlg._rate_spin.isEnabled() is True
    # 0 renders as "unlimited" and persists as rate_hz=None.
    assert dlg._rate_spin.value() == 0.0
    assert dlg._rate_spin.specialValueText() == "unlimited"

    dlg._rate_spin.stepUp()
    assert dlg._rate_spin.value() == 1.0
    dlg._rate_spin.stepDown()
    assert dlg._rate_spin.value() == 0.0, "stepping down must reach unlimited, not 0.01"

    dlg._message_combo.setCurrentText("0x301")
    rule = dlg.build_rule()
    assert rule is not None
    assert rule.rate_hz is None


def test_add_rule_dialog_populates_from_dbc(  # type: ignore[no-untyped-def]
    qapp, qtbot, tmp_path: Path, example_dbc_path: Path
) -> None:
    """Bound to a project, the pickers list DBC messages and their signals."""
    ctx = make_app_context(tmp_path)
    ctx.database.add_dbc(example_dbc_path, ["CAN_1"])

    dlg = AddRuleDialog(ctx=ctx)
    qtbot.addWidget(dlg)

    labels = [dlg._message_combo.itemText(i) for i in range(dlg._message_combo.count())]
    assert any(label.startswith("EngineCommand") for label in labels)
    assert any("0x100" in label for label in labels)
    # Nothing preselected — the user must choose.
    assert dlg._message_combo.currentText() == ""

    idx = next(i for i, label in enumerate(labels) if label.startswith("EngineCommand"))
    dlg._message_combo.setCurrentIndex(idx)
    signals = [dlg._signal_combo.itemText(i) for i in range(dlg._signal_combo.count())]
    assert signals[0] == "*"
    assert set(signals[1:]) == {"EngineSpeed", "IdleSetpoint"}

    # Picking a real signal surfaces its DBC range and clears the warning.
    dlg._signal_combo.setCurrentIndex(signals.index("EngineSpeed"))
    assert "0 … 8000" in dlg._signal_hint.text()
    assert "rpm" in dlg._signal_hint.text()
    assert dlg._wildcard_warning.text() == ""

    rule = dlg.build_rule()
    assert rule is not None
    assert rule.message_id == 0x100
    assert rule.signal == "EngineSpeed"

    # The bus picker offers the project's buses plus an explicit "any".
    buses = [dlg._bus_combo.itemText(i) for i in range(dlg._bus_combo.count())]
    assert buses[0] == AddRuleDialog.ANY_BUS
    assert rule.bus_name is None


def test_wildcard_signal_warns(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """The broadest grant available should say so."""
    dlg = AddRuleDialog()
    qtbot.addWidget(dlg)
    dlg._signal_combo.setCurrentText("*")
    dlg._refresh_signal_hint()
    assert "wildcard" in dlg._wildcard_warning.text().lower()


def test_add_rule_then_refresh_shows_in_table(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Adding via the store + refresh is the same path the modal walks."""
    ctx = make_app_context(tmp_path)
    dlg = McpDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(ctx)
    assert dlg._rules_table.rowCount() == 0

    ctx.allowlist.add_rule(AllowRule(id=0, message_id=0x250, signal="Throttle"))
    dlg._refresh_rules()
    assert dlg._rules_table.rowCount() == 1
    assert dlg._rules_table.item(0, 0).text() == "0x250"
    # No DBC loaded, so the message name is unresolved.
    assert dlg._rules_table.item(0, 1).text() == "—"
    assert dlg._rules_table.item(0, 2).text() == "Throttle"


def test_bridge_error_with_address_message_offers_port_retry(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Port-in-use error: banner shows action; clicking it bumps port and retries."""
    ctx = make_app_context(tmp_path)
    dlg = McpDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(ctx)

    # Simulate the bridge reporting EADDRINUSE. Use isHidden() rather
    # than isVisible() since the dialog isn't shown in the test.
    dlg._on_bridge_error("OSError: address already in use")
    assert dlg._banner.isHidden() is False

    initial_port = ctx.mcp_port if ctx.mcp_port else 7320
    # Pretend the bridge is not running (it isn't), then click the
    # banner's "Choose another port" action.
    dlg._on_banner_action()
    assert dlg._port_override == initial_port + 1
    # Banner is dismissed once the user accepted the retry offer.
    assert dlg._banner.isHidden() is True


def test_audit_tail_picks_up_lines_appended_after_bind(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx = make_app_context(tmp_path)
    # Empty audit file at bind time.
    ctx.audit_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.audit_path.touch()

    dlg = McpDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(ctx)
    assert dlg._audit_list.count() == 0

    # Append a line, then trigger the tail read manually (avoid waiting
    # on the 1 s QTimer).
    with open(ctx.audit_path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-04-25T12:00:00+00:00",
                    "call": "send_frame",
                    "decision": "DENY",
                    "reason": "NO_RULE",
                }
            )
            + "\n"
        )
    dlg._tail_audit()
    assert dlg._audit_list.count() == 1
    assert "DENY" in dlg._audit_list.item(0).text()


def test_audit_tail_resets_on_file_shrink(qapp, qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Rotation truncates the file; tail must re-read from the start."""
    ctx = make_app_context(tmp_path)
    ctx.audit_path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        json.dumps(
            {
                "ts": "2026-04-25T12:00:00+00:00",
                "call": "list_buses",
                "decision": "ALLOW",
                "reason": "OK",
            }
        )
        + "\n"
    )
    ctx.audit_path.write_text(line * 3, encoding="utf-8")

    dlg = McpDialog()
    qtbot.addWidget(dlg)
    dlg.bind_app(ctx)
    assert dlg._audit_list.count() == 3

    # Simulate rotation: shorter file with one fresh line.
    ctx.audit_path.write_text(line, encoding="utf-8")
    dlg._tail_audit()
    assert dlg._audit_list.count() == 1
