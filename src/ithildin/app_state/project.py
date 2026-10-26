# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Project lifecycle: open / save ``.itp`` files + per-project user-private state.

A ``Project`` represents one open ``.itp`` file. It owns the workspace
config and resolves the ``~/.ithildin/<project>/`` directory where layout,
allowlist, audit log and adapter pins live (kept out of the project file so
the project itself is shareable).

On first open of a legacy v1 ``.ithildin`` file, ``open_or_migrate`` writes
the legacy MCP allowlist out as ``AllowRule`` rows in
``~/.ithildin/<project>/allowlist.toml`` and rewrites the project as v2.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from ithildin.workspace.loader import (
    detect_schema_version,
    load_workspace,
    load_workspace_v1,
    save_workspace,
)
from ithildin.workspace.schema import (
    LOOPBACK_HOSTS,
    DbcRef,
    LegacyV1Workspace,
    McpConfig,
    Workspace,
)

_log = logging.getLogger(__name__)

_USER_ROOT = Path.home() / ".ithildin"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class MigrationError(Exception):
    """Raised when a legacy v1 file cannot be safely migrated.

    Examples: a v1 allowlist references a signal whose containing message
    is not defined in any DBC bound to the project, or a DBC fails to
    load. We refuse to write a half-broken v2 allowlist instead of
    silently dropping rules or zeroing CAN IDs.
    """


@dataclass(frozen=True)
class MigrationReport:
    """Summary of a legacy v1 → v2 migration."""

    rules_imported: int
    legacy_path: Path
    new_path: Path


class Project:
    """One ithildin project — either bound to a ``.itp`` file or unsaved.

    An unsaved project (``path is None``) is the running state of a
    fresh session before the user has chosen a filesystem location. Bus
    declarations, DBC bindings, and view content all live on the
    in-memory workspace immediately and survive the eventual Save-As.
    """

    def __init__(
        self,
        path: Path | None,
        workspace: Workspace,
        user_root: Path | None = None,
    ) -> None:
        self._path: Path | None = path.resolve() if path is not None else None
        self._workspace = workspace
        self._user_root = (user_root or _USER_ROOT).resolve()

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def is_saved(self) -> bool:
        """True once the project has a filesystem path bound to it."""
        return self._path is not None

    def set_path(self, path: Path) -> None:
        """Bind this project to a filesystem path. Used by Save-As."""
        self._path = path.resolve()

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def user_dir(self) -> Path:
        """``~/.ithildin/<project>/`` for layout, allowlist, audit.

        Falls back to ``_no_project`` while the project is unsaved so
        layout / allowlist / audit have somewhere to live before Save-As.
        """
        if self._path is None:
            return self._user_root / "_no_project"
        return self._user_root / _slug(self._workspace.name or self._path.stem)

    @property
    def layout_path(self) -> Path:
        return self.user_dir / "layout.json"

    @property
    def allowlist_path(self) -> Path:
        return self.user_dir / "allowlist.toml"

    @property
    def audit_path(self) -> Path:
        return self.user_dir / "audit.jsonl"

    @property
    def adapter_pins_path(self) -> Path:
        """Bus name → ``BusConfig.hw_id``: the adapter each bus is pinned to."""
        return self.user_dir / "adapter_pins.json"

    def ensure_user_dir(self) -> None:
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def load_adapter_pins(self) -> None:
        """Pin each bus to the adapter this user's pins file names for it."""
        path = self.adapter_pins_path
        try:
            pins = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            _log.error("Adapter pins in %s are unreadable; no bus is pinned", path, exc_info=True)
            return
        if not isinstance(pins, dict):
            _log.error("Adapter pins in %s are not a mapping; no bus is pinned", path)
            return
        self._workspace.bus = [
            b.model_copy(update={"hw_id": pins[b.name]}) if isinstance(pins.get(b.name), str) else b
            for b in self._workspace.bus
        ]

    def save_adapter_pins(self) -> None:
        """Write the buses' pins to :attr:`adapter_pins_path` (atomic)."""
        pins = {b.name: b.hw_id for b in self._workspace.bus if b.hw_id is not None}
        path = self.adapter_pins_path
        if not pins and not path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pins, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def set_workspace(self, workspace: Workspace) -> None:
        """Replace the in-memory workspace before save.

        ``MainWindow._save_project`` rebuilds a fresh ``Workspace`` each
        time from the live UI state and swaps it in here. Without this
        hop, ``save()`` would write the workspace as it was loaded —
        DBC additions, MCP changes, view drops since open would all be
        silently dropped.
        """
        self._workspace = workspace

    def adopt(self, other: Project) -> None:
        """Take on another project's path + workspace in place.

        Used when ``MainWindow`` switches projects mid-session (File →
        Open / File → New). Reassigning ``self._current_project`` to a
        fresh ``Project`` instance would split state from the one held
        by ``AppContext.project`` (frozen field), so left-dock and
        other consumers would keep reading the *old* workspace. By
        mutating the existing instance every consumer that holds a
        reference picks up the change for free.
        """
        self._path = other._path
        self._workspace = other._workspace
        self._user_root = other._user_root

    def save(self) -> None:
        """Write the ``.itp``, and the adapter pins beside this user's other state."""
        if self._path is None:
            raise ValueError("Project has no path; call set_path first")
        save_workspace(self._workspace, self._path)
        self.save_adapter_pins()


