# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Read and write ``.itp`` files.

Schema-version-aware. Use ``detect_schema_version`` to decide which loader
to call; ``Project`` (in ``app_state``) handles the v1→v2 migration path.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from ithildin.workspace.schema import LegacyV1Workspace, Workspace


def detect_schema_version(path: Path | str) -> int:
    """Return the ``schema_version`` field; defaults to 1 if missing."""
    p = Path(path)
    with open(p, "rb") as f:
        data = tomllib.load(f)
    if not data:
        raise ValueError(f"{p.name} is empty or not a valid .itp file")
    return int(data.get("schema_version", 1))


def load_workspace(path: Path | str) -> Workspace:
    """Load a v2 ``.itp`` and resolve relative DBC paths to absolute."""
    p = Path(path)
    with open(p, "rb") as f:
        data = tomllib.load(f)
    ws = Workspace.model_validate(data)
    base = p.parent.resolve()
    for dbc in ws.dbc:
        if not dbc.path.is_absolute():
            dbc.path = (base / dbc.path).resolve()
    return ws


def load_workspace_v1(path: Path | str) -> LegacyV1Workspace:
    """Load a legacy v1 file. Used only during migration."""
    p = Path(path)
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return LegacyV1Workspace.model_validate(data)


def save_workspace(ws: Workspace, path: Path | str) -> None:
    """Serialize a v2 workspace to TOML at ``path`` (atomic write)."""
    p = Path(path)
    data = _to_toml_dict(ws)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(data, f)
    os.replace(tmp, p)


def _to_toml_dict(ws: Workspace) -> dict[str, Any]:
    """Pydantic ``model_dump(mode='json')`` coerces ``Path`` to ``str``.

    ``exclude_none`` drops optional fields left at ``None`` — TOML has no
    null type, so ``tomli_w`` raises on them. Omitted keys re-default to
    ``None`` on load.
    """
    return ws.model_dump(mode="json", exclude_none=True)
