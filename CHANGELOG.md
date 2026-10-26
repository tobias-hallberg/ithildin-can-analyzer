# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.

## [Unreleased]

## [0.1.2] - 2026-10-26

### Added

- Every launch writes a diagnostic log to `~/.ithildin/logs` (the newest
  ten are kept): unhandled errors from any thread with their tracebacks,
  Qt's own messages, a hard crash's stack, and every thread's stack when
  the window stops responding for 5 s. Errors with no other surface also
  appear in the Report panel. Attach the log of the failing run to a bug
  report.
- Bus health. The status bar and the Buses panel show each bus as
  connecting, live, bus errors (with the reason), bus-off or adapter lost,
  and the Report panel notes the changes. The Trace window shows a banner
  for bus errors and bus-off, with **Reconnect** on bus-off. A pulled
  PCAN-USB is noticed within about a second instead of showing as a live
  bus with nothing arriving.
- The Trace counts frames the capture buffer overwrote before it read them
  as LOST. A recording counts the frames it lost the same way, and says so
  in the Report panel when it stops.
- After the window stalls, plots, monitors and data views skip the
  backlog instead of catching up on it, and the Report panel says how many
  frames they skipped on which bus.
- Buses follow their adapter. A bus created from the Create CAN bus dialog
  is pinned to the adapter picked there (a PCAN adapter by the device ID
  set in PCAN-View, Kvaser and IXXAT by serial, `gs_usb` by USB serial),
  and opens it in whichever port or channel it shows up on; if it is
  missing the bus says so instead of opening another adapter. An existing
  bus is pinned from its right-click menu (**Pin to …**). Pins are stored
  per user in `~/.ithildin/<project>/adapter_pins.json`, never in the
  project file.
- The Buses panel rescans the adapters when a device is plugged in or
  removed (Windows), or on its new **↻** button, without the rows
  flashing. When the adapter of a lost bus comes back, the Report panel
  says so; the bus reconnects only when you click it.
- A bus's status dot in the Buses panel connects or disconnects it, and
  the right-click menu offers Connect, Reconnect and Disconnect. A bus
  that failed shows ⊗ with the reason in its tooltip, apart from one you
  disconnected.
- The Trace banner keeps every failed bus, not only the latest: it names
  the newest with "(+N more)", and **Reconnect all** reopens them all.

### Changed

- TX Master turns itself off, with an amber banner in the Transmit window
  and a Report entry, when a bus an active row targets closes, is lost,
  gets bus errors or goes bus-off, when an active row targets a
  listen-only bus, or when a periodic send fails. It never turns back on
  by itself: switch it on again. Switching it on is refused while an
  active row's bus is closed, still connecting or listen-only. Frames
  still queued in the adapter are discarded when it turns off, so they
  don't burst out after a replug.
- Error frames are sampled: at most about ten per second per bus reach the
  Trace and recordings, each standing for the N it replaces. The Trace
  names one "Error frame ×N", sums the N in ERR and, in Latest mode, gives
  each bus's error frames a row of their own. Recordings hold only the
  sampled error frames, without the count. Error frames are never decoded
  as DBC messages, and only a whole-bus subscription shows them.
- A running Trace in Stream mode shows the newest 1000 rows; pausing shows
  the whole history. Resuming skips the frames that arrived while paused.
- Losing a bus no longer ends the capture session; only closing the last
  open bus does. The Trace, plots and recordings keep their data through
  a bus loss and reconnect, a recording keeps one file across a
  Disconnect and Connect, a plot shows the outage as a gap, and the Signal
  Monitor and Data view dim values that went stale.
- Replay starts only on a live bus, and stops when its bus disconnects, is
  lost or degrades. Stop acts at once.
- Open Project and New Project close the running buses, and removing a bus
  from the Buses panel closes it.
- python-can 4.6.1 or later and anyio 4.2 or later are required.
- CANable / CANtact / candleLight adapters run on python-can's `gs_usb`
  backend instead of `cantact`, as a regular dependency (with libusb
  bundled), so the project is no longer capped at Python 3.12. A project
  saved with a `cantact` bus opens with that bus moved to `gs_usb`, and is
  written back that way on the next save. On Windows the adapters are
  found by a USB scan; on Linux use SocketCAN. Listen-only refuses to open
  on firmware without a listen-only mode instead of ACKing on the wire.
- Kvaser Virtual CAN Driver channels are no longer supported: they aren't
  listed, and a project bus bound to one fails to open. Create the bus
  again on a physical Kvaser channel.
