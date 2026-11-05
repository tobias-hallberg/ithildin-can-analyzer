# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""User-scoped export preferences, kept in ``QSettings``.

The CSV format follows the user's spreadsheet locale, not the project, so
it stays out of the git-shared ``.itp``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PySide6.QtCore import QLocale, QSettings

from ithildin.core.signal_export import COMMA, SEMICOLON, CsvDialect

CsvFormat = Literal["system", "comma", "semicolon"]
CSV_FORMATS: tuple[CsvFormat, ...] = ("system", "comma", "semicolon")

SETTINGS_ORG = "Ithildin"
SETTINGS_APP = "Ithildin"
_FORMAT_KEY = "export/csv_format"
_LAST_DIR_KEY = "export/last_dir"


def _settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def load_csv_format() -> CsvFormat:
    choice = _settings().value(_FORMAT_KEY, "system")
    if choice not in CSV_FORMATS:
        return "system"
    return choice


def save_csv_format(fmt: CsvFormat) -> None:
    _settings().setValue(_FORMAT_KEY, fmt)


def csv_dialect(fmt: CsvFormat) -> CsvDialect:
    if fmt == "comma":
        return COMMA
    if fmt == "semicolon":
        return SEMICOLON
    return SEMICOLON if QLocale.system().decimalPoint() == "," else COMMA


def last_export_dir() -> Path:
    stored = _settings().value(_LAST_DIR_KEY, "")
    if isinstance(stored, str) and stored and Path(stored).is_dir():
        return Path(stored)
    return Path.home()


def remember_export_dir(directory: Path) -> None:
    _settings().setValue(_LAST_DIR_KEY, str(directory))
