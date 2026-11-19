# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""The socketcand interface: schema, open kwargs, a loopback server, the dialog."""

from __future__ import annotations

import socket
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import can
import pytest
from pydantic import ValidationError

from ithildin.core.bus import BusController, BusWorker
from ithildin.core.ringbuf import RingBuffer
from ithildin.ui import adapter_prober as prober_mod
from ithildin.ui.create_bus_dialog import CreateCanBusDialog
from ithildin.workspace.loader import load_workspace, save_workspace
from ithildin.workspace.schema import BusConfig, Workspace


def _cfg(**kw: Any) -> BusConfig:
    base: dict[str, Any] = {
        "name": "Sim",
        "interface": "socketcand",
        "channel": "can0",
        "bitrate": 500_000,
    }
    return BusConfig(**{**base, **kw})


def test_socketcand_config_roundtrip(tmp_path: Path) -> None:
    ws = Workspace(name="sc", bus=[_cfg(host="192.0.2.7", port=29537)])
    out = tmp_path / "sc.itp"
    save_workspace(ws, out)
    bus = load_workspace(out).bus[0]
    assert (bus.interface, bus.host, bus.port, bus.channel) == (
        "socketcand",
        "192.0.2.7",
        29537,
        "can0",
    )


def test_socketcand_defaults_to_the_sim_bridge_address() -> None:
    cfg = _cfg()
    assert (cfg.host, cfg.port) == ("127.0.0.1", 29536)


def test_other_interfaces_carry_no_host_or_port(tmp_path: Path) -> None:
    out = tmp_path / "p.itp"
    save_workspace(
        Workspace(
            name="p",
            bus=[BusConfig(name="a", interface="pcan", channel="PCAN_USBBUS1", bitrate=500_000)],
        ),
        out,
    )
    bus = tomllib.loads(out.read_text())["bus"][0]
    assert "host" not in bus and "port" not in bus


def test_socketcand_host_port_only_for_socketcand() -> None:
    with pytest.raises(ValidationError, match="socketcand"):
        BusConfig(name="a", interface="pcan", channel="PCAN_USBBUS1", bitrate=500_000, host="h")
    with pytest.raises(ValidationError, match="socketcand"):
        BusConfig(name="a", interface="pcan", channel="PCAN_USBBUS1", bitrate=500_000, port=1)


@pytest.mark.parametrize("port", [0, 65535, -1])
def test_socketcand_port_range(port: int) -> None:
    with pytest.raises(ValidationError):
        _cfg(port=port)


def test_socketcand_listen_only_is_the_app_level_block() -> None:
    assert _cfg(listen_only=True).listen_only is True


def test_socketcand_opens_with_tcp_tune_off(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, Any] = {}
    real_bus = can.Bus

    def fake_bus(**kwargs: Any) -> can.BusABC:
        seen.update(kwargs)
        return real_bus(interface="virtual", channel="sc-kwargs")

    monkeypatch.setattr(can, "Bus", fake_bus)
    worker = BusWorker("Sim", _cfg(host="h.example", port=1234), RingBuffer(8))
    bus = worker._open_bus()
    bus.shutdown()
    assert seen["interface"] == "socketcand"
    assert (seen["host"], seen["port"], seen["channel"]) == ("h.example", 1234, "can0")
    assert seen["tcp_tune"] is False
    assert "bitrate" not in seen


class _StubServer:
    """The socketcand handshake and rawmode over loopback: one client."""

    def __init__(self) -> None:
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.got: list[str] = []
        self.conn: socket.socket | None = None
        self.ready = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self.srv.accept()
        self.conn = conn
        conn.sendall(b"< hi >")
        buf = ""
        while True:
            try:
                data = conn.recv(1024)
            except OSError:
                return
            if not data:
                return
            buf += data.decode("ascii")
            while ">" in buf:
                msg, buf = buf[: buf.index(">") + 1], buf[buf.index(">") + 1 :]
                self.got.append(msg.strip())
                if msg.strip() in ("< open can0 >", "< rawmode >"):
                    conn.sendall(b"< ok >")
                    if msg.strip() == "< rawmode >":
                        # python-can reads the handshake one recv at a time.
                        time.sleep(0.2)
                        conn.sendall(b"< frame 123 0.0 DEAD >")
                        self.ready.set()

    def close(self) -> None:
        for s in (self.conn, self.srv):
            if s is not None:
                s.close()