- MCP audit lines record what happened to the frame (`outcome`: SENT,
  SEND_FAILED, NOT_SENT, DRY_RUN or UNKNOWN) next to the allowlist's
  `decision`. A send the driver refused is now ALLOW + SEND_FAILED (shown
  in the warning colour) rather than a DENY; look for failures by
  `outcome`.
- An MCP send that names no `bus` while none or several buses are open,
  or names a bus that isn't open, is refused with `NO_BUS` before the
  allowlist is asked, instead of matching a rule scoped to any one bus or
  failing as `BUS_ERROR`.
- MCP sends to a bus still connecting, with bus errors, bus-off or a lost
  adapter are refused with `BUS_UNHEALTHY`. `list_buses` reports
  `opening`, `live`, `errors` or `bus_off`, with a `status_detail`,
  instead of always `live`; a bus whose adapter is lost fails and drops
  out of it.

### Fixed

- The Trace window no longer slows down as its history grows, or freezes
  under an error-frame flood such as a PCAN adapter's on a bus with no
  other node. An expanded Latest row no longer crashes the app once its
  first frame has scrolled out of the capture buffer.
- A periodic TX frame no longer stops silently while the Transmit window
  still shows it transmitting, and no longer goes on after TX Master is
  switched off. A changed period takes effect, the same ID can be sent on
  two buses, and stopping an IXXAT periodic send no longer raises. One-shot
  sends no longer hold up the window.
- A PCAN or IXXAT bus that goes error passive or bus-off keeps capturing
  instead of being torn down.
- Disconnecting and quitting no longer freeze or crash on an adapter that
  hangs. A bus whose adapter hasn't let go of the channel shows as closing
  until it does, and can't be reopened meanwhile.
- Quitting while recording writes the file out in full, however long an
  `.mf4` takes to save, also for a recording whose window was closed
  first; a running replay stops.
- The MCP server starts in the windowed build, which has no console.
- The Trace's bus-off banner, with its Reconnect, comes back once a
  failure or error banner that covered it goes away.
- A slow adapter scan no longer reads as a pinned adapter missing, and a
  hung one no longer keeps the app from exiting.
- A CANtact-family bus created from the UI opens (the `cantact` backend
  was handed its `ch:N` channel name as the device index).
- Receiving or transmitting on a CANtact-family adapter no longer freezes
  the whole UI while the adapter waits.
- An unplugged `gs_usb` adapter is reported as lost within about a second,
  and its frames carry wall-clock timestamps like every other backend's.
- A listen-only Kvaser bus no longer ACKs on the wire: silent mode was set
  on a second handle that couldn't apply it.
- A Kvaser adapter plugged in, or plugged back in, after launch is found and
  opens without restarting the app.
- A Kvaser bus whose adapter is missing fails to open instead of opening a
  Kvaser Virtual channel in its place.
- A Kvaser bus refuses a bitrate CANlib has no preset for instead of
  leaving the channel half-open, and the Create CAN bus dialog no longer
  offers 800 kbit/s for Kvaser adapters.
- A PCAN bus whose open failed partway, for instance on an older
  PCAN-Basic driver, no longer leaves the channel held: the next open works
  without restarting the app.
- A PCAN bus refuses a bitrate PCAN-Basic has no preset for instead of
  running at 500 kbit/s.
- PCAN status messages no longer show up as data frames in the Trace,
  recordings and decoded signals.
- A listen-only PCAN bus refuses to open when the adapter doesn't enter
  listen-only mode, instead of ACKing on the wire.
- An MCP send stuck in the driver no longer stalls the other MCP tools or
  stopping the MCP server, and every send attempt is audited, including
  one that fails with an unexpected error.
- The MCP dialog opens quickly on a large audit log: it reads only the last
  2 MB.
- A second IXXAT adapter is listed: its channel 0 no longer hides behind
  the first adapter's. Two buses still can't share a channel number, even
  pinned to different adapters, so that channel 0 can't get a bus while
  another bus has channel 0.
- A backend whose detection hangs is probed once per session, not again on
  every rescan or Create dialog ↻, each of which left another thread hung.
- The Create CAN bus dialog labels a PCAN adapter by its device ID instead
  of calling it a serial number, and leaves out the factory-default ID.

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

[Unreleased]: https://github.com/tobias-hallberg/ithildin-can-analyzer/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/tobias-hallberg/ithildin-can-analyzer/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/tobias-hallberg/ithildin-can-analyzer/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tobias-hallberg/ithildin-can-analyzer/releases/tag/v0.1.0
