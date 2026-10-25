# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""MCP allowlist + audit dialog.

Three sections, top to bottom:

  1. Server status strip — running/stopped + URL + Start/Stop button.
  2. Allowlist — table of ``AllowRule`` rows + "Add rule…" button.
  3. Audit feed — last N lines from ``audit.jsonl``, polled via QTimer.

Modal-ish: opens from the toolbar's "MCP" action; user can keep it open
while working with the MDI canvas.

Rules are authored against the loaded DBCs — the add-rule modal's
pickers read through ``mcp.tools.list_messages`` / ``list_signals``, the
same functions the agent calls, so a rule can't name a message the agent
can't see. Raw CAN IDs remain typeable for un-DBC'd traffic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ithildin.mcp import tools
from ithildin.mcp.permissions import WILDCARD_SIGNAL, AllowlistStore, AllowRule
from ithildin.mcp.tools import McpReadContext
from ithildin.ui.banner import Banner
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import themed_callback
from ithildin.ui.theme import ThemeManager

if TYPE_CHECKING:
    from ithildin.app_state.context import AppContext
    from ithildin.mcp.bridge import McpServerBridge


_AUDIT_TAIL_LIMIT = 5000
_AUDIT_POLL_MS = 1000
# The first read of a log (rotation lets it reach 50 MB) starts this far
# from its end: it runs on the GUI thread.
_AUDIT_TAIL_BYTES = 2 * 1024 * 1024


