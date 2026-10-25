# Codebase guide for AI assistants

This file primes Claude Code (and other AI assistants) on how this
repository is laid out and what invariants to respect when changing
code. Read it once per session before writing.

## Stack

PySide6 (Qt 6) · `python-can` · `cantools` · `pydantic` v2 · `pyqtgraph`
· `mcp` (FastMCP) · `pytest` + `pytest-qt` · `ruff` + `mypy --strict`.

Python ≥ 3.12. Use `uv` for env management. `uv run pytest`,
`uv run ruff check src tests`, `uv run mypy src`.

## Architecture in one screen

Single `QMainWindow` (`ui/main_window.py`) hosts a `QMdiArea` for child
views (Trace, Signal Plot, Signal Monitor, Data view, Transmit, Replay,
Record). The Frame inspector is **not** a separate MDI window — it's an
embedded `FrameInspectorView` (collapsible `CollapsibleSection` pane) at
the bottom of every Trace window. Hardware / Settings / MCP / About are
`QDialog`s, not MDI subwindows.

Process-level singletons live in `app_state/`:

- `BusReader` (`bus_reader.py`) — wraps `core/bus.py`'s `BusController`,
  exposes `bus_opening / bus_started / bus_stopped / bus_failed /
  frames_received(list) / error`, plus `cyclic_failed / send_failed` for
  TX and `bus_health_changed(name, state, detail)` with `health(name)` for
  bus health. `send_message` is synchronous and callable from any thread
  (Replay, MCP); `send_message_async` is the TX window's one-shot path.
- `Database` (`database.py`) — wraps `core/decoder.py`'s `Decoder`,
  emits `database_changed / error`.
- `Project` (`project.py`) — `.itp` file loader + legacy migration.
- `AppContext` (`context.py`) — **frozen** dataclass bundling the above
  + `AllowlistStore`, audit path, `McpServerBridge`, and MCP host/port.
- `diagnostics.py` — process-wide logging: `install()` (per-launch log
  file + crash hooks), `ReportRelay` (log records → Report dock),
  `StallWatchdog` (GUI-stall stack dumps).

MCP layer (`src/ithildin/mcp/`):

- `AllowlistStore` is the **single chokepoint** every send goes through.
  Default-deny: a send without a matching `AllowRule` is rejected.
  Per-rule token-bucket rate limit; DBC range clamp; agent filter.
- `AuditWriter` writes JSONL with 50 MB rotation; every send decision
  (allow or deny) is logged.
- `bridge.py` runs FastMCP on a worker-thread asyncio loop. Qt and
  asyncio loops stay independent — **do not** introduce `qasync`. A
  misbehaving tool handler must not stall paint events.

## Invariants to respect

- **`AppContext` is frozen.** Don't reach for `ctx.x = y` — hold
  override state on the calling widget instead. See `McpDialog._port_override`
  for the established pattern.
- **Decode lazily, in every consumer.** Plot, Monitor, Trace each receive
  full `frames_received` batches at 60 Hz from `BusReader`. Filter to
  IDs of interest *first*, then decode. Decoding everything every tick
  melts the GUI thread.
- **MCP allowlist is the only send path.** Never bypass
  `AllowlistStore.check_*()`. The audit log must record both allow and
  deny outcomes.
- **Bus worker shutdown** uses `Qt.ConnectionType.DirectConnection` for
  `worker.stopped → thread.quit`. The main thread blocks on
  `thread.wait()`; a queued slot can't fire there.
- **One send lock per bus.** `BusWorker._send_lock` serialises every
  touch of a python-can bus other than `recv()`: `send` (from any
  thread), cyclic tasks, task creation, the TX flush and `_close_bus`.
  `send` reads `_bus` once under it, so after a close every sender gets
  `RuntimeError("… not open")`. Take it without a timeout.
