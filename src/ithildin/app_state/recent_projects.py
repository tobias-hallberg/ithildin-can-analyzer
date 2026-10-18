# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""MRU list of recently opened ``.itp`` / ``.ithildin`` paths.

Cross-project user state — lives at the ``~/.ithildin/`` root rather
than inside any per-project subdirectory. Backs the toolbar Open
button's recent-projects dropdown.

Format: JSON ``{"paths": ["/abs/path/one.itp", "/abs/path/two.itp"]}``,
newest first. Atomic write via tmpfile + ``os.replace``. Missing file
or unparseable contents → empty list, never raises during startup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class RecentProjectsStore:
    MAX = 10

    def __init__(self, path: Path, entries: list[Path]) -> None:
        self._path = path
        self._entries = entries

    @classmethod
    def load(cls, path: Path) -> RecentProjectsStore:
        if not path.exists():
            return cls(path, [])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path, [])
        raw = data.get("paths", []) if isinstance(data, dict) else []
        entries = [Path(p) for p in raw if isinstance(p, str)]
        return cls(path, entries)

    def paths(self) -> list[Path]:
        """MRU-ordered entries that still exist on disk."""
        return [p for p in self._entries if p.exists()]

    def add(self, path: Path) -> None:
        """Push ``path`` to the front, dedup, truncate, persist."""
        resolved = path.resolve()
        deduped = [resolved] + [p for p in self._entries if p.resolve() != resolved]
        self._entries = deduped[: self.MAX]
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {"paths": [str(p) for p in self._entries]}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