class McpDialog(QDialog):
    """Server status + allowlist editor + audit feed."""

    DEFAULT_SIZE = (640, 720)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MCP · Allowlist & Audit")
        w, h = self.DEFAULT_SIZE
        self.resize(w, h)

        self._ctx: AppContext | None = None
        self._allowlist: AllowlistStore | None = None
        self._bridge: McpServerBridge | None = None
        self._audit_path: Path | None = None
        self._port_override: int | None = None
        self._audit_tail_offset = 0
        self._status_kind: str = "idle"
        self._audit_timer = QTimer(self)
        self._audit_timer.setInterval(_AUDIT_POLL_MS)
        self._audit_timer.timeout.connect(self._tail_audit)

        self._build_body()
        themed_callback(self._refresh_status_dot)

    def bind_app(self, ctx: AppContext) -> None:
        """Wire the dialog to project-level allowlist/audit/bridge handles."""
        self._ctx = ctx
        self._allowlist = ctx.allowlist
        self._bridge = ctx.mcp_bridge
        self._audit_path = ctx.audit_path

        ctx.mcp_bridge.started.connect(self._on_bridge_started)
        ctx.mcp_bridge.stopped.connect(self._on_bridge_stopped)
        ctx.mcp_bridge.error.connect(self._on_bridge_error)

        self._token_field.setText(ctx.mcp_bridge.token)
        self._refresh_status_text()
        self._refresh_rules()
        self._tail_audit()
        self._audit_timer.start()

    # ---- Build ----

    def _build_body(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # ---- Server status strip ----
        status_row = QHBoxLayout()
        self._status_dot = QLabel("●", self)
        status_row.addWidget(self._status_dot)
        self._status_text = QLabel("MCP server stopped", self)
        status_row.addWidget(self._status_text, 1)
        self._toggle_btn = QPushButton("Start server", self)
        self._toggle_btn.clicked.connect(self._on_toggle_clicked)
        status_row.addWidget(self._toggle_btn)
        layout.addLayout(status_row)

        # ---- Access token ----
        # The HTTP transport is bearer-gated; a client must send this token
        # as ``Authorization: Bearer <token>``. Read-only + copy button so
        # the user can paste it into their agent's MCP client config.
        token_row = QHBoxLayout()
        token_row.addWidget(QLabel("Access token", self))
        self._token_field = QLineEdit(self)
        self._token_field.setReadOnly(True)
        self._token_field.setFont(tabular_font(point_size=9))
        self._token_field.setPlaceholderText("(available once the app has a project dir)")
        token_row.addWidget(self._token_field, 1)
        self._copy_token_btn = QPushButton("Copy", self)
        self._copy_token_btn.clicked.connect(self._on_copy_token)
        token_row.addWidget(self._copy_token_btn)
        layout.addLayout(token_row)

        # ---- Inline error banner (port-in-use, transport failures…) ----
        self._banner = Banner(kind="error", parent=self)
        self._banner.action_clicked.connect(self._on_banner_action)
        layout.addWidget(self._banner)

        # ---- Allowlist table ----
        layout.addWidget(_section_label("Allowlist"))
        self._rules_table = QTableWidget(0, 6, self)
        self._rules_table.setHorizontalHeaderLabels(
            ["CAN ID", "Message", "Signal", "Rate (Hz)", "Agent", "Bus"]
        )
        self._rules_table.verticalHeader().setVisible(False)
        self._rules_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._rules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._rules_table.setMinimumHeight(140)
        layout.addWidget(self._rules_table)

        rule_row = QHBoxLayout()
        add_btn = QPushButton("Add rule…", self)
        add_btn.clicked.connect(self._on_add_rule_clicked)
        rule_row.addWidget(add_btn)
        remove_btn = QPushButton("Remove selected", self)
        remove_btn.clicked.connect(self._on_remove_clicked)
        rule_row.addWidget(remove_btn)
        rule_row.addStretch(1)
        deny_hint = QLabel(
            "Default deny: a send is rejected unless it matches a rule.",
            self,
        )
        deny_hint.setStyleSheet("color: palette(mid);")
        rule_row.addWidget(deny_hint)
        layout.addLayout(rule_row)

        # ---- Audit feed ----
        layout.addWidget(_section_label("Audit"))
        self._audit_list = QListWidget(self)
        self._audit_list.setUniformItemSizes(True)
        self._audit_list.setFont(tabular_font(point_size=9))
        self._audit_list.setMinimumHeight(180)
        layout.addWidget(self._audit_list, 1)

        # Footer.
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

    # ---- Slots: server toggle ----

    def _on_toggle_clicked(self) -> None:
        if self._ctx is None or self._bridge is None:
            return
        if self._bridge.is_running:
            self._bridge.stop()
        else:
            port = self._port_override or self._ctx.mcp_port
            self._bridge.start(host=self._ctx.mcp_host, port=port)

    def _on_copy_token(self) -> None:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._token_field.text())

    def _refresh_status_dot(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        color = {
            "ok": tokens["ok"],
            "error": tokens["err"],
        }.get(self._status_kind, tokens["fg_2"])
        self._status_dot.setStyleSheet(f"color: {color}; font-size: 16px;")

    def _on_bridge_started(self, url: str) -> None:
        self._status_kind = "ok"
        self._refresh_status_dot()
        self._status_text.setText(f"MCP server running at {url}")
        self._toggle_btn.setText("Stop server")

    def _on_bridge_stopped(self) -> None:
        self._status_kind = "idle"
        self._refresh_status_dot()
        self._status_text.setText("MCP server stopped")
        self._toggle_btn.setText("Start server")

    def _on_bridge_error(self, message: str) -> None:
        self._status_kind = "error"
        self._refresh_status_dot()
        self._status_text.setText(f"MCP error: {message}")
        # Common case: port already in use. Offer the next port up.
        action_label = "Choose another port" if "address" in message.lower() else None
        self._banner.show_message(message, kind="error", action_label=action_label)

    def _on_banner_action(self) -> None:
        """User clicked the banner's "Choose another port" action.

        Bumps the dialog's port override and tries to restart the bridge.
        AppContext is frozen, so the override lives on the dialog rather
        than on the shared context.
        """
        if self._ctx is None or self._bridge is None:
            return
        self._port_override = (self._port_override or self._ctx.mcp_port) + 1
        self._banner.hide()
        if not self._bridge.is_running:
            self._bridge.start(host=self._ctx.mcp_host, port=self._port_override)

    def _refresh_status_text(self) -> None:
        if self._bridge is None:
            return
        if self._bridge.is_running:
            self._toggle_btn.setText("Stop server")
        else:
            self._toggle_btn.setText("Start server")
            self._on_bridge_stopped()

    # ---- Slots: allowlist ----

    def _message_names(self) -> dict[int, str]:
        """``{can_id: dbc_message_name}`` for resolving raw IDs in the table."""
        if self._ctx is None:
            return {}
        return {int(m["id"]): str(m["name"]) for m in tools.list_messages(_read_context(self._ctx))}

    def _refresh_rules(self) -> None:
        if self._allowlist is None:
            return
        rules = self._allowlist.rules
        names = self._message_names()
        self._rules_table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            self._rules_table.setItem(row, 0, QTableWidgetItem(f"0x{rule.message_id:X}"))
            self._rules_table.setItem(row, 1, QTableWidgetItem(names.get(rule.message_id, "—")))
            self._rules_table.setItem(row, 2, QTableWidgetItem(rule.signal))
            self._rules_table.setItem(
                row,
                3,
                QTableWidgetItem("unlimited" if rule.rate_hz is None else f"{rule.rate_hz:g}"),
            )
            self._rules_table.setItem(row, 4, QTableWidgetItem(rule.agent_filter or "—"))
            self._rules_table.setItem(row, 5, QTableWidgetItem(rule.bus_name or "—"))
            # Stash the rule id on the first cell so removal can recover it.
            cell = self._rules_table.item(row, 0)
            if cell is not None:
                cell.setData(Qt.ItemDataRole.UserRole, int(rule.id))

    def _on_add_rule_clicked(self) -> None:
        if self._allowlist is None:
            return
        dlg = AddRuleDialog(self, ctx=self._ctx)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rule = dlg.build_rule()
        if rule is None:
            return
        self._allowlist.add_rule(rule)
        self._allowlist.save()
        self._refresh_rules()

    def _on_remove_clicked(self) -> None:
        if self._allowlist is None:
            return
        row = self._rules_table.currentRow()
        if row < 0:
            return
        item = self._rules_table.item(row, 0)
        if item is None:
            return
        rule_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(rule_id, int):
            return
        confirmed = QMessageBox.question(
            self,
            "Remove allowlist rule?",
            f"Remove rule {rule_id} from the allowlist?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self._allowlist.remove_rule(rule_id)
        self._allowlist.save()
        self._refresh_rules()

    # ---- Audit feed ----

    def _tail_audit(self) -> None:
        if self._audit_path is None or not self._audit_path.exists():
            return
        try:
            stat = self._audit_path.stat()
        except OSError:
            return
        # Reset the cursor if the file shrank (rotation).
        if stat.st_size < self._audit_tail_offset:
            self._audit_tail_offset = 0
            self._audit_list.clear()
        if stat.st_size == self._audit_tail_offset:
            return
        start = self._audit_tail_offset
        mid_line = start == 0 and stat.st_size > _AUDIT_TAIL_BYTES
        if mid_line:
            start = stat.st_size - _AUDIT_TAIL_BYTES
        try:
            with open(self._audit_path, "rb") as f:
                f.seek(start)
                new_bytes = f.read()
            self._audit_tail_offset = stat.st_size
        except OSError:
            return
        if mid_line:
            new_bytes = new_bytes.partition(b"\n")[2]

        lines = new_bytes.decode("utf-8", errors="replace").splitlines()
        for line in lines[-_AUDIT_TAIL_LIMIT:]:
            if not line.strip():
                continue
            self._append_audit_line(line)

        # Cap visible items.
        excess = self._audit_list.count() - _AUDIT_TAIL_LIMIT
        if excess > 0:
            for _ in range(excess):
                self._audit_list.takeItem(0)
        self._audit_list.scrollToBottom()

    def _append_audit_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self._audit_list.addItem(QListWidgetItem(line))
            return
        ts = event.get("ts", "")
        decision = event.get("decision", "")
        outcome = event.get("outcome", "")
        call = event.get("call", "")
        reason = event.get("reason", "")
        rule_id = event.get("rule_id")
        suffix = f" rule={rule_id}" if rule_id is not None else ""
        text = f"{_short_ts(ts)}  {decision:5s}  {outcome:11s}  {call:14s}  {reason}{suffix}"
        item = QListWidgetItem(text)
        tokens = ThemeManager.instance().current_tokens()
        if outcome in ("SEND_FAILED", "UNKNOWN"):
            item.setForeground(QColor(tokens["warn"]))
        elif decision == "ALLOW":
            item.setForeground(QColor(tokens["ok"]))
        elif decision == "DENY":
            item.setForeground(QColor(tokens["err"]))
        self._audit_list.addItem(item)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._audit_timer.stop()
        super().closeEvent(event)


# --------------------------------------------------------------------------
# Add-rule modal
# --------------------------------------------------------------------------


class AddRuleDialog(QDialog):
    """Modal that captures a new ``AllowRule``'s fields.

    Message and signal are picked from the loaded DBCs rather than typed
    as raw numbers — the pickers read through ``mcp.tools.list_messages``
    / ``list_signals``, the same functions the agent sees, so the GUI and
    the agent can never disagree about the namespace. Both combos stay
    editable so an un-DBC'd CAN ID (``0x301``) is still accepted.
    """

    ANY_BUS = "(any bus)"

    def __init__(self, parent: QWidget | None = None, ctx: AppContext | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self.setWindowTitle("Add allowlist rule")
        self.resize(460, 320)

        form = QFormLayout(self)

        self._message_combo = QComboBox(self)
        self._message_combo.setEditable(True)
        self._message_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        message_edit = self._message_combo.lineEdit()
        if message_edit is not None:
            message_edit.setPlaceholderText("pick a message, or type 0x301")
        self._message_combo.currentIndexChanged.connect(self._on_message_changed)
        form.addRow("Message", self._message_combo)

        self._signal_combo = QComboBox(self)
        self._signal_combo.setEditable(True)
        self._signal_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._signal_combo.currentIndexChanged.connect(self._refresh_signal_hint)
        form.addRow("Signal", self._signal_combo)

        self._signal_hint = QLabel("", self)
        self._signal_hint.setStyleSheet("color: palette(mid);")
        self._signal_hint.setWordWrap(True)
        form.addRow("", self._signal_hint)

        # 0 reads as "unlimited" via specialValueText, so the field is
        # always live — the old checkbox left the spinner disabled on open,
        # which made its arrows look broken.
        self._rate_spin = QDoubleSpinBox(self)
        self._rate_spin.setRange(0.0, 10_000.0)
        self._rate_spin.setDecimals(2)
        self._rate_spin.setSingleStep(1.0)
        self._rate_spin.setSuffix(" Hz")
        self._rate_spin.setSpecialValueText("unlimited")
        self._rate_spin.setValue(0.0)
        self._rate_spin.setToolTip("Maximum send rate for this rule. 0 = unlimited.")
        form.addRow("Rate limit", self._rate_spin)

        self._agent_edit = QLineEdit(self)
        self._agent_edit.setPlaceholderText("blank = any agent (advisory only)")
        form.addRow("Agent filter", self._agent_edit)

        self._bus_combo = QComboBox(self)
        self._bus_combo.setEditable(True)
        self._bus_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form.addRow("Bus filter", self._bus_combo)

        self._enforce_ranges = QCheckBox("Enforce DBC signal min/max", self)
        self._enforce_ranges.setChecked(True)
        form.addRow("", self._enforce_ranges)

        self._wildcard_warning = QLabel("", self)
        self._wildcard_warning.setWordWrap(True)
        form.addRow("", self._wildcard_warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._populate_from_project()

    # ---- Population ----

    def _populate_from_project(self) -> None:
        self._bus_combo.addItem(self.ANY_BUS)
        if self._ctx is None:
            self._signal_combo.addItem(WILDCARD_SIGNAL)
            self._refresh_signal_hint()
            return

        for name in _known_bus_names(self._ctx):
            self._bus_combo.addItem(name)

        read_ctx = _read_context(self._ctx)
        for msg in sorted(tools.list_messages(read_ctx), key=lambda m: str(m["name"])):
            label = f"{msg['name']}  ·  {msg['id_hex']}"
            self._message_combo.addItem(label, int(msg["id"]))
        self._message_combo.setCurrentIndex(-1)
        self._message_combo.setCurrentText("")
        self._on_message_changed()

    def _on_message_changed(self) -> None:
        """Repopulate the signal picker for the selected message."""
        self._signal_combo.clear()
        self._signal_combo.addItem(WILDCARD_SIGNAL)
        name = self._selected_message_name()
        if name is not None and self._ctx is not None:
            read_ctx = _read_context(self._ctx)
            for sig in tools.list_signals(read_ctx, name):
                self._signal_combo.addItem(str(sig["signal"]), sig)
        self._signal_combo.setCurrentIndex(0)
        self._refresh_signal_hint()

    def _refresh_signal_hint(self) -> None:
        data = self._signal_combo.currentData()
        if not isinstance(data, dict):
            self._signal_hint.setText(
                "* authorises raw send_frame for this ID — any payload, any signal."
                if self._signal_combo.currentText().strip() == WILDCARD_SIGNAL
                else ""
            )
            self._refresh_wildcard_warning()
            return
        unit = data.get("unit") or ""
        lo, hi = data.get("minimum"), data.get("maximum")
        bits = data.get("length_bits")
        parts = [f"{bits} bits"] if bits else []
        if lo is not None and hi is not None:
            parts.append(f"range {lo:g} … {hi:g}{' ' + unit if unit else ''}")
        elif unit:
            parts.append(unit)
        if data.get("choices"):
            parts.append(f"{len(data['choices'])} named values")
        self._signal_hint.setText(" · ".join(parts))
        self._refresh_wildcard_warning()

    def _refresh_wildcard_warning(self) -> None:
        wildcard = self._signal_combo.currentText().strip() == WILDCARD_SIGNAL
        if wildcard:
            tokens = ThemeManager.instance().current_tokens()
            self._wildcard_warning.setStyleSheet(f"color: {tokens['warn']};")
            self._wildcard_warning.setText(
                "⚠ A wildcard rule is the broadest grant available. Prefer a "
                "named signal unless the agent genuinely needs raw frames."
            )
        else:
            self._wildcard_warning.setText("")

    # ---- Resolution ----

    def _selected_message_name(self) -> str | None:
        """DBC message name for the current selection, or ``None`` for a raw ID."""
        idx = self._message_combo.currentIndex()
        if idx < 0:
            return None
        if self._message_combo.itemText(idx) != self._message_combo.currentText():
            return None
        return self._message_combo.itemText(idx).split("  ·  ", 1)[0]

    def _resolve_message_id(self) -> int | None:
        text = self._message_combo.currentText().strip()
        if not text:
            return None
        idx = self._message_combo.findText(text)
        if idx >= 0:
            data = self._message_combo.itemData(idx)
            if isinstance(data, int):
                return data
        try:
            return int(text, 0)
        except ValueError:
            return None

    def build_rule(self) -> AllowRule | None:
        raw = self._message_combo.currentText().strip()
        if not raw:
            return None
        message_id = self._resolve_message_id()
        if message_id is None:
            QMessageBox.warning(
                self,
                "Unknown message",
                f"{raw!r} is neither a loaded DBC message nor a CAN ID "
                f"(try a hex value like 0x301).",
            )
            return None
        signal = self._signal_combo.currentText().strip() or WILDCARD_SIGNAL
        # 0 Hz is the specialValueText "unlimited".
        rate = self._rate_spin.value() or None
        agent = self._agent_edit.text().strip() or None
        bus = self._bus_combo.currentText().strip()
        return AllowRule(
            id=0,
            message_id=message_id,
            signal=signal,
            rate_hz=rate,
            agent_filter=agent,
            bus_name=None if bus in ("", self.ANY_BUS) else bus,
            enforce_signal_ranges=self._enforce_ranges.isChecked(),
            created_at=datetime.now(UTC),
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet(
        "color: palette(mid); letter-spacing: 0.6px; font-size: 11px; padding-top: 4px;"
    )
    return label


def _read_context(ctx: AppContext) -> McpReadContext:
    """Same read context the MCP server hands its tools.

    Building it here means the dialog's pickers and the agent's
    ``list_messages`` / ``list_signals`` cannot drift apart.
    """
    return McpReadContext(
        bus_reader=ctx.bus_reader,
        database=ctx.database,
        allowlist=ctx.allowlist,
        audit_path=ctx.audit_path,
    )


def _known_bus_names(ctx: AppContext) -> list[str]:
    """Open buses plus any configured in the project, in a stable order."""
    names = list(ctx.bus_reader.bus_names())
    for cfg in ctx.project.workspace.bus:
        if cfg.name not in names:
            names.append(cfg.name)
    return names


def _short_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return ts[:8]
