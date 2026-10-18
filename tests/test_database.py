# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``app_state.database.Database``."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtTest import QSignalSpy

from ithildin.app_state.database import Database
from ithildin.core.decoder import Decoder


def test_add_and_remove_dbc(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    db = Database()
    spy = QSignalSpy(db.database_changed)

    db.add_dbc(example_dbc_path, ["CAN_1"])
    assert spy.count() == 1
    assert example_dbc_path in db.loaded_paths()
    decoder = db.decoder_for_bus("CAN_1")
    assert decoder is not None
    assert decoder.message_name(0x100) == "EngineCommand"
    assert db.buses_for_path(example_dbc_path) == ["CAN_1"]

    db.remove_dbc(example_dbc_path)
    assert spy.count() == 2
    assert db.decoder_for_bus("CAN_1") is None
    assert db.loaded_paths() == []


def test_add_dbc_failure_emits_error(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    db = Database()
    err_spy = QSignalSpy(db.error)
    bad = tmp_path / "broken.dbc"
    bad.write_text("not a dbc", encoding="utf-8")
    db.add_dbc(bad, ["CAN_1"])
    assert err_spy.count() == 1
    assert db.decoder_for_bus("CAN_1") is None


def test_import_without_bind_keeps_dbc_unbound(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Imported DBC appears in loaded_paths but doesn't decode for any bus."""
    db = Database()
    spy = QSignalSpy(db.database_changed)

    assert db.import_dbc(example_dbc_path) is True
    assert spy.count() == 1
    assert example_dbc_path in db.loaded_paths()
    assert db.buses_for_path(example_dbc_path) == []
    assert db.decoder_for_bus("CAN_1") is None


def test_re_import_is_idempotent(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    db = Database()
    db.import_dbc(example_dbc_path)
    spy = QSignalSpy(db.database_changed)

    assert db.import_dbc(example_dbc_path) is True
    assert spy.count() == 0  # no-op, no signal, no error


def test_bind_and_unbind_dbc(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    db = Database()
    db.import_dbc(example_dbc_path)
    spy = QSignalSpy(db.database_changed)

    db.bind_dbc(example_dbc_path, "CAN_1")
    assert spy.count() == 1
    assert db.buses_for_path(example_dbc_path) == ["CAN_1"]
    assert db.decoder_for_bus("CAN_1") is not None

    # Bind to a second bus — additive.
    db.bind_dbc(example_dbc_path, "CAN_2")
    assert spy.count() == 2
    assert db.buses_for_path(example_dbc_path) == ["CAN_1", "CAN_2"]

    # Re-binding the same (path, bus) pair is a no-op.
    db.bind_dbc(example_dbc_path, "CAN_1")
    assert spy.count() == 2

    db.unbind_dbc(example_dbc_path, "CAN_1")
    assert spy.count() == 3
    assert db.buses_for_path(example_dbc_path) == ["CAN_2"]
    assert db.decoder_for_bus("CAN_1") is None
    assert db.decoder_for_bus("CAN_2") is not None

    # Unbinding an absent (path, bus) is a no-op.
    db.unbind_dbc(example_dbc_path, "CAN_1")
    assert spy.count() == 3


def test_bind_dbc_without_import_is_silent_noop(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    db = Database()
    spy = QSignalSpy(db.database_changed)
    db.bind_dbc(example_dbc_path, "CAN_1")
    assert spy.count() == 0
    assert db.buses_for_path(example_dbc_path) == []


def test_remove_drops_bindings_too(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1", "CAN_2"])
    db.remove_dbc(example_dbc_path)
    assert db.loaded_paths() == []
    assert db.buses_for_path(example_dbc_path) == []
    assert db.decoder_for_bus("CAN_1") is None
    assert db.decoder_for_bus("CAN_2") is None


def test_attach_loaded_decoder(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Pre-built Decoder lands in loaded set, fires database_changed once."""
    db = Database()
    spy = QSignalSpy(db.database_changed)
    decoder = Decoder.from_files(example_dbc_path)

    assert db.attach_loaded_decoder(example_dbc_path, decoder) is True
    assert spy.count() == 1
    assert example_dbc_path in db.loaded_paths()
    assert db.decoder_for_path(example_dbc_path) is decoder
    assert db.buses_for_path(example_dbc_path) == []

    # Re-attaching is a no-op — no second signal.
    assert db.attach_loaded_decoder(example_dbc_path, decoder) is True
    assert spy.count() == 1


def test_signal_centric_default_and_toggle(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """New DBCs default to signal-centric; toggle flips and emits change."""
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    assert db.is_signal_centric(example_dbc_path) is True

    spy = QSignalSpy(db.database_changed)
    db.set_signal_centric(example_dbc_path, False)
    assert spy.count() == 1
    assert db.is_signal_centric(example_dbc_path) is False

    # Setting the same value is a no-op.
    db.set_signal_centric(example_dbc_path, False)
    assert spy.count() == 1

    db.set_signal_centric(example_dbc_path, True)
    assert spy.count() == 2
    assert db.is_signal_centric(example_dbc_path) is True


def test_signal_centric_explicit_at_add(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """add_dbc(..., signal_centric=False) honours the override on first import."""
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"], signal_centric=False)
    assert db.is_signal_centric(example_dbc_path) is False


def test_signal_centric_unknown_path_defaults_true(qapp) -> None:  # type: ignore[no-untyped-def]
    """Querying a not-yet-loaded path returns the default (True)."""
    db = Database()
    assert db.is_signal_centric(Path("not_loaded.dbc")) is True


def test_signal_centric_for_message_resolves_via_decoder(
    qapp,
    example_dbc_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    """signal_centric_for_message walks decoders and returns the owning DBC's flag."""
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"], signal_centric=False)
    assert db.signal_centric_for_message("EngineCommand") is False

    db.set_signal_centric(example_dbc_path, True)
    assert db.signal_centric_for_message("EngineCommand") is True

    # Unknown message falls back to True so callers don't special-case startup.
    assert db.signal_centric_for_message("NoSuchMessage") is True


def test_origin_for_message(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """origin_for_message returns sorted buses + the DBC path."""
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_2", "CAN_1"])
    origin = db.origin_for_message("EngineCommand")
    assert origin is not None
    buses, path = origin
    assert buses == ["CAN_1", "CAN_2"]
    assert path == example_dbc_path

    assert db.origin_for_message("NoSuchMessage") is None


def test_remove_dbc_clears_signal_centric(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Removing a DBC drops its signal-centric flag so a re-add starts fresh."""
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"], signal_centric=False)
    db.remove_dbc(example_dbc_path)

    # Re-add without specifying — should take the default (True) again,
    # not silently inherit the prior False.
    db.add_dbc(example_dbc_path, ["CAN_1"])
    assert db.is_signal_centric(example_dbc_path) is True


def test_re_add_preserves_signal_centric(qapp, example_dbc_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Re-adding an already-loaded DBC keeps the user's toggle intact."""
    db = Database()
    db.add_dbc(example_dbc_path, ["CAN_1"])
    db.set_signal_centric(example_dbc_path, False)

    # An idempotent re-add (e.g. project re-bind) shouldn't clobber the toggle.
    db.add_dbc(example_dbc_path, ["CAN_2"], signal_centric=True)
    assert db.is_signal_centric(example_dbc_path) is False
