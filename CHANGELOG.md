# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.

## [Unreleased]

## [0.1.1] - 2026-10-24

### Added

- Plot curve colours can be picked: click the colour bar on a legend row
  for the theme swatches, a custom colour, or back to automatic. Picked
  colours survive theme changes and are saved with the project.

### Fixed

- The Trace and Record toolbar buttons accept buses and databases dragged
  from the left dock, not just signals and messages, and the new window
  starts out tracking what was dropped.
- MDI subwindows are easier to resize: the grab zone along each edge and
  corner is several pixels deep instead of the 2 px frame.

## [0.1.0] - 2026-10-18

First public beta. Feature set at this point:

### Added

- Live CAN capture from `pcan`, `kvaser`, `cantact`, `ixxat`, and
  `socketcan` backends, plus a driver-free `virtual` backend for demos and
  tests. Listen-only mode on backends that expose a silent kwarg. CANtact
  support rides on the optional `cantact` extra, which upstream ships only
  up to CPython 3.12.
- DBC decode via `cantools`, with a read-only DBC tree in the left dock.
- Trace, Signal Plot, Signal Monitor, and Data views (multi-instance MDI),
  an embedded Frame Inspector, and Record / Replay windows.
- Transmit window with one-shot and periodic sends behind a master switch.
- Built-in MCP server: bearer-token-gated, loopback-only streamable-HTTP
  transport, started from the MCP dialog or automatically via
  `[mcp] enabled`. Ten read tools cover bus, DBC, and traffic
  introspection (`list_buses`, `list_messages`, `list_signals`,
  `describe_signal`, `get_recent_messages`, `get_signal_value`,
  `get_bus_stats`, `list_dbcs`, `list_allowlist`, `get_audit_log`); the
  two write tools (`send_signal`, `send_frame`) are allowlist-gated with
  DBC range enforcement, per-rule rate limiting, an optional
  `[mcp] dry_run` mode, and a JSONL audit log. Rules are authored from
  DBC message/signal pickers with the signal's range shown inline. See
  `SECURITY.md`.
- Token-driven dark/light themes with theme-tinted SVG icons.
- `.itp` project files (TOML schema v2) with one-time migration from v1.

### Security

- The MCP HTTP transport requires an `Authorization: Bearer` token stored
  per-user (never in the shareable `.itp`), binds to loopback only, and
  validates CAN ID range, payload length, and signal finiteness on every
  send.

[Unreleased]: https://github.com/tobias-hallberg/ithildin-can-analyzer/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/tobias-hallberg/ithildin-can-analyzer/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tobias-hallberg/ithildin-can-analyzer/releases/tag/v0.1.0
