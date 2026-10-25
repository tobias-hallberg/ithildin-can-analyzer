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
  Per-rule token-bucket rate limit; DBC range clamp; agent filter. A
  bus-scoped rule matches only a request for its bus; one with no bus
  matches only any-bus rules.
- `AuditWriter` writes JSONL with 50 MB rotation. Every write-tool attempt
  writes exactly one line, through `tools._audited` (try/finally, so an
  attempt that raises — even a non-`Exception` — is recorded as
  `INTERNAL_ERROR`). The line carries the allowlist's `decision` (ALLOW /
  DENY) and, separately, the frame's `outcome` (SENT / SEND_FAILED /
  NOT_SENT / DRY_RUN / UNKNOWN): a driver error is ALLOW + SEND_FAILED,
  never a DENY. SENT means the driver accepted the frame, not that the bus
  ACKed it.
- Before the allowlist, the write tools resolve the target (the `bus`
  named, else the single open bus) and refuse an unresolved one (`NO_BUS`),
  a listen-only one (`BUS_READ_ONLY`) and a degraded one (`BUS_UNHEALTHY`,
  any `BusReader.health()` but OK), so a refusal spends no rate-limit
  token. The send always gets a concrete name: given `None`,
  `send_message` would pick whichever single bus is open by then, unchecked.
  `list_buses` reports `opening` (no health yet), `live`, or the degraded
  `BusHealth` value with `status_detail`.
- `bridge.py` runs FastMCP on a worker-thread asyncio loop. Qt and
  asyncio loops stay independent — **do not** introduce `qasync`. A
  misbehaving tool handler must not stall paint events. Write tools are
  `async` wrappers that run the synchronous send on an anyio worker thread
  (`to_thread.run_sync(..., abandon_on_cancel=True, limiter=…)` with one
  `CapacityLimiter(1)` per server), so a stalled driver blocks neither the
  read tools nor the bridge's stop, and the audit order is the send order —
  except after a cancelled call or a bridge restart, whose abandoned send
  may still be inside the driver beside the next one.
  The limiter is built on the GUI thread, hence the `anyio>=4.2` floor.

## Invariants to respect

- **`AppContext` is frozen.** Don't reach for `ctx.x = y` — hold
  override state on the calling widget instead. See `McpDialog._port_override`
  for the established pattern.
- **Decode lazily, in every consumer.** Plot, Monitor, Trace each receive
  full `frames_received` batches at 60 Hz from `BusReader`. Filter to
  IDs of interest *first*, then decode. Decoding everything every tick
  melts the GUI thread. A batch carries at most 8192 frames per bus
  (`_MAX_FANOUT_PER_BUS`); after a stall the older backlog is skipped and
  reported through `frames_dropped`, never caught up on.
- **Consumers skip error frames before decoding.** An error frame's
  `arbitration_id` is an error class, not a DBC message ID, and a sampled
  one stands for `FrameRecord.coalesced` error frames. Every view and MCP
  read tool checks `is_error_frame` before any decode. Only a whole-bus
  subscription admits error frames: every caller of
  `_Effective.admits(bus, aid, *, is_error=)` passes `is_error`.
- **Never put a proxy model over `TraceTreeModel`.** QTreeView lays out
  every top-level row on each insert at the root, and a proxy maps each
  row through `rowCount`, which decodes. So the model filters text itself
  (`set_text_filter`), keeps up to `max_rows` frames but shows only the
  newest `live_rows` (1000) while running and the whole history while
  paused, and answers `index` / `hasChildren` / `rowCount` without
  decoding a collapsed row: `hasChildren` asks the DBC (cached per ID)
  unless the frame is already decoded,
  and Latest mode decodes a new frame only for an ID whose signal rows the
  view has asked for. Each drain costs O(new frames + `live_rows`).
  Signal rows carry an integer internal id, never a pointer
  (`createIndex(row, col, obj)` holds no reference): the view keeps a
  Latest row's signal-row indexes across frame replacements, and a pointer
  to a frame the ring has freed crashes the next paint. `TraceWindow`
  connects `rowsAboutToBeRemoved` before `setModel`, so a selected row
  leaving the live window is deselected before the view moves the
  selection to the next row on every drain.
- **MCP allowlist is the only send path.** Never bypass
  `AllowlistStore.check_*()`. The audit log must record every attempt,
  allowed or denied, sent or not: write tools go through `tools._audited`,
  the only writer of their audit lines.
- **Bus worker shutdown** uses `Qt.ConnectionType.DirectConnection` for
  `worker.stopped → thread.quit`. The main thread blocks on
  `thread.wait()`; a queued slot can't fire there. `start_capture` catches
  `BaseException` at its boundary (vendor panics) and emits `stopped` from
  a nested `finally`, so it can't be skipped; a stop requested during a
  slow open shuts the bus without ever emitting `started`.
