# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Background-thread CAN adapter probe.

Detecting a python-can backend's adapters imports its vendor module, and
python-can ships many more backends than Ithildin supports — work that
must stay off the GUI thread.

This module probes the supported backends one at a time on a worker
``QThread``, streaming each enriched device (``device_found``) and its
derived channel rows (``adapter_found``) to the UI as Qt signals. The
Create CAN bus dialog runs its own instance of this prober on open and
on rescan; the left dock runs one at startup and on every rescan (a
device change on Windows, or its ↻) to mark declared buses available.

The no-hardware virtual loopback is emitted last, but only in dev mode
(``ITHILDIN_DEV`` / ``--dev``) — normal users never see it. See
:func:`dev_mode`.

Per-backend probing (one ``core.bus.detect_configs`` call per backend)
lets adapters appear in the dock progressively rather than as a single
batch at the end. python-can can't enumerate ``gs_usb`` adapters, so
:func:`_detect_gs_usb` scans for them itself. Each per-backend call is
wrapped in its own thread with a hard timeout: a misbehaving vendor module
(PCAN without the driver, Kvaser with stale CANlib) must not be able to
wedge the dock indefinitely.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
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
"""Hard ceiling per backend probe. A vendor import or detection hung past
this is reported via ``backend_failed`` and the worker thread carries on;
the hung helper thread is a daemon, left to die when the process exits.
Below the 5 s ``_stop_all_probes`` gives a probe at quit, which can't
cancel one mid-backend."""

_PROBE_LOCK_WAIT_S = 2.0
"""Below :data:`BACKEND_TIMEOUT_S`: a Kvaser or gs_usb probe queued behind a
slow or hung open reports the driver busy and its thread ends, instead of
timing out with the thread parked on ``KVASER_ENUM_LOCK`` / ``GS_USB_LOCK``."""


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
    hw_id: str | None = None


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
        *,
        skip_backends: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(parent)
        self._timeout_s = timeout_s
        # Backends an earlier probe timed out on: their helper thread is
        # still hung, and probing again would only hang another.
        self._skip_backends = skip_backends
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
        seen: set[tuple[str, str | None, str | int]] = set()
        for backend in _SUPPORTED_BACKENDS:
            if self._cancelled:
                break
            if backend in self._skip_backends:
                continue
            for device in self._probe_with_timeout(backend):
                self._emit_device(device, seen)
            self.backend_done.emit(backend)
        if not self._cancelled and dev_mode():
            self._emit_device(virtual_device(), seen)
        # Always emit finished so the owner's quit/deleteLater chain runs
        # and the thread tears down cleanly, cancelled or not.
        self.finished.emit()

    def _emit_device(
        self, device: DeviceInfo, seen: set[tuple[str, str | None, str | int]]
    ) -> None:
        """Emit a device plus a derived ``adapter_found`` row per channel."""
        self.device_found.emit(device)
        for ch in device.channels:
            # With the id: two IXXAT devices both have a channel 0.
            key = (device.interface, ch.hw_id, ch.channel)
            if key in seen:
                continue
            seen.add(key)
            self.adapter_found.emit(
                AdapterChoice(
                    label=f"{device.interface} · {ch.channel}",
                    interface=device.interface,
                    channel=ch.channel,
                    device=device,
                    hw_id=ch.hw_id,
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
    """Retain a probe until its thread ends; arm the quit guard.

    Call right before ``thread.start()``: a probe with little to do can
    finish before a later call connects to ``finished``, and would then be
    held, deleted, for good. The owner still wires its own ``finished``
    handlers; this only guarantees the objects survive long enough to tear
    down cleanly regardless of the owner's lifetime.
    """
    global _quit_guard_installed
    pair = (prober, thread)
    _INFLIGHT_PROBES.add(pair)
    # Direct, on the finishing thread: queued, the call was at times never
    # delivered, as the same signal has the thread deleted.
    thread.finished.connect(
        lambda: _INFLIGHT_PROBES.discard(pair), Qt.ConnectionType.DirectConnection
    )
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
    """Raw detection dicts for one backend.

    Module-level so tests can patch it, and so the eager ``import can``
    happens off whoever's calling — the first import of python-can
    pulls in the full backend registry and can take 100+ ms even on a
    warm machine.
    """
    if backend == "gs_usb":
        # On Linux the kernel's gs_usb driver owns the adapter, which shows
        # up as SocketCAN; pyusb would detach it.
        return _detect_gs_usb() if sys.platform == "win32" else []

    from ithildin.core.bus import KVASER_ENUM_LOCK, detect_configs, kvaser_enum_hardware

    if backend != "kvaser":
        return detect_configs(backend)

    if not KVASER_ENUM_LOCK.acquire(timeout=_PROBE_LOCK_WAIT_S):
        raise RuntimeError("Kvaser driver busy: a Kvaser bus or earlier scan is still in it")
    try:
        kvaser_enum_hardware()
        configs = detect_configs(backend)
        _add_kvaser_channel_data(configs)
    finally:
        KVASER_ENUM_LOCK.release()
    return configs


def _detect_gs_usb() -> list[dict[str, object]]:
    """One config per connected gs_usb adapter: its ``GsUsb.scan()`` index and USB serial.

    The index follows plug order, so ``GsUsbBus`` opens a pinned bus by the
    serial. Reading it opens the device, which WinUSB grants to one handle
    at a time: the reads hold ``GS_USB_LOCK`` so no bus opens meanwhile, the
    handle is released at once, and an adapter a bus holds open reports no
    serial.
    """
    import libusb_package
    import usb.util
    from gs_usb.gs_usb import GsUsb

    from ithildin.core.gs_usb_bus import GS_USB_LOCK, SERIAL_READ_ERRORS

    libusb_package.get_libusb1_backend()
    if not GS_USB_LOCK.acquire(timeout=_PROBE_LOCK_WAIT_S):
        raise RuntimeError("gs_usb driver busy: a gs_usb bus is still opening")
    try:
        configs: list[dict[str, object]] = []
        for index, dev in enumerate(GsUsb.scan()):
            serial: str | None = None
            with contextlib.suppress(*SERIAL_READ_ERRORS):
                serial = dev.serial_number
            usb.util.dispose_resources(dev.gs_usb)
            configs.append({"interface": "gs_usb", "channel": index, "serial": serial})
    finally:
        GS_USB_LOCK.release()
    return configs


def _add_kvaser_channel_data(configs: list[dict[str, object]]) -> None:
    """Fold the CANlib card type and firmware revision into each kvaser
    config dict.

    ``detect_available_configs`` gives name/serial/dongle_channel but not
    these; two extra passive ``canGetChannelData`` queries per channel
    add them (no channel open, so it's cheap and side-effect free). The
    enricher hides Kvaser Virtual channels by ``"card_type"``. On any
    CANlib hiccup both keys are simply absent — the enricher then keeps
    the channel and reports firmware as ``None``.

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
        card_type = ctypes.c_uint()
        firmware = (ctypes.c_ushort * 4)()
        try:
            canlib.canGetChannelData(
                channel,
                constants.canCHANNELDATA_CARD_TYPE,
                ctypes.byref(card_type),
                ctypes.sizeof(card_type),
            )
            canlib.canGetChannelData(
                channel,
                constants.canCHANNELDATA_CARD_FIRMWARE_REV,
                ctypes.byref(firmware),
                ctypes.sizeof(firmware),
            )
        except Exception:
            continue
        cfg["card_type"] = card_type.value
        words = tuple(firmware)
        if any(words):
            cfg["firmware"] = ".".join(str(w) for w in reversed(words))
