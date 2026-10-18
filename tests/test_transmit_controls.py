# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Smoke + round-trip tests for the per-signal TX controls."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics

from ithildin.core.transmit import TxSignalSpec
from ithildin.ui.fonts import tabular_font
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
from ithildin.ui.transmit.list_delegate import fit_name_and_id
from ithildin.ui.transmit_window import _type_badge


def _spec(**kwargs) -> TxSignalSpec:  # type: ignore[no-untyped-def]
    defaults = {
        "name": "Sig",
        "kind": "num-sm",
        "value": 0,
        "length_bits": 8,
        "is_signed": False,
        "is_float": False,
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
    captured: list[float] = []
    w.value_changed.connect(captured.append)
    w._field.stepBy(100)
    assert w.value == 10.0
    w._field.stepBy(-50)
    assert w.value == 0.0
    assert captured == [10.0, 0.0]
    assert w._plus.autoRepeat() and w._minus.autoRepeat()


def _type_into(qtbot, w: TxStepper, text: str) -> None:  # type: ignore[no-untyped-def]
    line = w._field.lineEdit()
    line.selectAll()
    qtbot.keyClicks(line, text)
    qtbot.keyClick(line, Qt.Key.Key_Return)


def test_stepper_accepts_a_typed_value(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """Regression: a 0 … 1023 signal could only be changed one click at a time."""
    w = TxStepper(0.0, 1023.0, scale=1.0, unit="m", value=0.0)
    qtbot.addWidget(w)
    captured: list[float] = []
    w.value_changed.connect(captured.append)
    _type_into(qtbot, w, "512")
    assert captured == [512.0]
    assert w.value == 512.0
    assert w._field.text() == "512 m"


def test_stepper_snaps_typed_value_to_the_scale(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxStepper(0.0, 1000.0, scale=0.25, unit="", value=0.0)
    qtbot.addWidget(w)
    _type_into(qtbot, w, "1.3")
    assert w.value == 1.25
    assert w._field.text() == "1.25"


def test_list_name_is_elided_to_keep_clear_of_the_badge(qapp) -> None:  # type: ignore[no-untyped-def]
    """Regression: long names with 29-bit IDs ran under the "once" badge."""
    fm = QFontMetrics(tabular_font())
    full, ident = "BCU_IMD_Ctrl_Long_Name", "0xA100101"
    id_w = fm.horizontalAdvance(ident) + 8
    roomy = fm.horizontalAdvance(full) + id_w
    assert fit_name_and_id(fm, full, ident, roomy) == (full, True)

    tight = id_w + fm.horizontalAdvance("BCU_IMD…")
    name, show_id = fit_name_and_id(fm, full, ident, tight)
    assert show_id
    assert name.endswith("…")
    assert fm.horizontalAdvance(name) + id_w <= tight

    # Too narrow for any name beside the ID: the name keeps the line.
    name, show_id = fit_name_and_id(fm, full, ident, id_w)
    assert not show_id
    assert fm.horizontalAdvance(name) <= id_w


def test_dropdown_shows_blank_for_value_outside_its_table(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxDropdown({98: "a", 193: "b", 240: "c"}, value=0)
    qtbot.addWidget(w)
    assert w.currentIndex() == -1
    captured: list[int] = []
    w.value_changed.connect(captured.append)
    w.setCurrentIndex(0)
    assert captured == [98]


def test_signed_slider_centres_on_zero(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxSignedSlider(-300.0, 300.0, scale=1.0, unit="Nm", value=0.0)
    qtbot.addWidget(w)
    w.set_value(150.0)
    assert abs(w.value - 150.0) < 1.0
    w.set_value(-200.0)
    assert abs(w.value + 200.0) < 1.0


def test_signed_slider_stops_at_the_encodable_maximum(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    # A signed 8-bit signal spans −128 … 127; the bar is drawn ±128 around zero.
    w = TxSignedSlider(-128.0, 127.0, scale=1.0, unit="", value=0.0)
    qtbot.addWidget(w)
    w.set_value(500.0)
    assert w.value == 127.0
    w.set_value(-500.0)
    assert w.value == -128.0


def test_slider_stays_inside_range_when_offset_is_off_the_scale_grid(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    # 0.25 / 0.5 rounds to 0, which would put the slider's floor below 0.25.
    w = TxSlider(0.25, 10.25, scale=0.5, unit="", value=1.0)
    qtbot.addWidget(w)
    w.set_value(-100.0)
    assert w.value == 0.5
    w.set_value(100.0)
    assert w.value == 10.0


def test_hex_grid_holds_a_64_bit_value_exactly(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxHexGrid(0x0123456789ABCDEF, 64)
    qtbot.addWidget(w)
    assert [c.text() for c in w._cells] == ["01", "23", "45", "67", "89", "AB", "CD", "EF"]
    assert w.value == 0x0123456789ABCDEF
    captured: list[int] = []
    w.value_changed.connect(captured.append)
    w._cells[0].setText("f")
    w._cells[0].editingFinished.emit()
    assert captured == [0x0F23456789ABCDEF]
    assert w._cells[0].text() == "0F"
    # Leaving a cell without changing it doesn't re-send.
    w._cells[1].editingFinished.emit()
    assert captured == [0x0F23456789ABCDEF]


def test_hex_grid_sizes_to_the_signal_and_reads_signed_values(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    w = TxHexGrid(-2, 12, is_signed=True)
    qtbot.addWidget(w)
    assert [c.text() for c in w._cells] == ["0F", "FE"]
    assert w.value == -2
    # Bits past the signal's width are dropped.
    w._cells[0].setText("FF")
    w._cells[0].editingFinished.emit()
    assert w.value == -2
    assert w._cells[0].text() == "0F"


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

    spec = _spec(kind="hex", value=2**64 - 1, length_bits=64)
    w = control_for_signal(spec, on_change=captured.append)
    qtbot.addWidget(w)
    assert isinstance(w, TxHexGrid)
    assert w.value == 2**64 - 1


def test_hex_control_round_trips_whole_number_scaling_exactly(qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
    """The grid shows raw bits; the value handed on is the exact physical int."""
    captured: list[int] = []
    physical = 0x0123456789ABCDEF * 10 - 1000
    spec = _spec(kind="hex", value=physical, length_bits=64, scale=10.0, offset=-1000.0)
    w = control_for_signal(spec, on_change=captured.append)
    qtbot.addWidget(w)
    assert isinstance(w, TxHexGrid)
    assert w.value == 0x0123456789ABCDEF
    w._cells[7].setText("EE")
    w._cells[7].editingFinished.emit()
    assert captured == [0x0123456789ABCDEE * 10 - 1000]


def test_type_badge_shows_the_real_encoding() -> None:
    """Regression: numeric badges were fixed per control kind ("u16" for any wide range)."""
    assert _type_badge(_spec(kind="num-lg", length_bits=10)) == "u10"
    assert _type_badge(_spec(kind="signed", length_bits=8, is_signed=True)) == "i8"
    assert _type_badge(_spec(kind="hex", length_bits=64)) == "u64"
    assert _type_badge(_spec(kind="num-lg", length_bits=32, is_float=True)) == "f32"
    assert _type_badge(_spec(kind="bool", length_bits=1)) == "bool"
    assert _type_badge(_spec(kind="enum-dd", choices={1: "a"})) == "enum"
