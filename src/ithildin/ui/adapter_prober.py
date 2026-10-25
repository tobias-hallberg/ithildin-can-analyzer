# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Background-thread CAN adapter probe.

python-can's ``can.detect_available_configs()`` imports each backend
module at probe time. Without an ``interfaces=`` filter it imports
every backend it ships with — which is wasted work on the GUI thread
at startup, since Ithildin only supports a small subset.

This module probes the supported backends one at a time on a worker
``QThread``, streaming each enriched device (``device_found``) and its
derived channel rows (``adapter_found``) to the UI as Qt signals. The
Create CAN bus dialog runs its own instance of this prober on open and
on rescan; the left dock runs one at startup to mark declared buses
available.

The no-hardware virtual loopback is emitted last, but only in dev mode
(``ITHILDIN_DEV`` / ``--dev``) — normal users never see it. See
:func:`dev_mode`.

Per-backend probing (one ``detect_available_configs(interfaces=[name])``
call per backend) lets adapters appear in the dock progressively rather
than as a single batch at the end. python-can can't enumerate ``gs_usb``
adapters, so :func:`_detect_gs_usb` scans for them itself. Each
per-backend call is wrapped in its own thread with a hard timeout: a
misbehaving vendor module (PCAN without the driver, Kvaser with stale
CANlib) must not be able to wedge the dock indefinitely.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication

from ithildin.ui.adapter_enrich import ChannelInfo, DeviceInfo, enrich
from ithildin.workspace.schema import SupportedInterface

_log = logging.getLogger(__name__)

_SUPPORTED_BACKENDS: tuple[str, ...] = (
    "pcan",
    "kvaser",
    "gs_usb",
    "socketcan",
    "ixxat",
)
"""Backends the prober actively probes. Kept narrow so we don't import
vendor modules we don't intend to support."""

BACKEND_TIMEOUT_S = 3.0
"""Hard ceiling per backend probe. A hung vendor import past this is
reported via ``backend_failed`` and the worker thread carries on; the
hung helper thread is a daemon, left to die when the process exits."""


@dataclass(frozen=True)
class AdapterChoice:
    """One channel row in the adapter list (left dock + dialog).

    ``device`` back-references the enriched :class:`DeviceInfo` this
    channel belongs to. It defaults to ``None`` so hand-built choices
    (and the legacy ``VIRTUAL_LOOPBACK`` constant) keep comparing equal
    to older code paths that never set it.
    """

    label: str
    interface: SupportedInterface
    channel: str | int
    device: DeviceInfo | None = None


VIRTUAL_LOOPBACK = AdapterChoice(
    label="Ithildin Virtual loopback",
    interface="virtual",
    channel="ithildin_virtual",
)
"""No-hardware loopback adapter. Surfaced only in dev mode (see
:func:`dev_mode`); appended last in every probe when it is on."""


def dev_mode() -> bool:
    """Whether dev-only surfaces (currently the virtual loopback adapter)
    are exposed in the UI.

    The loopback exists for development, demos, and the test suite — it
    is not something a normal user should connect to — so it is hidden
    unless ``ITHILDIN_DEV`` is set in the environment (the ``--dev``
    launch flag sets it). Read live rather than cached so tests can flip
    it per case via ``monkeypatch.setenv``.
    """
    return bool(os.environ.get("ITHILDIN_DEV"))


def virtual_device() -> DeviceInfo:
    """Synthetic two-channel loopback device for the dialog's pill row.

    The first channel reuses ``VIRTUAL_LOOPBACK``'s channel value so a
    bus declared the old way still resolves against it.
    """
    return DeviceInfo(
        interface="virtual",
        vendor="Ithildin",
        model="Virtual loopback",
        transport="Software",
        channels=(
            ChannelInfo(channel="ithildin_virtual", label="vcan0"),
            ChannelInfo(channel="ithildin_virtual_1", label="vcan1"),
        ),
    )


