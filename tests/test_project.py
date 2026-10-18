# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Tests for ``app_state.project`` — open / migrate / user-dir layout."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ithildin.app_state.project import MigrationError, new_unsaved, open_or_migrate

_V2_TOML = """\
schema_version = 2
name = "Engine"

[[bus]]
name = "CAN_1"
interface = "virtual"
channel = "engine"
bitrate = 500000

[mcp]
enabled = false
"""


def _v1_with_allowlist(dbc_filename: str = "example.dbc") -> str:
    return f"""\
schema_version = 1
name = "Engine"

[[bus]]
name = "Engine"
interface = "virtual"
channel = "engine"
bitrate = 500000

[[dbc]]
path = "{dbc_filename}"
buses = ["Engine"]

[[mcp.permissions.tx_budget]]
bus = "Engine"
max_frames_per_second = 50

[[mcp.permissions.send]]
bus = "Engine"
allow_messages = ["EngineCommand"]
allow_signals = ["EngineCommand.IdleSetpoint", "EngineCommand.EngineSpeed"]
allow_raw_ids = [256]
"""


_V1_NO_DBC_WITH_SIGNAL_RULE = """\
schema_version = 1
name = "Engine"

[[bus]]
name = "Engine"
interface = "virtual"
channel = "engine"
bitrate = 500000

[[mcp.permissions.send]]
bus = "Engine"
allow_signals = ["EngineCommand.IdleSetpoint"]
"""


@pytest.fixture
def alt_user_root(tmp_path: Path) -> Path:
    return tmp_path / "user-root"


def test_open_v2_no_migration(tmp_path: Path, alt_user_root: Path) -> None:
    p = tmp_path / "demo.itp"
    p.write_text(_V2_TOML, encoding="utf-8")
    project, report = open_or_migrate(p, user_root=alt_user_root)
    assert report is None
    assert project.workspace.name == "Engine"
    assert project.user_dir == alt_user_root / "Engine"


def test_user_dir_slugifies_name(tmp_path: Path, alt_user_root: Path) -> None:
    p = tmp_path / "demo.itp"
    p.write_text(_V2_TOML.replace("Engine", "Engine + Body / v2"), encoding="utf-8")
    project, _ = open_or_migrate(p, user_root=alt_user_root)
    # Slashes / spaces collapsed into underscores; never escapes user_root.
    rel = project.user_dir.relative_to(alt_user_root)
    assert "/" not in str(rel)
    assert all(c.isalnum() or c in "._-" for c in rel.name)


def test_v1_migration_writes_v2_and_lowers_allowlist(
    tmp_path: Path, alt_user_root: Path, example_dbc_path: Path
) -> None:
    legacy = tmp_path / "old.ithildin"
    legacy.write_text(_v1_with_allowlist(example_dbc_path.name), encoding="utf-8")

    project, report = open_or_migrate(legacy, user_root=alt_user_root)

    assert report is not None
    # 2 signals + 1 raw_id = 3 rules.
    assert report.rules_imported == 3
    # New file at .itp; old .ithildin stays on disk for safety.
    assert project.path.suffix == ".itp"
    assert project.path.exists()
    assert project.workspace.schema_version == 2
    assert legacy.exists()  # we don't delete the old file

    # Allowlist file exists and parses as TOML.
    allow = project.allowlist_path
    assert allow.exists()
    with open(allow, "rb") as f:
        data = tomllib.load(f)
    rules = data["rule"]
    assert len(rules) == 3
    signal_rules = [r for r in rules if r["signal"] != "*"]
    assert {r["signal"] for r in signal_rules} == {"IdleSetpoint", "EngineSpeed"}
    assert all(r["rate_hz"] == 50 for r in signal_rules)
    # Both signal rules belong to EngineCommand (frame_id 256 in the
    # fixture DBC). The migration must resolve the real CAN ID instead of
    # zeroing it.
    assert all(r["message_id"] == 256 for r in signal_rules)
    raw_rule = next(r for r in rules if r["signal"] == "*")
    assert raw_rule["message_id"] == 256
    assert raw_rule["enforce_signal_ranges"] is False


def test_v1_migration_refuses_when_signal_message_unknown(
    tmp_path: Path, alt_user_root: Path
) -> None:
    legacy = tmp_path / "old.ithildin"
    legacy.write_text(_V1_NO_DBC_WITH_SIGNAL_RULE, encoding="utf-8")

    with pytest.raises(MigrationError, match="EngineCommand"):
        open_or_migrate(legacy, user_root=alt_user_root)


def test_v2_project_with_dbc_can_be_loaded_into_database(
    qapp, tmp_path: Path, alt_user_root: Path, example_dbc_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A v2 .itp that declares ``[[dbc]]`` with bound buses should populate
    the runtime Database when iterated by ``app.py``.

    This pins the project-load fix so a future regression (i.e. dropping
    the ``add_dbc`` loop in ``app.py``) can't silently land buses with no
    decoders again.
    """
    from ithildin.app_state.database import Database

    p = tmp_path / "engine.itp"
    p.write_text(
        f"""schema_version = 2
name = "Engine"

[[bus]]
name = "Engine"
interface = "virtual"
channel = "engine"
bitrate = 500000

[[bus]]
name = "Body"
interface = "virtual"
channel = "body"
bitrate = 500000

[[dbc]]
path = "{example_dbc_path.as_posix()}"
buses = ["Engine", "Body"]
""",
        encoding="utf-8",
    )
    project, _report = open_or_migrate(p, user_root=alt_user_root)

    # Reproduce the loop in app.py.main:
    db = Database()
    for ref in project.workspace.dbc:
        db.add_dbc(ref.path, list(ref.buses))

    assert db.decoder_for_bus("Engine") is not None
    assert db.decoder_for_bus("Body") is not None
    assert sorted(db.buses_for_path(example_dbc_path.resolve())) == ["Body", "Engine"]


def test_unsupported_version_rejected(tmp_path: Path, alt_user_root: Path) -> None:
    p = tmp_path / "future.itp"
    p.write_text('schema_version = 99\nname = "Future"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version=99"):
        open_or_migrate(p, user_root=alt_user_root)


def test_unsaved_project_path_is_none_and_save_raises(alt_user_root: Path) -> None:
    project = new_unsaved(user_root=alt_user_root)
    assert not project.is_saved
    assert project.path is None
    # user_dir falls back to the _no_project slot so layout / allowlist /
    # audit have a well-known place to live before Save-As.
    assert project.user_dir == alt_user_root / "_no_project"
    with pytest.raises(ValueError, match="set_path"):
        project.save()


def test_set_path_then_save_writes_workspace(tmp_path: Path, alt_user_root: Path) -> None:
    project = new_unsaved(user_root=alt_user_root)
    target = tmp_path / "fresh.itp"
    project.set_path(target)
    assert project.is_saved
    project.save()
    assert target.exists()
    # user_dir is now slugified from the workspace name (default "Untitled").
    assert project.user_dir.name == "Untitled"
