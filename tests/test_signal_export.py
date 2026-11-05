# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``core.signal_export``."""

from __future__ import annotations

import csv
import math
from pathlib import Path

from ithildin.core.signal_export import (
    COMMA,
    SEMICOLON,
    ExportColumn,
    format_value,
    write_signal_csv,
)


def _read(path: Path, delimiter: str = ",") -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh, delimiter=delimiter))


def test_columns_merge_on_shared_timestamps_with_blank_gaps(tmp_path: Path) -> None:
    out = tmp_path / "plot.csv"
    speed = ExportColumn("Engine.Speed [rpm]", [0.1, 0.2, 0.3], [800.0, 850.0, 900.0])
    gear = ExportColumn("Engine.Gear", [0.2, 0.25], [3.0, 4.0])

    rows = write_signal_csv(out, [speed, gear], t_lo=0.0, t_hi=1.0, dialect=COMMA)

    assert rows == 4
    assert _read(out) == [
        ["time_s", "Engine.Speed [rpm]", "Engine.Gear"],
        ["0.100000", "800", ""],
        ["0.200000", "850", "3"],
        ["0.250000", "", "4"],
        ["0.300000", "900", ""],
    ]


def test_span_is_inclusive_and_nan_markers_are_skipped(tmp_path: Path) -> None:
    out = tmp_path / "plot.csv"
    times = [0.5, 1.0, 1.0, 2.0, 3.0, 3.5]
    column = ExportColumn("A.B", times, [1.0, 2.0, math.nan, 3.0, 4.0, 5.0])

    rows = write_signal_csv(out, [column], t_lo=1.0, t_hi=3.0, dialect=COMMA)

    assert rows == 3
    assert _read(out)[1:] == [["1.000000", "2"], ["2.000000", "3"], ["3.000000", "4"]]


def test_value_formatting() -> None:
    assert format_value(800.0, COMMA) == "800"
    assert format_value(-3.0, COMMA) == "-3"
    assert format_value(12.5, COMMA) == "12.5"
    assert format_value(0.1, COMMA) == "0.1"
    assert format_value(1e-05, COMMA) == "1e-05"
    assert format_value(12.5, SEMICOLON) == "12,5"


def test_semicolon_dialect_uses_decimal_comma(tmp_path: Path) -> None:
    out = tmp_path / "plot.csv"
    column = ExportColumn("Engine.Temp [°C]", [0.125], [21.5])

    write_signal_csv(out, [column], t_lo=0.0, t_hi=1.0, dialect=SEMICOLON)

    assert out.read_text(encoding="utf-8-sig").splitlines() == [
        "time_s;Engine.Temp [°C]",
        "0,125000;21,5",
    ]


def test_header_holding_the_delimiter_round_trips(tmp_path: Path) -> None:
    out = tmp_path / "plot.csv"
    column = ExportColumn("A.B [m;s]", [0.0], [1.5])

    write_signal_csv(out, [column], t_lo=0.0, t_hi=1.0, dialect=SEMICOLON)

    assert _read(out, ";") == [["time_s", "A.B [m;s]"], ["0,000000", "1,5"]]


def test_file_starts_with_a_bom_for_excel(tmp_path: Path) -> None:
    out = tmp_path / "plot.csv"
    write_signal_csv(out, [ExportColumn("A.B", [0.0], [1.0])], t_lo=0.0, t_hi=1.0, dialect=COMMA)

    assert out.read_bytes().startswith(b"\xef\xbb\xbftime_s,")
    assert not (tmp_path / "plot.csv.tmp").exists()


def test_nothing_in_span_writes_only_the_header(tmp_path: Path) -> None:
    out = tmp_path / "plot.csv"
    rows = write_signal_csv(
        out, [ExportColumn("A.B", [5.0], [1.0])], t_lo=0.0, t_hi=1.0, dialect=COMMA
    )

    assert rows == 0
    assert _read(out) == [["time_s", "A.B"]]