def new_unsaved(*, user_root: Path | None = None) -> Project:
    """Construct a fresh, unsaved ``Project``.

    Used by ``app.py`` when no ``.itp`` argv was given and by
    ``MainWindow._new_project``. Callers can mutate
    ``project.workspace`` immediately; ``save()`` will refuse until
    ``set_path`` has been called (typically via the Save-As dialog).
    """
    return Project(path=None, workspace=Workspace(name="Untitled"), user_root=user_root)


def _slug(name: str) -> str:
    """Filesystem-safe directory name for a project's user dir."""
    safe = _SAFE_NAME.sub("_", name).strip("_")
    return safe or "project"


def open_or_migrate(
    path: Path | str,
    *,
    user_root: Path | None = None,
) -> tuple[Project, MigrationReport | None]:
    """Open a ``.itp`` (v2) or ``.ithildin`` (v1, migrated in place).

    Returns the project plus, when a migration happened, a report the
    caller should surface in the Report dock.
    """
    p = Path(path).resolve()
    version = detect_schema_version(p)
    if version == 2:
        project = Project(p, load_workspace(p), user_root=user_root)
        project.load_adapter_pins()
        return project, None
    if version == 1:
        return _migrate_v1(p, user_root=user_root)
    raise ValueError(f"Unsupported schema_version={version} in {p!s}")


def _migrate_v1(
    legacy_path: Path,
    *,
    user_root: Path | None,
) -> tuple[Project, MigrationReport]:
    """Lower a v1 file's inline allowlist to the external file and rewrite as v2."""
    legacy = load_workspace_v1(legacy_path)

    new_path = legacy_path.with_suffix(".itp")

    new_ws = Workspace(
        schema_version=2,
        name=legacy.name,
        bus=list(legacy.bus),
        dbc=list(legacy.dbc),
        mcp=McpConfig(
            enabled=legacy.mcp.enabled,
            # v2 dropped the unused inline auth_token (auth is now a per-user
            # token file) and rejects non-loopback binds; sanitize a legacy
            # host rather than fail the migration on an old bad value.
            host=legacy.mcp.host if legacy.mcp.host in LOOPBACK_HOSTS else "127.0.0.1",
            port=legacy.mcp.port,
            dry_run=legacy.mcp.dry_run,
        ),
    )
    project = Project(new_path, new_ws, user_root=user_root)
    project.ensure_user_dir()

    name_to_id = _build_dbc_name_index(legacy.dbc, base_dir=legacy_path.parent)
    rules_imported = _lower_v1_allowlist(legacy, project.allowlist_path, name_to_id)
    save_workspace(new_ws, new_path)

    report = MigrationReport(
        rules_imported=rules_imported,
        legacy_path=legacy_path,
        new_path=new_path,
    )
    return project, report


