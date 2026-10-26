# Contributing to ithildin

Thanks for your interest! ithildin is a CAN-bus analyser, so a lot of the
value is in getting the details right around real hardware and real DBCs.
Bug reports, hardware-compatibility notes, and PRs are all welcome.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

We use [uv](https://docs.astral.sh/uv/) for environment management and
Python ≥ 3.12.

```sh
uv sync
uv run ithildin examples/demo.itp   # boots with the demo virtual bus
```

## Quality gates

Every change must pass all four before it merges — CI runs them on Linux
and Windows across Python 3.12 and 3.13:

```sh
QT_QPA_PLATFORM=offscreen uv run pytest   # Qt tests run offscreen
uv run ruff check src tests
uv run ruff format --check
uv run mypy src                           # strict mode
```

`QT_QPA_PLATFORM=offscreen` keeps the PySide6 tests from flashing windows
on your screen. The `virtual` CAN backend works on every platform, so no
hardware is needed to run the suite.

## Conventions

The architecture and the invariants that keep the GUI responsive live in
[`CLAUDE.md`](CLAUDE.md) — read it before a non-trivial change. Highlights:

- **Comments explain _why_, not _what_.** Default to none; add one only for
  a non-obvious constraint or workaround.
- **Don't add speculative abstractions, fallbacks, or validation for
  things that can't happen.** Three similar lines beat a premature helper.
- **Decode lazily.** Plot / Monitor / Trace get full frame batches at
  60 Hz — filter to the IDs of interest first, then decode.
- **The MCP allowlist is the only send path.** Never bypass
  `AllowlistStore.check_*()`; every send attempt is audited, allowed or
  denied, sent or failed. See [SECURITY.md](SECURITY.md).
- **Qt thread shutdown** uses `DirectConnection` for `worker.stopped →
  thread.quit`, joined via `thread.wait()`.
- **DBC test fixtures** are hand-written DBC source strings (see
  `tests/conftest.py`), not programmatically built `cantools` objects.
- For Qt signals fired during a call, assert with
  `qtbot.waitSignal(...)`, not a spy that can miss the emission.

Where to make common changes:

| You want to … | Look at |
|---|---|
| Add a view kind | `ui/_child_base.py` + `ui/factory.py` |
| Add an MCP read tool | implement in `mcp/tools.py`, register in `mcp/server.py::register_read_tools` |
| Add an MCP write tool | same split; the body runs inside `tools._audited` and goes through `AllowlistStore.check_*`; register an `async` wrapper in `mcp/server.py::register_write_tools` that calls it via `to_thread.run_sync` with the server's `sends` limiter |
| Tune theme colours / QSS | `ui/theme.py` |
| Add an icon | drop the SVG in `resources/icons/`, append to `ui/icons.py` |

## Pull requests

1. Branch from `main`.
2. Keep the change focused; describe the user-visible effect and how you
   tested it (include the platform + adapter if hardware-specific).
3. Make sure the four gates above pass.
4. Add a `CHANGELOG.md` entry under `[Unreleased]` for anything
   user-visible.

## Reporting bugs

Open an issue with the bug-report template. Include your OS, the adapter
and vendor driver version, the DBC (or a minimal excerpt), and steps to
reproduce. Attach the log of the run where it happened: each launch writes
one to `~/.ithildin/logs/ithildin-<date>-<time>-<pid>.log`. For
**security** issues, do not open a public issue — follow
[SECURITY.md](SECURITY.md).
