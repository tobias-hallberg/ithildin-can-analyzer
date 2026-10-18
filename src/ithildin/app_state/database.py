# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Application-wide DBC database.

Tracks two things, separately:

- ``_loaded`` — every DBC the user has imported, keyed by path. A DBC
  here may have zero bus bindings; it just sits idle until bound.
- ``_bindings`` — for each loaded DBC, the set of bus names it applies
  to. Decoders are looked up by bus name from this map.

Splitting "loaded" from "bound" lets the UI introduce a DBC into the
project without committing to a bus, then bind it (or rebind it) by
drag-drop. Emits ``database_changed()`` after every public mutation
so dock and views refresh.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ithildin.core.decoder import Decoder


class Database(QObject):
    database_changed = Signal()
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loaded: dict[Path, Decoder] = {}
        self._bindings: dict[Path, set[str]] = {}
        self._signal_centric: dict[Path, bool] = {}

    # ---- Import / remove ----

    def import_dbc(self, path: Path | str) -> bool:
        """Load a DBC file into the project's loaded set, no bus binding.

        Re-importing an already-loaded path is a no-op (no signal,
        no error). Returns ``True`` if the DBC was loaded successfully
        or was already loaded; ``False`` on parse failure.
        """
        p = Path(path)
        if p in self._loaded:
            return True
        try:
            decoder = Decoder.from_files(p)
        except Exception as exc:
            self.error.emit(f"Failed to load DBC {p!s}: {exc}")
            return False
        return self.attach_loaded_decoder(p, decoder)

    def attach_loaded_decoder(self, path: Path | str, decoder: Decoder) -> bool:
        """Insert a pre-built ``Decoder`` for ``path`` into the loaded set.

        Lets the caller parse off the GUI thread (see
        ``ui/dbc_loader.py``) and hand the result back here without
        re-doing the work. Re-attaching an already-loaded path is a
        no-op (no signal). Returns ``True`` if attached or already
        present.
        """
        p = Path(path)
        if p in self._loaded:
            return True
        self._loaded[p] = decoder
        self._bindings.setdefault(p, set())
        self.database_changed.emit()
        return True

    def remove_dbc(self, path: Path | str) -> None:
        """Remove a DBC entirely — drops both the loaded entry and any bindings."""
        p = Path(path)
        loaded = self._loaded.pop(p, None)
        bound = self._bindings.pop(p, None)
        self._signal_centric.pop(p, None)
        if loaded is not None or bound is not None:
            self.database_changed.emit()

    # ---- Bind / unbind ----

    def bind_dbc(self, path: Path | str, bus: str) -> None:
        """Bind a loaded DBC to ``bus``. No-op if already bound to it.

        Silently ignored when the DBC has not been imported. Callers
        should ``import_dbc`` first; ``add_dbc`` does both in one step.
        """
        p = Path(path)
        if p not in self._loaded:
            return
        bindings = self._bindings.setdefault(p, set())
        if bus in bindings:
            return
        bindings.add(bus)
        self.database_changed.emit()

    def unbind_dbc(self, path: Path | str, bus: str) -> None:
        """Remove the (path, bus) binding. No-op if absent."""
        p = Path(path)
        bindings = self._bindings.get(p)
        if not bindings or bus not in bindings:
            return
        bindings.discard(bus)
        self.database_changed.emit()

    # ---- Combined: import + bind in one step (project load uses this) ----

    def add_dbc(
        self,
        path: Path | str,
        buses: list[str],
        *,
        signal_centric: bool = True,
    ) -> None:
        """Import then bind to each name in ``buses``.

        Emits ``database_changed`` once at the end, not once per bind,
        so the UI doesn't see partial state during a multi-bind import.
        ``signal_centric`` applies on first import only — re-adding an
        already-loaded DBC keeps whatever flag was previously set so a
        user's toggle isn't clobbered by an idempotent re-load.
        """
        p = Path(path)
        first_import = p not in self._loaded
        if first_import:
            try:
                decoder = Decoder.from_files(p)
            except Exception as exc:
                self.error.emit(f"Failed to load DBC {p!s}: {exc}")
                return
            self._loaded[p] = decoder
            self._signal_centric[p] = signal_centric
        bindings = self._bindings.setdefault(p, set())
        bindings.update(buses)
        self.database_changed.emit()

    # ---- Signal-centric display mode (per DBC) ----

    def set_signal_centric(self, path: Path | str, value: bool) -> None:
        """Flip the signal-centric flag for a loaded DBC. Emits change."""
        p = Path(path)
        if p not in self._loaded:
            return
        if self._signal_centric.get(p, True) == value:
            return
        self._signal_centric[p] = value
        self.database_changed.emit()

    def is_signal_centric(self, path: Path | str) -> bool:
        """Read the signal-centric flag for ``path`` (default True)."""
        return self._signal_centric.get(Path(path), True)

    def signal_centric_for_message(self, message_name: str) -> bool:
        """Resolve ``message_name`` to its DBC and return its flag.

        Returns the default (True) when the message isn't known yet —
        keeps callers from having to special-case the pre-load window.
        """
        for p, decoder in self._loaded.items():
            try:
                decoder.database.get_message_by_name(message_name)
            except KeyError:
                continue
            return self._signal_centric.get(p, True)
        return True

    def origin_for_message(self, message_name: str) -> tuple[list[str], Path] | None:
        """Return ``(bus_names, dbc_path)`` for tooltips. ``None`` if unknown.

        Bus names are sorted; ``dbc_path`` is the full path on disk
        (callers typically render only its ``.name``).
        """
        for p, decoder in self._loaded.items():
            try:
                decoder.database.get_message_by_name(message_name)
            except KeyError:
                continue
            return sorted(self._bindings.get(p, set())), p
        return None

    # ---- Read-only views ----

    def decoders_for_bus(self, bus_name: str) -> list[Decoder]:
        return [
            self._loaded[p]
            for p, buses in self._bindings.items()
            if bus_name in buses and p in self._loaded
        ]

    def decoder_for_bus(self, bus_name: str) -> Decoder | None:
        decs = self.decoders_for_bus(bus_name)
        return decs[0] if decs else None

    def decoder_for_path(self, path: Path | str) -> Decoder | None:
        """Return the loaded ``Decoder`` for a path regardless of binding."""
        return self._loaded.get(Path(path))

    def decoder_for_message(self, message_name: str) -> Decoder | None:
        """Return any loaded decoder whose database contains ``message_name``.

        Useful when the caller cannot resolve a message via a bus name —
        e.g. before a capture has started, where ``decoder_for_bus``
        would otherwise need a live bus to look up. First match wins;
        when multiple DBCs define the same message name the binding
        order in ``_loaded`` decides, which is fine for the pre-capture
        seeding case (post-capture, the bus-scoped lookup takes over).
        """
        for decoder in self._loaded.values():
            try:
                decoder.database.get_message_by_name(message_name)
            except KeyError:
                continue
            return decoder
        return None

    def loaded_paths(self) -> list[Path]:
        return list(self._loaded.keys())

    def buses_for_path(self, path: Path | str) -> list[str]:
        target = Path(path)
        return sorted(self._bindings.get(target, set()))