def test_socketcand_against_a_local_server(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    server = _StubServer()
    ring = RingBuffer(64)
    controller = BusController(_cfg(port=server.port), ring)
    try:
        with qtbot.waitSignal(controller.started, timeout=5000):
            controller.start()
        qtbot.waitUntil(lambda: ring.total_seq >= 1, timeout=3000)
        rx = ring.read_since(-1)[0][0]
        assert (rx.arbitration_id, rx.data, rx.is_rx) == (0x123, b"\xde\xad", True)
        assert not rx.is_extended_id

        controller.worker.send(
            can.Message(arbitration_id=0x123, data=b"\xde\xad", is_extended_id=False)
        )
        qtbot.waitUntil(lambda: any(m.startswith("< send") for m in server.got), timeout=3000)
        sent = next(m for m in server.got if m.startswith("< send"))
        assert sent.upper() == "< SEND 123 2 DE AD >"

        # The server never echoes a client's own frame, so the app does: the
        # Trace must show what was sent, tagged TX.
        qtbot.waitUntil(lambda: ring.total_seq >= 2, timeout=3000)
        tx = ring.read_since(0)[0][0]
        assert (tx.arbitration_id, tx.data, tx.is_rx) == (0x123, b"\xde\xad", False)
    finally:
        controller.stop(wait_ms=2000)
        server.close()


def test_socketcand_periodic_send_is_echoed_too(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    server = _StubServer()
    ring = RingBuffer(64)
    controller = BusController(_cfg(port=server.port), ring)
    try:
        with qtbot.waitSignal(controller.started, timeout=5000):
            controller.start()
        qtbot.waitUntil(lambda: ring.total_seq >= 1, timeout=3000)
        task = controller.worker.send_periodic(
            can.Message(arbitration_id=0x200, data=b"\x01", is_extended_id=False), 0.02
        )
        qtbot.waitUntil(lambda: sum(not f.is_rx for f in ring.read_since(-1)[0]) >= 2, timeout=3000)
        controller.worker.stop_periodic(task)
    finally:
        controller.stop(wait_ms=2000)
        server.close()


@pytest.fixture
def _dialog_env(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(prober_mod, "_detect_configs", lambda _backend: [])
    monkeypatch.delenv("ITHILDIN_DEV", raising=False)


def _socketcand_dialog(qtbot) -> CreateCanBusDialog:  # type: ignore[no-untyped-def]
    dlg = CreateCanBusDialog(existing_names=set())
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: dlg._prober is None, timeout=2000)  # probe done
    return dlg


def test_dialog_offers_socketcand_without_probing(qapp, qtbot, _dialog_env) -> None:  # type: ignore[no-untyped-def]
    dlg = _socketcand_dialog(qtbot)
    dev = dlg._current_device()
    assert dev is not None and dev.interface == "socketcand"
    assert [c.channel for c in dev.channels] == ["can0", "can1"]
    assert dlg._baud_combo.isHidden()
    assert not dlg._host_edit.isHidden()


def test_dialog_builds_a_socketcand_config(qapp, qtbot, _dialog_env) -> None:  # type: ignore[no-untyped-def]
    dlg = _socketcand_dialog(qtbot)
    dlg._name_edit.setText("Sim")
    dlg._host_edit.setText("10.0.0.5")
    dlg._port_edit.setText("29540")
    dlg._channel_edit.setText("can1")
    cfg = dlg._build_config()
    assert cfg is not None
    assert (cfg.interface, cfg.host, cfg.port, cfg.channel) == (
        "socketcand",
        "10.0.0.5",
        29540,
        "can1",
    )


def test_dialog_refuses_a_bad_port(qapp, qtbot, _dialog_env) -> None:  # type: ignore[no-untyped-def]
    dlg = _socketcand_dialog(qtbot)
    dlg._name_edit.setText("Sim")
    dlg._port_edit.setText("70000")
    assert not dlg._create_btn.isEnabled()
    assert dlg._build_config() is None
