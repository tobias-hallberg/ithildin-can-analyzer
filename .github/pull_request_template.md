<!-- Thanks for contributing! Keep the change focused and described below. -->

## What & why

<!-- What does this change, and what problem does it solve? -->

## How it was tested

<!-- Commands run, platform, and adapter if hardware-specific. -->

## Checklist

- [ ] `QT_QPA_PLATFORM=offscreen uv run pytest` passes
- [ ] `uv run ruff check src tests` and `uv run ruff format --check` pass
- [ ] `uv run mypy src` passes
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (for user-visible changes)
- [ ] Sends still route through the MCP allowlist (if MCP code changed)