- **Cyclic tasks are app-built.** `BusWorker.send_periodic` builds a
  `ThreadBasedCyclicSendTask(bus, lock, …)` for every backend; never call
  `bus.send_periodic` (IXXAT's hardware scheduler raises when stopped
  twice, has no `modify_data` and doesn't echo). Its `on_error` emits
  `cyclic_failed` and returns `False`, so a failing task stops instead of
  retrying. Stop tasks through `BusReader.stop_periodic(bus, task)`.
- **TX flush runs on the worker thread.** `BusReader.flush_tx(bus)` →
  `BusWorker.request_flush()` sets a flag the capture loop services under
  the send lock: pcan `reset()` (also empties the RX queue), kvaser
  `flush_tx_buffer()`; never IXXAT (its flush waits INFINITE), never a
  listen-only bus. Flush on master OFF, on every disarm and on scheduler
  shutdown only — not when a row stops. The worker also flushes by itself
  when health worsens into bus errors / bus-off, which covers MCP and
  Replay frames.
- **TX disarms and never resumes implicitly.** `TxScheduler._disarm(reason)`
  is the single path: master OFF, stop every task, flush, emit
  `disarmed` (the TX window shows an amber banner and a Report entry). It
  runs when a bus an *active* row targets closes or fails or its health
  worsens into bus errors / bus-off, when a cyclic task fails, and when a
  wanted task can't start while armed. Re-arming while the bus is still
  degraded is allowed and holds until the next worsening. Arming is
  refused while an active row's bus is closed, still opening or
  listen-only. Nothing starts while master is OFF, so a reopened bus or a
  later edit never restarts TX — the user re-arms. The TX window and its
  scheduler share one `TxSetup`; what the UI shows as transmitting comes
  from `TxScheduler.is_transmitting`.
- **Bus health comes from the capture worker.** `BusWorker` classifies a
  `recv` `CanOperationError` instead of dying on it: pcan by `status()`
  (a raise the status doesn't explain, clean or error warning, is fatal
  unless its text is an overrun's), IXXAT by its exact message strings,
  Kvaser −15/−26 → lost; anything else stays fatal. A 0.5 s tick (it runs on an idle bus too)
  probes pcan `status()`, which is how a pulled PCAN-USB is noticed (`recv`
  only times out), and counts error frames (≥50/s → bus errors); pcan's
  error warning counts only while error frames arrive, since the
  controller holds it until it next transmits. Recovery needs two clean
  ticks, ties break probe > raised > SocketCAN state > rate, and
  `health(state, detail)` fires only on a change. A lost adapter raises
  into the normal failure path. Never add a sleep when `recv` returns
  `None`. `BusReader.health(name)` is `None` while opening, OK from
  `bus_started`; LOST outlives the failure until the bus is opened again
  or closed by name. **Degraded means `health is not BusHealth.OK`**, one rule
  everywhere. Known gaps: Kvaser bus-off shows only through the error-frame
  rate (python-can doesn't wrap `canReadStatus`); an IXXAT bus in error
  keeps up to 16 stale queued frames (its flush waits INFINITE).
- **Error frames are coalesced in `BusWorker`.** The ring gets at most ~10
  per second per bus, each with `FrameRecord.coalesced` = how many it
  stands for; data frames are never dropped. Bus state comes from
  `BusReader.health()`, not from counting error frames in a view.
- **Connect app-lifetime emitters to bound methods.** `BusReader`,
  `ThemeManager` and `UiEvents` outlive every view. A lambda capturing
  `self` keeps firing into a deleted widget; a bound QObject method
  disconnects itself. For theme reactions use
  `icons.themed_callback(self._restyle)`, which holds the widget weakly.
- **`Qt.WA_DeleteOnClose`** is set on every child view. The
  `WindowManager` listens to `QObject.destroyed` (not `closeEvent`) to
  deregister — `closeEvent` runs while the C++ object is still alive,
  but a queued connection from there fires after deletion.
- **Layout JSON** stores per-subwindow geometry relative to the
  `QMdiArea` viewport, not screen coordinates. That makes layouts
  naturally screen-independent: re-opening on a different monitor
  topology just clamps each subwindow into the new viewport. Atomic
  writes via `os.replace` of a `.tmp` file.
- **Window `instance_id`** is a uuid4 chosen at creation, persisted in
  `layout.json`. Never key persistence on the window title — titles
  change.
- **Vendor backends imported off the GUI thread.** `import ithildin`
  still does no vendor work; adapter detection runs on a background
  `QThread` (`ui/adapter_prober.py::AdapterProber`) kicked off from
  `LeftDockWidget.start_adapter_probe()` after `MainWindow.show()`. The
  supported set is deliberately narrow (`pcan`, `kvaser`, `cantact`,
  `socketcan`, `ixxat`) — every name in `_SUPPORTED_BACKENDS` is a
  vendor module python-can will eagerly import during
  `detect_available_configs(interfaces=[name])`, so
  adding one is paying real startup cost. Each per-backend probe runs
  in a sub-thread with a hard `BACKEND_TIMEOUT_S` ceiling; a vendor
  module that hangs on import is reported via `backend_failed` and the
  prober moves on rather than wedging the dock. The `virtual` loopback
  is a dev-only surface: it's emitted by the prober and seeded in the
  Create-bus dialog **only** when `adapter_prober.dev_mode()` is on
  (`ITHILDIN_DEV` env / `--dev` launch flag). Normal users never see it;
  the test suite still uses the `virtual` backend directly (it stays a
  valid `SupportedInterface`). `cantact` is an **optional extra**, not a
  hard dependency: upstream ships no wheel past cp312 and no sdist, so
  depending on it would cap the project at Python 3.12. python-can
  returns no configs when the module is absent, so the probe degrades to
  "no CANtact devices" — don't write code that assumes it imports.
- **Diagnostics go through `logging`.** `diagnostics.install()` is the
  first statement of `app.main`: one log file per launch in
  `~/.ithildin/logs` (newest 10 kept), `None` stdio (the windowed frozen
  build) redirected into it, `sys` / `threading` / unraisable excepthooks,
  `faulthandler`, and a Qt message handler. Use
  `logging.getLogger(__name__)` with %-style templates and args — every
  handler rate-limits per (logger, level, template, exception site), so a
  repeating message coalesces only if its template is constant. Never log
  per frame. **Rule B:** a failure already surfaced through a Qt `error`
  signal logs its traceback at WARNING (file only); ERROR is for failures
  with no other surface. `ReportRelay` mirrors ERROR and above from every
  logger except `can.bcm` into the Report dock, rate-limited 3 per 30 s;
  `can.*` warnings stay in the file and console. `MainWindow.report()`
  also writes each entry to the `ithildin.report` logger. Never call
  `basicConfig` / `dictConfig` or add handlers anywhere else; uvicorn runs
  with `log_config=None`. `StallWatchdog` (always on) dumps every thread's
  stack into the log when the GUI thread stalls for 5 s.

## UI patterns

### Theme

`ui/theme.py` owns tokens + `ThemeManager` singleton. Token names use
underscores (`bg_0`, `fg_1`, `accent_dim`) so they drop into
`str.format(**tokens)`. The QSS template covers
`QToolBar / QToolButton / QStatusBar / QDockWidget / QMdiArea /
QMdiSubWindow / Q{Tree,Table,List}View / QHeaderView / QLineEdit /
QPushButton / QMenu / QScrollBar` plus banner widgets
(`#WarnBanner / #InfoBanner / #ErrorBanner`).

When you need a colour in inline QSS, prefer `palette(mid)` (= fg_2)
etc. over hard-coding hex. When you need a colour from Python, read
`ThemeManager.instance().current_tokens()[token]`.

### Icons

`ui/icons.py` is the only entry point. `icon(name)` returns a
theme-tinted multi-size `QIcon` cached by `(name, tint)`. Bundled SVGs
live in `src/ithildin/resources/icons/`; they paint with
`stroke="currentColor"` (or `fill="currentColor"` for filled glyphs)
and are rewritten at load time to the active `fg_1`. The cache is
cleared on `ThemeManager.theme_changed`. `toggle_icon(off, on)` makes a
two-state icon for checkable buttons.

### Fonts

`ui/fonts.py` exposes `ui_font()` (Inter → Segoe UI → system-ui) and
`tabular_font()` (JetBrains Mono → Consolas → Menlo → monospace, with
`StyleHint.Monospace`). Use `tabular_font()` anywhere digit alignment
matters: Trace ID column, audit feed, Frame Inspector byte cells.

### Banners + Report dock

`ui/banner.py`'s `Banner(kind=…)` is the inline error / warn / info
strip — uses `setObjectName("ErrorBanner" | "WarnBanner" | "InfoBanner")`
+ `WA_StyledBackground` so the QSS rules tint it. `ui/report_dock.py`'s
`ReportDockWidget` is the cross-window event log living in MainWindow's
bottom dock. `MainWindow.report(msg, kind=...)` is the public hook —
`BusReader.error / send_failed / cyclic_failed` and `Database.error`
already wire into it. Views, which hold no `MainWindow` reference, emit
`ctx.events.report_requested(msg, kind)` instead.

### Toolbar buttons

The main toolbar uses `setIconSize(QSize(24, 24))` +
`ToolButtonTextUnderIcon`. Note: that style only applies to QToolButtons
auto-created from QActions added via `addAction()`. Custom QToolButtons
added via `insertWidget()` (e.g. `ViewLauncherButton`) **must call
`setToolButtonStyle` themselves** or icons render without their text
labels.

### View header bar

`ui/view_header.py`'s `ViewHeaderBar` is the uniform top strip every MDI
view kind uses — it owns the chrome (`#ViewHeader` QSS: window-tinted
background + 1 px bottom divider, fixed height) so Trace / Plot / Record
strips read identically instead of each rolling a bare `QHBoxLayout`.
Views fill it via `add_widget / add_layout / add_stretch` and standard
pane toggles via `add_pane_toggle(tooltip, slot)` (which pins the flat
`AutoRaise` + `NoFocus` look). The bar is placed wherever that view's
strip already lived — placement is **not** unified, only the chrome.

It also hosts the maximized-window controls: when a subwindow is
maximized the native MDI title bar collapses to 0 px
(`_TightMdiFrameStyle`), so `WindowManager` calls `enter_maximized` /
`exit_maximized` on the active view's header to surface a title + the
restore/close buttons (wired to `showNormal` / `close` at creation).
**Every maximizable view must assign `self.header` in `_build_body`** —
otherwise a maximized window has no chrome and no way to restore/close.
Views with no controls of their own (Monitor, Data, Replay, Transmit)
pass `ViewHeaderBar(hide_when_idle=True)` so the bar stays hidden until
maximized.

### Drag and drop

`ui/_drag_payload.py` defines `SignalPayload` and `MessagePayload` and
the `application/x-ithildin-signal` MIME type. Drop-target widgets use
`ui/_signal_drop.py`'s `SignalDropArea` wrapper around any pyqtgraph
widget (drops on the inner GraphicsView don't propagate reliably).

## Coding conventions

- Default to writing **no comments**. Add one only when the *why* is
  non-obvious — a hidden invariant, a workaround, behaviour that would
  surprise a reader. Don't restate what the code says.
- Don't add fallbacks, validation, or error handling for things that
  can't happen. Trust internal code.
- No backwards-compat shims for unreleased code. Just change it.
- Don't introduce abstractions speculatively. Three similar lines is
  better than a premature helper.
- Default units: metric.
- Stage names use Greek letters (`α / β / γ / δ / ε`); ruff RUF001/2/3
  are globally ignored in `pyproject.toml` for that reason.

## Testing

- `tests/conftest.py` exposes `make_app_context(tmp_path)` — use it
  whenever a test needs a populated `AppContext`.
- The `qapp` and `qtbot` fixtures come from `pytest-qt`.
- For CAN, the `virtual` backend works on every platform — see
  `tests/test_signal_plot.py::test_filter_then_decode_updates_curve` for
  a complete end-to-end pattern: open a channel, send via `can.Bus`,
  assert via `qtbot.waitUntil`.
- For bus fault paths, `tests/fake_bus.py`: `use_fake_bus(monkeypatch)`
  makes every `BusWorker` open a scriptable `FakeBus` (raising
  `send`/`recv`, flush counters, the sending thread and lock state per
  frame), so a `pcan` / `kvaser` / `ixxat` config runs that backend's
  branches without importing a vendor module. Extend it rather than
  writing another fake.
- For Qt signals fired during a method call, use
  `with qtbot.waitSignal(sig, timeout=…):` not `QSignalSpy().wait()` —
  the spy can miss signals fired before `wait()` returns.
- DBC fixtures: hand-write the DBC source as a string (see
  `tests/conftest.py::_DBC_SOURCE`). Don't construct `cantools.Signal`
  objects programmatically — that constructor has shifted across
  cantools major versions.
- Never call `diagnostics.install()` in the test process: it rewires the
  root logger, stdio, excepthooks and faulthandler, and defaults to the
  real `~/.ithildin/logs`. Run it in a subprocess against `tmp_path` (see
  `tests/test_diagnostics.py`); assert log output with `caplog`.

## Common file locations

| You want to … | Look at |
|---|---|
| Wire a new toolbar action | `ui/main_window.py::_build_toolbar` |
| Add a new view kind | `ui/_child_base.py` + `ui/factory.py` registry |
| Add a new icon | drop SVG in `resources/icons/`, append to `_INVENTORY` in `ui/icons.py` |
| Tune theme colours | `ui/theme.py::TOKENS` |
| Tune QSS for a widget | `ui/theme.py::_QSS_TEMPLATE` |
| Add an MCP read tool | implement in `mcp/tools.py`, register in `mcp/server.py::register_read_tools` |
| Add an MCP write tool | same split; register in `mcp/server.py::register_write_tools` (must go through `AllowlistStore.check_*`) |
| Wire a `BusReader` error display | `MainWindow.report()` and per-view banners |
| Add or re-enable a CAN backend | `ui/adapter_prober.py::_SUPPORTED_BACKENDS` + `workspace/schema.py::SupportedInterface` (must update both) |
| Add a vendor dep that limits the Python range | `[project.optional-dependencies]` in `pyproject.toml` + the `--extra` in `.github/workflows/release.yml` |
| Persist user-private state | `~/.ithildin/<project>/` (allowlist, audit, layout) |
| Log a diagnostic | `logging.getLogger(__name__)`; levels and dock mirroring in `app_state/diagnostics.py` |
| Read the diagnostic logs | `~/.ithildin/logs/` (app-global, one file per launch) |
| Persist project state | the `.itp` file (TOML schema v2) |

## What this project is *not*

- Not multi-bus in v0.1 (`BusController` supports it; UI exposes one).
- Not CAN FD in v0.1.
- Not a DBC editor — read-only DBC tree only.
- Not macOS in v1.
- Not UDS / ISO-TP / J1939.

## Deferred features (not in v0.1)

These are intentionally not built yet. Don't reference them in code or
docs as if they exist.

- **Cyclic / periodic MCP transmit.** `python-can`'s `CyclicSendTask`
  isn't wired up; the MCP layer has only one-shot `send_signal` and
  `send_frame`. When this lands, agent disconnect must tear down that
  agent's running tasks.
- **Replay pause / scrub.** `core/replay.py` plays linearly from start
  to end. The Replay window discloses this in its hint.
- **Settings dialog content.** `ui/settings_window.py` exposes only the
  Project · Plot defaults section. Capture / MCP sections land in v0.2.
- **Frame inspector bit-spanning hover highlight.** Byte cells render
  but bit-level highlight is a Stage 1.E polish item, not done.