- **`bus_stopped` is synchronous and fires once per open.** `BusReader.close()`
  asks every target to stop, waits on one shared deadline, fans out the
  rings' tails, then emits `bus_stopped(name)` as `name` leaves
  `bus_names()` — so a `bus_stopped` slot runs inside the caller's
  `close()` (don't call `close()`/`open()` back from it). A worker that
  exits on its own does the same from its `stopped`, with `bus_failed`
  just before. Open / New Project and removing a bus from the dock close
  and `forget()` it.
- **Capture sessions end only on an explicit close of the last bus.** A
  worker failure keeps `session_t0` and every view's data, and
  `BusReader.reopen(config)` (the Trace's Reconnects, given the
  workspace's config) closes and opens a bus within the same session.
  `session_t0_changed(None)` means a new session is starting: `open()`
  emits it after its retiring refusal, before the new ring exists, and
  views drop old data only then. Views never rebuild their models on bus
  start or stop: Trace builds `TraceTreeModel` once and swaps rings with
  `set_rings`, which keeps a cursor only for the same ring object (a
  reopened bus's seq restarts at 0); Plot breaks the lines of a stopped or
  degraded bus with a NaN; `TeeLogger.set_rings` keeps one recording
  across bus loss and reconnects; Monitor and Data view dim stale values.
  Replay stops when its target bus stops, fails or degrades.
- **Controller signals are routed by identity.** `BusReader` tells
  controllers apart by `sender()`, never by bus name, and ignores any
  that is no longer registered, so a late signal can't act on a bus
  reopened under the same name. Connect them to bound slots: a lambda
  capturing the reader frees it mid-destruction when the controller is
  deleted. A close that misses its deadline leaves the controller
  *retiring* (`is_closing`, dock "closing"); `open()` refuses its name
  and channel until its thread exits and `bus_released` fires.
- **Delete a thread's owner only after the thread is joined.**
  `BusController.finished`, `TeeLogger.stopped` and
  `ReplayController.finished` fire after `QThread.finished` + `wait()`;
  `BusReader` `deleteLater`s a controller only from `finished`. Child
  views get no `closeEvent` on quit, so Record and Replay stop from
  `aboutToQuit`. Quit calls `bus_reader.shutdown()` and `os._exit`s only
  when a worker is still stuck (a running QThread can't be destroyed).
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
  `flush_tx_buffer()`, gs_usb `flush_tx_buffer()` (a controller restart);
  never IXXAT (its flush waits INFINITE), never a listen-only bus. Flush
  on master OFF, on every disarm and on scheduler shutdown only — not
  when a row stops. The worker also flushes by itself
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
  Kvaser −15/−26 → lost, gs_usb caused by an `OSError` (its in-bus probe)
  → lost; anything else stays fatal. SocketCAN and gs_usb error frames
  carry the controller state. A 0.5 s tick (it runs on an idle bus too)
  probes pcan `status()`, which is how a pulled PCAN-USB is noticed (`recv`
  only times out), and counts error frames (≥50/s → bus errors); pcan's
  error warning counts only while error frames arrive, since the
  controller holds it until it next transmits. Recovery needs two clean
  ticks, ties break probe > raised > SocketCAN state > rate, and
  `health(state, detail)` fires only on a change. A lost adapter raises
  into the normal failure path. Never add a sleep when `recv` returns
  `None`. `BusReader.health(name)` is `None` while opening, OK from
  `bus_started`; LOST outlives the failure until the bus is opened again,
  closed by name or forgotten. **Degraded means `health is not
  BusHealth.OK`**, one rule everywhere. Known gaps: Kvaser bus-off shows
  only through the error-frame rate (python-can doesn't wrap
  `canReadStatus`); an IXXAT bus in error keeps up to 16 stale queued
  frames (its flush waits INFINITE).
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
  supported set is deliberately narrow (`pcan`, `kvaser`, `gs_usb`,
  `socketcan`, `ixxat`) — every name in `_SUPPORTED_BACKENDS` is a
  vendor module the probe imports (python-can's
  `detect_available_configs(interfaces=[name])`, or `_detect_gs_usb`), so
  adding one is paying real startup cost. Each per-backend probe runs
  in a sub-thread with a hard `BACKEND_TIMEOUT_S` ceiling; a vendor
  module that hangs on import is reported via `backend_failed` and the
  prober moves on rather than wedging the dock. The `virtual` loopback
  is a dev-only surface: it's emitted by the prober and seeded in the
  Create-bus dialog **only** when `adapter_prober.dev_mode()` is on
  (`ITHILDIN_DEV` env / `--dev` launch flag). Normal users never see it;
  the test suite still uses the `virtual` backend directly (it stays a
  valid `SupportedInterface`).
- **gs_usb (CANable / CANtact / candleLight) goes through `GsUsbBus`.**
  `gs-usb`, `pyusb` and `libusb-package` (which bundles libusb) are hard
  dependencies. Open the adapter with `core/gs_usb_bus.py::GsUsbBus`,
  never `can.Bus(interface="gs_usb")`: it adds listen-only (refused when
  the firmware lacks it, since gs_usb drops the flag silently), an in-bus
  probe that raises "adapter lost" within ~1 s of an unplug (gs_usb's
  read swallows USB errors, so `recv` would spin), epoch timestamps across
  the device counter's 71.6 min wrap, a listen-only-safe shutdown and
  `flush_tx_buffer`. python-can can't enumerate gs_usb, so
  `adapter_prober._detect_gs_usb` scans on Windows only (on Linux the
  kernel driver owns the adapter: use SocketCAN) and reads no string
  descriptors — opening a WinUSB device is exclusive and would race a bus
  opening on it. Call `libusb_package.get_libusb1_backend()` before
  `GsUsb.scan()` so pyusb loads the bundled libusb. `BusConfig` migrates
  saved `cantact` buses (0.1.0 / 0.1.1) to `gs_usb`.
- **Kvaser channel numbers are re-enumerated under one lock.**
  `core/bus.py::kvaser_enum_hardware()` has CANlib re-enumerate (Windows
  only, via `canEnumHardwareEx`: the DLL doesn't export
  `canEnumHardware`), so an adapter plugged in after launch is found and
  a replugged one reopens. Re-enumerating renumbers the channels, so hold
  `KVASER_ENUM_LOCK` across it and every use of the numbers: the prober's
  detection and channel-data read, the worker's `can.Bus`. A vendor call
  that hangs keeps the lock, so never wait for it unbounded: a probe gives
  up after 2 s (below `BACKEND_TIMEOUT_S`, reported as driver busy rather
  than a timeout), an open after 10 s or once its bus is stopped. Kvaser
  buses open through `BusWorker._open_kvaser`: `accept_virtual=False` (a
  number shifted by an unplugged adapter must fail, not open a Kvaser
  Virtual channel, so the enricher hides Virtual channels by card type),
  and only CANlib preset bitrates, checked before any driver call
  (`KvaserBus` leaks an init-access handle when CANlib refuses one; the
  Create dialog offers no 800 kbit/s for Kvaser). Listen-only uses a
  single handle: python-can sets silent mode on its write handle, which
  has no init access.
- **PCAN buses open through `BusWorker._open_pcan`.** Only PCAN-Basic
  preset bitrates (`PCAN_BITRATES`), checked before any driver call:
  `PcanBus` runs any other rate at 500 kbit/s without a word (the Create
  dialog's default rates are all presets). An open that fails after
  `CAN_Initialize` initialised the channel — a later step failed, or
  Initialize returned CAUTION — uninitialises it, or every later open of
  it fails until a restart. Whether this open initialised it comes from
  `GetStatus` before and after (`_pcan_initialised`), never from the
  error: when Initialize itself fails, the channel may be another bus's
  in this process. Status frames are turned off after the open (`PcanBus`
  doesn't check the status-message type, so they can read as data frames),
  which leaves the health tick's `status()` as pcan's only bus-state
  source; `CAN_Reset` then empties the queues of those queued since
  Initialize (it leaves the controller alone). Listen-only is read back
  with `GetValue(PCAN_LISTEN_ONLY)` and the open refused unless the read
  succeeds and it is on: `PcanBus` ignores that `SetValue`'s result.
  `_finish_pcan_open` runs these steps; any failure shuts the bus down.
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
  frame), so a `pcan` / `kvaser` / `ixxat` / `gs_usb` config runs that
  backend's branches without importing a vendor module. `open_gate` /
  `block` hang the open or `recv` like a stuck vendor call until the
  event is set; set it before the test ends. Extend it rather than
  writing another fake. `GsUsbBus` itself runs against a fake `GsUsb`
  adapter in `tests/test_gs_usb_bus.py`, and `_open_pcan` runs
  python-can's own `PcanBus` against a fake PCAN-Basic driver
  (`tests/test_bus.py::_PcanBasic`).
- The conftest fails a test whose `make_app_context` reader still has a
  worker running after `shutdown()`.
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
| Add an MCP write tool | same split; the body runs inside `tools._audited` and goes through `AllowlistStore.check_*`; register an `async` wrapper in `mcp/server.py::register_write_tools` that calls it via `to_thread.run_sync` with the server's `sends` limiter |
| Wire a `BusReader` error display | `MainWindow.report()` and per-view banners |
| Add or re-enable a CAN backend | `ui/adapter_prober.py::_SUPPORTED_BACKENDS` + `workspace/schema.py::SupportedInterface` (must update both); for listen-only also `LISTEN_ONLY_INTERFACES` + `core/bus.py::_apply_listen_only_kwargs` |
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
