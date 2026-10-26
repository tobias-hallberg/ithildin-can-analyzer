# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Create CAN bus dialog — define a named bus profile, optionally connect.

A modal ``QDialog`` (design handoff ``design_handoff_create_can_bus``):
name, device + channel, baudrate, a prominent listen-only safety panel,
a connect-on-startup toggle, and a DBC picker (:class:`DbcPicker`). The
footer offers **Create** (declare only) and **Create & connect** (declare
then open just this bus).

Scope: CAN FD is out, so there is no bus-mode/data-baudrate row. The
dialog only sets ``BusConfig`` fields the bus layer actually consumes
(bitrate, listen-only, and ``connect_on_startup``, acted on at launch).
Picked databases ride alongside the emitted ``BusConfig`` as parsed
``(path, Decoder)`` pairs; the left dock binds them to the new bus.

Device metadata comes from :class:`AdapterProber`, which the dialog runs
on its own worker thread (open + rescan). In dev mode the virtual
loopback is seeded synchronously so the dialog is usable before hardware
enumeration finishes; in normal use it is hidden, and an empty device
list shows a "connect a device and rescan" hint instead.
"""

from __future__ import annotations

from itertools import pairwise

from pydantic import ValidationError
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ithildin.ui.adapter_enrich import ChannelInfo, DeviceInfo
from ithildin.ui.adapter_prober import (
    AdapterProber,
    dev_mode,
    register_prober,
    virtual_device,
)
from ithildin.ui.banner import Banner
from ithildin.ui.dbc_picker import DbcPicker
from ithildin.ui.fonts import tabular_font
from ithildin.ui.icons import themed_icon
from ithildin.ui.pill_toggle import IthildinPillToggle
from ithildin.ui.theme import ThemeManager
from ithildin.workspace.schema import (
    LISTEN_ONLY_INTERFACES,
    BusConfig,
    ChannelKey,
    channel_keys,
)

__all__ = ["CreateCanBusDialog"]

_BAUDRATES = (125_000, 250_000, 500_000, 800_000, 1_000_000)
_BAUDRATES_BY_INTERFACE: dict[str, tuple[int, ...]] = {
    # CANlib has no 800 kbit/s preset, and a Kvaser bus refuses to open on
    # anything but a preset (core.bus.BusWorker._open_kvaser).
    "kvaser": (125_000, 250_000, 500_000, 1_000_000),
}
_DEFAULT_BAUD = 500_000

_LISTEN_ONLY_COPY = (
    "Decode incoming frames only. The interface will never transmit — "
    "including ACK bits — so it is invisible on the bus. Use when probing "
    "a system you must not perturb."
)


def _format_bitrate(bps: int) -> str:
    if bps >= 1_000_000:
        return f"{bps // 1_000_000} Mbit/s"
    return f"{bps // 1_000} kbit/s"


def _suggested_name(device: DeviceInfo, channel: ChannelInfo, taken: set[str]) -> str:
    if device.interface == "virtual":
        base = "CAN"
    else:
        base = f"{device.vendor} {channel.label}"
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


class _ListenOnlyPanel(QFrame):
    """Safety-critical listen-only control — a clickable card, amber when on."""

    toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(12)

        self._eye = QLabel(self)
        themed_icon("eye", self._set_eye_icon)
        self._eye.setFixedWidth(20)
        self._eye.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._eye)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title = QLabel("Listen-only mode", self)
        title_row.addWidget(title)
        self._badge = QLabel("OFF", self)
        self._badge.setFont(tabular_font(7))
        self._badge.setObjectName("ListenOnlyBadge")
        title_row.addWidget(self._badge)
        title_row.addStretch(1)
        col.addLayout(title_row)
        copy = QLabel(_LISTEN_ONLY_COPY, self)
        copy.setWordWrap(True)
        copy.setObjectName("ListenOnlyCopy")
        col.addWidget(copy)
        row.addLayout(col, 1)

        self._switch = IthildinPillToggle(danger=True, parent=self)
        self._switch.toggled.connect(self._on_switch)
        row.addWidget(self._switch, 0, Qt.AlignmentFlag.AlignTop)

    def _set_eye_icon(self, ic: object) -> None:
        self._eye.setPixmap(ic.pixmap(18, 18))  # type: ignore[attr-defined]

    def is_on(self) -> bool:
        return self._switch.isChecked()

    def set_locked_off(self, locked: bool) -> None:
        """Force off + disable (e.g. ixxat has no silent mode)."""
        if locked and self._switch.isChecked():
            self._switch.setChecked(False)
        self._switch.setEnabled(not locked)
        self.setCursor(Qt.CursorShape.ArrowCursor if locked else Qt.CursorShape.PointingHandCursor)
        self._locked = locked

    def mousePressEvent(self, _event: object) -> None:
        if self._switch.isEnabled():
            self._switch.toggle()

    def _on_switch(self, on: bool) -> None:
        self._badge.setText("ON" if on else "OFF")
        self.setProperty("on", on)
        self.style().unpolish(self)
        self.style().polish(self)
        self.toggled.emit(on)

    _locked = False


class _ToggleRow(QWidget):
    """A pill toggle with a label + hint, laid out like the design rows."""

    def __init__(
        self, label: str, hint: str, *, on: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(10)
        self.toggle = IthildinPillToggle(parent=self)
        self.toggle.setChecked(on)
        row.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        self._label = QLabel(label, self)
        col.addWidget(self._label)
        self._hint = QLabel(hint, self)
        self._hint.setWordWrap(True)
        self._hint.setObjectName("ToggleHint")
        col.addWidget(self._hint)
        row.addLayout(col, 1)


class CreateCanBusDialog(QDialog):
    """Declare a CAN bus; optionally open it immediately."""

    DEFAULT_SIZE = (560, 700)

    bus_declared = Signal(BusConfig, list)  # (config, [(path, Decoder), …])
    bus_declared_and_connect = Signal(BusConfig, list)

    def __init__(
        self,
        *,
        existing_names: set[str],
        existing_keys: frozenset[ChannelKey] = frozenset(),
        hung_backends: set[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create CAN bus")
        w, h = self.DEFAULT_SIZE
        self.resize(w, h)
        self._existing = set(existing_names)
        # ``schema.channel_keys`` of every declared bus.
        self._existing_keys = existing_keys
        # The dock's set, shared: a backend either prober timed out on is
        # skipped by both from then on.
        self._hung_backends = hung_backends if hung_backends is not None else set()
        self._devices: list[DeviceInfo] = []
        self._channel: ChannelInfo | None = None
        self._name_edited = False
        self._prober: AdapterProber | None = None
        self._prober_thread: QThread | None = None

        self._build_body()
        self._seed_devices()
        self._apply_theme(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme)
        self._start_probe()
        self._validate()

    # -- construction -------------------------------------------------------

    def _build_body(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(12)

        subtitle = QLabel("Configure a hardware interface and save as a named bus.")
        subtitle.setObjectName("DialogSubtitle")
        layout.addWidget(subtitle)

        # Name
        layout.addWidget(self._field_label("Name", required=True))
        self._name_edit = QLineEdit(self)
        self._name_edit.setPlaceholderText("Powertrain")
        self._name_edit.textEdited.connect(self._on_name_edited)
        self._name_edit.textChanged.connect(lambda _t: self._validate())
        layout.addWidget(self._name_edit)
        self._name_error = Banner(kind="error", parent=self)
        self._name_error.setVisible(False)
        layout.addWidget(self._name_error)

        # Device
        dev_label_row = QHBoxLayout()
        dev_label_row.setContentsMargins(0, 0, 0, 0)
        dev_label_row.addWidget(self._field_label("Device", required=True))
        dev_label_row.addStretch(1)
        self._rescan_btn = QPushButton("↻ Rescan", self)
        self._rescan_btn.setObjectName("LinkButton")
        self._rescan_btn.setFlat(True)
        self._rescan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rescan_btn.clicked.connect(self._start_probe)
        dev_label_row.addWidget(self._rescan_btn)
        layout.addLayout(dev_label_row)

        self._device_combo = QComboBox(self)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        layout.addWidget(self._device_combo)

        self._info_strip = QLabel(self)
        self._info_strip.setObjectName("DeviceInfoStrip")
        self._info_strip.setFont(tabular_font(8))
        self._info_strip.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._info_strip)

        self._empty_hint = QLabel("No CAN adapters detected — connect a device and ↻ Rescan.", self)
        self._empty_hint.setObjectName("DeviceInfoStrip")
        self._empty_hint.setFont(tabular_font(8))
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setVisible(False)
        layout.addWidget(self._empty_hint)

        # Channels
        self._channel_label = self._field_label("Channel")
        layout.addWidget(self._channel_label)
        self._channel_box = QWidget(self)
        self._channel_layout = QHBoxLayout(self._channel_box)
        self._channel_layout.setContentsMargins(0, 0, 0, 0)
        self._channel_layout.setSpacing(4)
        self._channel_layout.addStretch(1)
        layout.addWidget(self._channel_box)

        # Baudrate
        layout.addWidget(self._field_label("Baudrate"))
        self._baud_combo = QComboBox(self)
        self._offer_baudrates(_BAUDRATES)
        layout.addWidget(self._baud_combo)

        # Listen-only
        self._listen_only = _ListenOnlyPanel(self)
        self._listen_only.setObjectName("ListenOnlyPanel")
        self._listen_only.toggled.connect(lambda _on: self._refresh_safety_hint())
        layout.addWidget(self._listen_only)

        # Connect on startup
        self._startup_row = _ToggleRow(
            "Connect on startup", "Open this bus when Ithildin launches."
        )
        layout.addWidget(self._startup_row)

        # Databases (DBC) — picked or dropped here, parsed off-thread, bound to
        # the new bus by the left dock on Create. The dashed box is a drop zone.
        self._dbc_picker = DbcPicker(self)
        self._dbc_picker.changed.connect(self._validate)
        layout.addWidget(self._dbc_picker)

        layout.addStretch(1)

        # Footer
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        self._safety_hint = QLabel(self)
        self._safety_hint.setObjectName("SafetyHint")
        self._safety_hint.setFont(tabular_font(8))
        footer.addWidget(self._safety_hint)
        footer.addStretch(1)
        self._cancel_btn = QPushButton("Cancel", self)
        self._cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self._cancel_btn)
        self._create_btn = QPushButton("Create", self)
        self._create_btn.clicked.connect(self._on_create)
        footer.addWidget(self._create_btn)
        self._connect_btn = QPushButton("Create && connect", self)
        self._connect_btn.setDefault(True)
        self._connect_btn.clicked.connect(self._on_create_and_connect)
        footer.addWidget(self._connect_btn)
        layout.addLayout(footer)

        QShortcut(QKeySequence.StandardKey.Save, self, self._on_create)
        self._wire_tab_order()
        self._refresh_safety_hint()

    def _field_label(self, text: str, *, required: bool = False) -> QLabel:
        lbl = QLabel(f"{text} ·" if required else text, self)
        lbl.setObjectName("FieldLabel")
        return lbl

    def _wire_tab_order(self) -> None:
        chain: list[QWidget] = [
            self._name_edit,
            self._device_combo,
            self._rescan_btn,
            self._baud_combo,
            self._listen_only._switch,
            self._startup_row.toggle,
            self._cancel_btn,
            self._create_btn,
            self._connect_btn,
        ]
        for a, b in pairwise(chain):
            self.setTabOrder(a, b)

    # -- device probing -----------------------------------------------------

    def _seed_devices(self) -> None:
        """Seed the virtual loopback so the dialog has a connectable choice
        before hardware enumeration finishes — dev mode only."""
        if dev_mode():
            self._add_device(virtual_device())

    def _start_probe(self) -> None:
        if self._prober is not None:
            return
        self._rescan_btn.setEnabled(False)
        # Reset to just the seed; the probe re-adds hardware devices.
        self._devices = []
        self._device_combo.clear()
        self._seed_devices()
        self._refresh_empty_state()
        self._prober = AdapterProber(skip_backends=frozenset(self._hung_backends))
        self._prober_thread = QThread()
        self._prober.moveToThread(self._prober_thread)
        self._prober_thread.started.connect(self._prober.run)
        self._prober.device_found.connect(self._on_device_found)
        self._prober.backend_failed.connect(self._on_backend_failed)
        self._prober.finished.connect(self._on_probe_finished)
        self._prober.finished.connect(self._prober_thread.quit, Qt.ConnectionType.DirectConnection)
        self._prober.finished.connect(self._prober.deleteLater)
        self._prober_thread.finished.connect(self._prober_thread.deleteLater)
        # Retain across our own lifetime + arm the app-quit join, so a
        # probe still running when this dialog closes can't crash teardown.
        register_prober(self._prober, self._prober_thread)
        self._prober_thread.start()

    def _stop_probe(self) -> None:
        """Detach + cancel our probe when the dialog closes.

        We disconnect our slots so the still-running worker can't call
        back into a closing dialog, then cancel it. The registry keeps
        the thread alive until it actually finishes; we don't block here.
        """
        prober = self._prober
        if prober is None:
            return
        for signal, slot in (
            (prober.device_found, self._on_device_found),
            (prober.backend_failed, self._on_backend_failed),
            (prober.finished, self._on_probe_finished),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        prober.cancel()
        self._prober = None
        self._prober_thread = None

    def done(self, result: int) -> None:
        self._stop_probe()
        self._dbc_picker.shutdown()
        super().done(result)

    def _on_device_found(self, device: DeviceInfo) -> None:
        if device.interface == "virtual":
            return  # already seeded
        self._add_device(device)

    def _on_backend_failed(self, backend: str, reason: str) -> None:
        if reason == "timeout":
            self._hung_backends.add(backend)

    def _on_probe_finished(self) -> None:
        self._prober = None
        self._prober_thread = None
        self._rescan_btn.setEnabled(True)
        self._refresh_empty_state()

    def _add_device(self, device: DeviceInfo) -> None:
        if any(d.key == device.key for d in self._devices):
            return
        self._devices.append(device)
        self._device_combo.addItem(device.display_label)
        self._refresh_empty_state()

    def _refresh_empty_state(self) -> None:
        """Show a "no adapters" hint when the device list is empty (the
        common case in normal use with no hardware attached and the loopback
        hidden). When a device is present, ``_render_info_strip`` owns the
        strip's visibility — it hides itself for devices with no real data."""
        empty = self._device_combo.count() == 0
        self._empty_hint.setVisible(empty)
        if empty:
            self._info_strip.setVisible(False)
        else:
            device = self._current_device()
            if device is not None:
                self._render_info_strip(device)

    # -- selection / rendering ---------------------------------------------

    def _current_device(self) -> DeviceInfo | None:
        idx = self._device_combo.currentIndex()
        if 0 <= idx < len(self._devices):
            return self._devices[idx]
        return None

    def _on_device_changed(self, _idx: int) -> None:
        device = self._current_device()
        if device is None:
            return
        self._render_info_strip(device)
        self._render_channels(device)
        self._apply_device_capabilities(device)
        if not self._name_edited and self._channel is not None:
            self._name_edit.setText(_suggested_name(device, self._channel, self._existing))
        self._validate()

    def _render_info_strip(self, device: DeviceInfo) -> None:
        """Show only fields that were genuinely probed — status and
        capabilities are ``None`` for backends that don't read them, and
        rendering a default there would fake telemetry the probe never saw.
        ``transport`` is a static label, so a device with none of status,
        firmware, or capabilities has nothing worth showing: hide the strip
        rather than leave a bare ``USB``."""
        has_data = device.status is not None or bool(device.firmware) or bool(device.capabilities)
        self._info_strip.setVisible(has_data)
        if not has_data:
            self._info_strip.clear()
            return
        tokens = ThemeManager.instance().current_tokens()
        parts: list[str] = []
        if device.status is not None:
            dot = {"available": tokens["ok"], "in-use": tokens["warn"]}.get(
                device.status, tokens["fg_3"]
            )
            parts.append(f"<span style='color:{dot}'>●</span> {device.status}")
        parts.append(device.transport)
        if device.firmware:
            parts.append(f"fw {device.firmware}")
        if device.capabilities:
            parts.append(" / ".join(device.capabilities))
        self._info_strip.setText("  ·  ".join(parts))

    def _render_channels(self, device: DeviceInfo) -> None:
        while self._channel_layout.count():
            item = self._channel_layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)

        taken = [self._channel_taken(device.interface, ch) for ch in device.channels]
        # A single-channel device normally hides the row; reveal it when its
        # sole channel is unavailable so the greyed pill explains why Create
        # is disabled.
        any_unavailable = any(
            t or ch.status != "free" for ch, t in zip(device.channels, taken, strict=True)
        )
        show = len(device.channels) > 1 or any_unavailable
        self._channel_label.setVisible(show)
        self._channel_box.setVisible(show)

        first_free: ChannelInfo | None = None
        for ch, is_taken in zip(device.channels, taken, strict=True):
            pill = QPushButton(ch.label, self._channel_box)
            pill.setObjectName("ChannelPill")
            pill.setCheckable(True)
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            free = ch.status == "free" and not is_taken
            pill.setEnabled(free)
            if is_taken:
                pill.setToolTip("Already added as a bus")
            elif ch.status != "free":
                pill.setText(f"{ch.label}  ({ch.status})")
            pill.clicked.connect(lambda _c=False, c=ch, b=pill: self._select_channel(c, b))
            self._channel_layout.addWidget(pill)
            if free and first_free is None:
                first_free = ch
                pill.setChecked(True)
        self._channel_layout.addStretch(1)
        self._channel = first_free

    def _select_channel(self, channel: ChannelInfo, btn: QPushButton) -> None:
        for i in range(self._channel_layout.count()):
            item = self._channel_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, QPushButton) and w is not btn:
                w.setChecked(False)
        btn.setChecked(True)
        self._channel = channel
        self._validate()

    def _apply_device_capabilities(self, device: DeviceInfo) -> None:
        # Listen-only: only adapters with a silent kwarg ithildin can set
        # (socketcan/ixxat have none — see LISTEN_ONLY_INTERFACES).
        locked = device.interface not in LISTEN_ONLY_INTERFACES
        self._listen_only.set_locked_off(locked)
        self._refresh_safety_hint()
        self._offer_baudrates(_BAUDRATES_BY_INTERFACE.get(device.interface, _BAUDRATES))

    def _offer_baudrates(self, rates: tuple[int, ...]) -> None:
        """Fill the baudrate combo, keeping the choice while it is offered."""
        current = self._baud_combo.currentData()
        self._baud_combo.clear()
        for br in rates:
            self._baud_combo.addItem(_format_bitrate(br), br)
        self._baud_combo.setCurrentIndex(
            rates.index(current if current in rates else _DEFAULT_BAUD)
        )

    # -- validation ---------------------------------------------------------

    def _channel_taken(self, interface: str, channel: ChannelInfo) -> bool:
        keys = channel_keys(interface, channel.channel, channel.hw_id)
        return not keys.isdisjoint(self._existing_keys)

    def _name_problem(self) -> str | None:
        name = self._name_edit.text().strip()
        if not name:
            return "Name is required."
        if name in self._existing:
            return f"A bus named {name!r} already exists."
        return None

    def _validate(self) -> None:
        problem = self._name_problem()
        if problem:
            self._name_error.show_message(problem, kind="error")
            self._name_error.setVisible(True)
        else:
            self._name_error.setVisible(False)

        device = self._current_device()
        has_channel = self._channel is not None
        # A DBC still parsing or one that failed blocks create: we won't ship a
        # bus that silently dropped a database the user added (the failed row's
        # ✗ + tooltip tell them to remove it).
        dbc_ready = self._dbc_picker.is_ready()
        can_create = problem is None and has_channel and dbc_ready
        offline = device is not None and device.status == "offline"
        self._create_btn.setEnabled(can_create)
        self._connect_btn.setEnabled(can_create and not offline)

    def _refresh_safety_hint(self) -> None:
        tokens = ThemeManager.instance().current_tokens()
        if self._listen_only.is_on():
            self._safety_hint.setText("Listen-only · safe")
            self._safety_hint.setStyleSheet(f"color: {tokens['fg_3']};")
        else:
            self._safety_hint.setText("⚠ This bus will transmit when used")
            self._safety_hint.setStyleSheet(f"color: {tokens['warn']};")

    # -- input handlers -----------------------------------------------------

    def _on_name_edited(self, text: str) -> None:
        self._name_edited = bool(text.strip())

    # -- submit -------------------------------------------------------------

    def _build_config(self) -> BusConfig | None:
        device = self._current_device()
        if device is None or self._channel is None or self._name_problem():
            return None
        try:
            return BusConfig(
                name=self._name_edit.text().strip(),
                interface=device.interface,
                channel=self._channel.channel,
                bitrate=int(self._baud_combo.currentData() or _DEFAULT_BAUD),
                fd=False,
                listen_only=self._listen_only.is_on(),
                connect_on_startup=self._startup_row.toggle.isChecked(),
                hw_id=self._channel.hw_id,
            )
        except ValidationError as exc:
            self._name_error.show_message(_first_error_message(exc), kind="error")
            self._name_error.setVisible(True)
            return None

    def _on_create(self) -> None:
        config = self._build_config()
        if config is None:
            return
        self.bus_declared.emit(config, self._dbc_picker.results())
        self.accept()

    def _on_create_and_connect(self) -> None:
        config = self._build_config()
        if config is None:
            return
        self.bus_declared_and_connect.emit(config, self._dbc_picker.results())
        self.accept()

    # -- theming ------------------------------------------------------------

    def _apply_theme(self, tokens: dict[str, str]) -> None:
        amber_bg = tokens["warn_bg"]
        self.setStyleSheet(
            f"QLabel#DialogSubtitle {{ color: {tokens['fg_3']}; }}"
            f"QLabel#FieldLabel {{ color: {tokens['fg_2']}; font-size: 8pt; }}"
            f"QLabel#DeviceInfoStrip {{ color: {tokens['fg_3']};"
            f" background: {tokens['bg_1']}; border: 1px solid {tokens['border_0']};"
            f" border-radius: 3px; padding: 4px 6px; }}"
            f"QLabel#ToggleHint, QLabel#ListenOnlyCopy {{ color: {tokens['fg_3']}; }}"
            f"QPushButton#LinkButton {{ color: {tokens['fg_2']}; border: none; }}"
            f"QPushButton#ChannelPill {{ background: {tokens['bg_2']};"
            f" color: {tokens['fg_1']}; border: 1px solid {tokens['border_1']};"
            f" border-radius: 3px; padding: 4px 8px; }}"
            f"QPushButton#ChannelPill:checked {{ background: {tokens['accent_bg']};"
            f" color: {tokens['accent']}; border-color: {tokens['accent_dim']}; }}"
            f"QPushButton#ChannelPill:disabled {{ color: {tokens['fg_3']}; }}"
            f"QFrame#ListenOnlyPanel {{ background: {tokens['bg_2']};"
            f" border: 1px solid {tokens['border_1']}; border-radius: 4px; }}"
            f'QFrame#ListenOnlyPanel[on="true"] {{ background: {amber_bg};'
            f" border: 1px solid {tokens['warn']}; }}"
            f"QLabel#ListenOnlyBadge {{ color: {tokens['fg_3']};"
            f" border: 1px solid {tokens['border_1']}; border-radius: 2px;"
            f" padding: 0px 4px; }}"
            f"QWidget#DbcDropBox {{ border: 1px dashed {tokens['border_1']};"
            f" border-radius: 4px; }}"
            f'QWidget#DbcDropBox[dragActive="true"] {{'
            f" border: 1px dashed {tokens['accent']};"
            f" background: {tokens['accent_bg']}; }}"
            f"QLabel#DbcStatus, QLabel#DbcHint {{ color: {tokens['fg_3']}; }}"
        )
        self._refresh_safety_hint()
        device = self._current_device()
        if device is not None:
            self._render_info_strip(device)


def _first_error_message(exc: ValidationError) -> str:
    """A human-readable line for the first validation error."""
    errors = exc.errors()
    if errors:
        field = str(errors[0].get("loc", ("",))[0])
        return f"{field}: {errors[0].get('msg', 'invalid value')}"
    return "Invalid bus configuration."