class AdapterProber(QObject):
    """Probe each supported python-can backend on a worker thread.

    Lifecycle: create on main thread, ``moveToThread`` onto a fresh
    ``QThread``, connect ``thread.started`` to ``run``. ``run`` returns
    when all backends are done; the ``finished`` signal is the cue for
    the owner to ``thread.quit()`` and dispose.
    """

    adapter_found = Signal(AdapterChoice)
    device_found = Signal(DeviceInfo)
    backend_done = Signal(str)
    backend_failed = Signal(str, str)  # (backend_name, reason)
    finished = Signal()

    def __init__(
        self,
        timeout_s: float = BACKEND_TIMEOUT_S,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._timeout_s = timeout_s
        self._cancelled = False

    @Slot()
    def cancel(self) -> None:
        """Cross-thread safe stop request — just flips a flag.

        ``run`` checks it between backends, so a cancelled probe exits
        within one backend's timeout rather than wedging shutdown. Used
        when the owning dialog closes or the app quits while a probe is
        still in flight.
        """
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        seen: set[tuple[str, object]] = set()
        for backend in _SUPPORTED_BACKENDS:
            if self._cancelled:
                break
            for device in self._probe_with_timeout(backend):
                self._emit_device(device, seen)
            self.backend_done.emit(backend)
        if not self._cancelled and dev_mode():
            self._emit_device(virtual_device(), seen)
        # Always emit finished so the owner's quit/deleteLater chain runs
        # and the thread tears down cleanly, cancelled or not.
        self.finished.emit()

    def _emit_device(self, device: DeviceInfo, seen: set[tuple[str, object]]) -> None:
        """Emit a device plus a derived ``adapter_found`` row per channel."""
        self.device_found.emit(device)
        for ch in device.channels:
            key = (device.interface, ch.channel)
            if key in seen:
                continue
            seen.add(key)
            self.adapter_found.emit(
                AdapterChoice(
                    label=f"{device.interface} · {ch.channel}",
                    interface=device.interface,
                    channel=ch.channel,
                    device=device,
                )
            )

    def _probe_with_timeout(self, backend: str) -> list[DeviceInfo]:
        """Run one enriched probe with a hard timeout.

        On a daemon thread: Python can't kill a hung vendor call, so on
        timeout we stop waiting and move on, and the abandoned thread can't
        hold up interpreter exit the way a joined pool worker would. The
        enrichment runs inside the thread so any backend-specific deepen is
        covered by the same timeout.
        """
        outcome: list[list[DeviceInfo] | BaseException] = []

        def probe() -> None:
            try:
                outcome.append(_detect_and_enrich(backend))
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=probe, name=f"probe-{backend}", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            # Interpreter is shutting down ("can't create new thread at
            # interpreter shutdown"). Bail quietly.
            return []
        thread.join(self._timeout_s)
        if not outcome:
            self.backend_failed.emit(backend, "timeout")
            return []
        result = outcome[0]
        if isinstance(result, BaseException):
            _log.warning("Probe of %s failed", backend, exc_info=result)
            self.backend_failed.emit(backend, str(result))
            return []
        return result


# --------------------------------------------------------------------------
# In-flight registry. A prober runs on a worker QThread that may outlive its
# owner (a transient dialog, or the app at quit). We keep a strong reference
# to every running (prober, thread) until the thread actually finishes, and
# install an aboutToQuit hook that cancels + joins them — otherwise a probe
# still running at teardown crashes ("QThread: Destroyed while thread is
# still running" / "cannot schedule new futures after interpreter shutdown").
# --------------------------------------------------------------------------
_INFLIGHT_PROBES: set[tuple[AdapterProber, QThread]] = set()
_quit_guard_installed = False


def register_prober(prober: AdapterProber, thread: QThread) -> None:
    """Retain a running probe until its thread ends; arm the quit guard.

    Call right after ``thread.start()``. The owner still wires its own
    ``finished`` handlers; this only guarantees the objects survive long
    enough to tear down cleanly regardless of the owner's lifetime.
    """
    global _quit_guard_installed
    pair = (prober, thread)
    _INFLIGHT_PROBES.add(pair)
    thread.finished.connect(lambda: _INFLIGHT_PROBES.discard(pair))
    app = QApplication.instance()
    if app is not None and not _quit_guard_installed:
        app.aboutToQuit.connect(_stop_all_probes)
        _quit_guard_installed = True


def _stop_all_probes() -> None:
    """Cancel and join every in-flight probe before the app exits."""
    pairs = list(_INFLIGHT_PROBES)
    for prober, _thread in pairs:
        prober.cancel()
    for _prober, thread in pairs:
        thread.quit()
        thread.wait(5000)


def _detect_and_enrich(backend: str) -> list[DeviceInfo]:
    """Target for the per-backend probe thread.

    Runs the raw probe and the enrichment together so both are bounded
    by ``_probe_with_timeout``'s ceiling.
    """
    return enrich(backend, _detect_configs(backend))


def _detect_configs(backend: str) -> list[dict[str, object]]:
    """Raw ``detect_available_configs`` call for one backend.

    Module-level so tests can patch it, and so the eager ``import can``
    happens off whoever's calling — the first import of python-can
    pulls in the full backend registry and can take 100+ ms even on a
    warm machine.
    """
    if backend == "gs_usb":
        # On Linux the kernel's gs_usb driver owns the adapter, which shows
        # up as SocketCAN; pyusb would detach it.
        return _detect_gs_usb() if sys.platform == "win32" else []

    import can

    # python-can types this as ``Sequence[AutoDetectedConfig]`` (a
    # TypedDict). For our purposes — passing dicts up to the UI to
    # render — they're indistinguishable from plain mappings.
    configs = [dict(cfg) for cfg in can.detect_available_configs(interfaces=[backend])]
    if backend == "kvaser":
        _add_kvaser_firmware(configs)
    return configs


def _detect_gs_usb() -> list[dict[str, object]]:
    """One config per connected gs_usb adapter, by its ``GsUsb.scan()`` index.

    Reads no string descriptors: that opens the device, which WinUSB grants
    to one handle at a time, so it would race a bus opening on the adapter.
    ``GsUsbBus`` opens the adapter at the same index of the same scan.
    """
    import libusb_package
    from gs_usb.gs_usb import GsUsb

    libusb_package.get_libusb1_backend()
    return [{"interface": "gs_usb", "channel": index} for index in range(len(GsUsb.scan()))]


def _add_kvaser_firmware(configs: list[dict[str, object]]) -> None:
    """Fold the CANlib firmware revision into each kvaser config dict.

    ``detect_available_configs`` gives name/serial/dongle_channel but not
    firmware; one extra passive ``canGetChannelData`` query per channel
    adds it (no channel open, so it's cheap and side-effect free). On any
    CANlib hiccup the ``"firmware"`` key is simply absent — the enricher
    then reports firmware as ``None``, same as before this deepen.

    The revision is returned as four 16-bit words. CANlib packs it
    most-significant word first as ``major.minor.build.release``; that
    ordering is not documented by python-can, so confirm against a real
    Kvaser. A wrong guess only mislabels the string — it can't break the
    probe.
    """
    try:
        import ctypes

        from can.interfaces.kvaser import canlib, constants
    except Exception:
        return
    for cfg in configs:
        channel = cfg.get("channel")
        if not isinstance(channel, int):
            continue
        try:
            buf = (ctypes.c_ushort * 4)()
            canlib.canGetChannelData(
                channel,
                constants.canCHANNELDATA_CARD_FIRMWARE_REV,
                ctypes.byref(buf),
                ctypes.sizeof(buf),
            )
        except Exception:
            continue
        words = tuple(buf)
        if any(words):
            cfg["firmware"] = ".".join(str(w) for w in reversed(words))
