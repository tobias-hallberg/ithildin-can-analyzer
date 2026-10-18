# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Smoke + round-trip tests for the per-signal TX controls."""

from __future__ import annotations

from ithildin.core.transmit import TxSignalSpec
from ithildin.ui.transmit.controls import (
    TxAutoBadge,
    TxDropdown,
    TxHexGrid,
    TxSegmented,
    TxSignedSlider,
    TxSlider,
    TxStepper,
    TxToggle,
    control_for_signal,
)


def _spec(**kwargs) -> TxSignalSpec:  # type: ignore[no-untyped-def]
    defaults = {
        "name": "Sig",
        "kind": "num-sm",
        "value": 0,
        "length_bits": 8,
        "is_signed": False,
        "minimum": 0.0,
        "maximum": 100.0,
        "scale": 1.0,
        "offset": 0.0,
        "unit": "",
        "choices": None,
        "initial": None,
    }
    defaults.update(kwargs)
    return TxSignalSpec(**defaults)


def test_toggle_round_trip(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxToggle(value=False)
    qtbot.addWidget(w)
    captured: list[bool] = []
    w.value_changed.connect(captured.append)
    w.set_value(True)
    assert w.value is True
    w.set_value(False)
    assert w.value is False
    # set_value does not emit; click does (we test via setChecked which the
    # QAbstractButton fires for toggled→value_changed).
    w.setChecked(True)
    assert captured[-1] is True


def test_segmented_round_trip(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxSegmented({0: "PARK", 1: "REVERSE", 2: "NEUTRAL", 3: "DRIVE"}, value=0)
    qtbot.addWidget(w)
    captured: list[int] = []
    w.value_changed.connect(captured.append)
    w.set_value(2)
    assert w.value == 2
    # Programmatic set_value must not emit.
    assert captured == []


def test_dropdown_round_trip(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxDropdown({i: f"opt-{i}" for i in range(8)}, value=3)
    qtbot.addWidget(w)
    assert w.value == 3
    w.set_value(5)
    assert w.value == 5


def test_slider_emits_on_change(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxSlider(0.0, 100.0, scale=1.0, unit="%", value=20.0)
    qtbot.addWidget(w)
    captured: list[float] = []
    w.value_changed.connect(captured.append)
    w._slider.setValue(40)
    assert captured and abs(captured[-1] - 40.0) < 1e-6


def test_stepper_clamps_to_range(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxStepper(0.0, 10.0, scale=1.0, unit="", value=5.0)
    qtbot.addWidget(w)
    w._bump(100.0)
    assert w.value == 10.0
    w._bump(-50.0)
    assert w.value == 0.0


def test_signed_slider_centres_on_zero(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxSignedSlider(-300.0, 300.0, scale=1.0, unit="Nm", value=0.0)
    qtbot.addWidget(w)
    w.set_value(150.0)
    assert abs(w.value - 150.0) < 1.0
    w.set_value(-200.0)
    assert abs(w.value + 200.0) < 1.0


def test_hex_grid_round_trip(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxHexGrid("DE AD BE EF 00 00 00 00")
    qtbot.addWidget(w)
    assert w.value.startswith("DE AD BE EF")
    w.set_value("01 02 03 04 05 06 07 08")
    assert w.value == "01 02 03 04 05 06 07 08"


def test_auto_badge_renders(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxAutoBadge(7, hex_format=False, maximum=15)
    qtbot.addWidget(w)
    assert "7" in w._readout.text()


def test_dispatcher_chooses_correct_widget(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    captured: list[object] = []
    spec = _spec(kind="bool", value=True)
    w = control_for_signal(spec, on_change=captured.append)
    qtbot.addWidget(w)
    assert isinstance(w, TxToggle)

    spec = _spec(kind="enum-seg", value=1, choices={0: "A", 1: "B"})
    w = control_for_signal(spec, on_change=captured.append)
    qtbot.addWidget(w)
    assert isinstance(w, TxSegmented)

    spec = _spec(kind="signed", value=0, minimum=-100.0, maximum=100.0)
    w = control_for_signal(spec, on_change=captured.append)
    qtbot.addWidget(w)
    assert isinstance(w, TxSignedSlider)

    spec = _spec(kind="hex", value="00 00 00 00 00 00 00 00")
    w = control_for_signal(spec, on_change=captured.append)
    qtbot.addWidget(w)
    assert isinstance(w, TxHexGrid)
