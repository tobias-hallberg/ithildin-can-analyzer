# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Write decoded signal samples to a wide CSV file.

One ``time_s`` column, then one column per signal. Each distinct
timestamp gets one row; a signal with no sample at that instant leaves its
cell empty, so nothing is interpolated or held. Signals decoded from the
same frame share a timestamp and land on the same row.
"""

from __future__ import annotations

import csv
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CsvDialect:
    delimiter: str
    decimal: str


COMMA = CsvDialect(delimiter=",", decimal=".")
SEMICOLON = CsvDialect(delimiter=";", decimal=",")


@dataclass(frozen=True)
class ExportColumn:
    header: str
    times: Sequence[float]
    values: Sequence[float]


def format_value(value: float, dialect: CsvDialect) -> str:
    text = str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    return text.replace(".", dialect.decimal) if dialect.decimal != "." else text


def format_time(t: float, dialect: CsvDialect) -> str:
    text = f"{t:.6f}"
    return text.replace(".", dialect.decimal) if dialect.decimal != "." else text


def write_signal_csv(
    path: Path,
    columns: Sequence[ExportColumn],
    *,
    t_lo: float,
    t_hi: float,
    dialect: CsvDialect,
) -> int:
    """Write the samples within ``[t_lo, t_hi]`` to ``path``; return the row count.

    NaN samples are the plot's line-break markers, not data, and are skipped.
    """
    rows: dict[float, list[str]] = {}
    width = len(columns)
    for i, column in enumerate(columns):
        for t, value in zip(column.times, column.values, strict=True):
            if t < t_lo or t > t_hi or math.isnan(value):
                continue
            row = rows.get(t)
            if row is None:
                row = rows[t] = [""] * width
            row[i] = format_value(value, dialect)

    tmp = path.with_name(path.name + ".tmp")
    # utf-8-sig: Excel reads a BOM-less file in the ANSI code page, which
    # mangles units such as °C.
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=dialect.delimiter)
        writer.writerow(["time_s", *(c.header for c in columns)])
        for t in sorted(rows):
            writer.writerow([format_time(t, dialect), *rows[t]])
    os.replace(tmp, path)
    return len(rows)