def _build_dbc_name_index(dbc_refs: list[DbcRef], base_dir: Path) -> dict[str, int]:
    """Resolve message names to CAN IDs by loading every bound DBC.

    Used by the v1 → v2 migration so signal rules can carry a real
    ``message_id`` instead of a zeroed-out placeholder. Last writer wins
    on duplicate names — fine for migration, since v1 signal paths
    weren't bus-scoped anyway. Raises ``MigrationError`` on a DBC that
    cantools cannot load; that's a deliberate refusal to ship a partial
    allowlist.
    """
    import cantools.database

    name_to_id: dict[str, int] = {}
    for ref in dbc_refs:
        path = ref.path if ref.path.is_absolute() else (base_dir / ref.path)
        try:
            db = cantools.database.load_file(str(path))
        except Exception as exc:
            raise MigrationError(f"cannot load DBC {path!s}: {exc}") from exc
        # ``load_file`` returns either a CAN or a Diagnostics database;
        # only the CAN variant carries the message list we want.
        if not isinstance(db, cantools.database.can.database.Database):
            continue
        for msg in db.messages:
            name_to_id[msg.name] = msg.frame_id
    return name_to_id


def _lower_v1_allowlist(
    legacy: LegacyV1Workspace,
    dest: Path,
    name_to_id: dict[str, int],
) -> int:
    """Import v1 inline allowlist into the new external file.

    The new ``AllowRule`` shape is per-message + per-signal. The v1 shape
    was per-bus + lists. We expand: each ``allow_signals`` entry becomes
    one rule with a real ``message_id`` resolved via ``name_to_id``;
    ``allow_raw_ids`` become wildcard-signal rules with a matching CAN
    ID. ``tx_budget`` becomes a default ``rate_hz`` on each rule for
    that bus.

    Raises ``MigrationError`` if any signal rule names a message not
    found in any bound DBC. We refuse to write a rule that would never
    match.
    """
    rate_for_bus: dict[str, float] = {
        b.bus: float(b.max_frames_per_second) for b in legacy.mcp.permissions.tx_budget
    }

    rules: list[dict[str, object]] = []
    next_id = 1
    now = datetime.now(UTC).isoformat()

    def _row(**kw: object) -> dict[str, object]:
        # TOML has no null; drop None fields.
        return {k: v for k, v in kw.items() if v is not None}

    for rule in legacy.mcp.permissions.send:
        rate = rate_for_bus.get(rule.bus)
        for signal_path in rule.allow_signals:
            if "." not in signal_path:
                continue
            msg_name, sig = signal_path.split(".", 1)
            message_id = name_to_id.get(msg_name)
            if message_id is None:
                raise MigrationError(
                    f"v1 allowlist references unknown message {msg_name!r} "
                    f"on bus {rule.bus!r}; bind the DBC that defines it "
                    "before migrating, or remove the rule."
                )
            rules.append(
                _row(
                    id=next_id,
                    message_id=message_id,
                    message_name=msg_name,
                    signal=sig,
                    rate_hz=rate,
                    bus_name=rule.bus,
                    enforce_signal_ranges=rule.enforce_signal_ranges,
                    created_at=now,
                )
            )
            next_id += 1
        for raw_id in rule.allow_raw_ids:
            rules.append(
                _row(
                    id=next_id,
                    message_id=int(raw_id),
                    signal="*",
                    rate_hz=rate,
                    bus_name=rule.bus,
                    enforce_signal_ranges=False,
                    created_at=now,
                )
            )
            next_id += 1

    payload = {"rule": rules}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        tomli_w.dump(payload, f)
    return len(rules)
