# Ithildin — Design

> **About this document.** This is the design-phase index carried over from the
> React/HTML prototype that preceded this codebase. The HTML files, `src/*.jsx`
> modules, and `design_handoff_*/` folders it refers to live in that prototype, not
> here — the PySide6 implementation is under `src/ithildin/`, and `CLAUDE.md` at the
> repo root documents the architecture as built. Where the two disagree (for example
> the light theme, which the prototype scoped out of v1 and the app now ships), the
> code and `CLAUDE.md` are current; this file records the design intent and rationale.

> An open-source CAN bus analyser. A slate, dark-first, density-heavy IDE for embedded /
> automotive engineers. Designed to be implemented in **Python + Qt 6 / PySide6**; everything
> described below is a high-fidelity React/HTML prototype — not production code.

This document is the design index for the project. It explains where each artefact lives,
what design decisions are settled, and what's still in flux. For per-surface implementation
specs (Qt class mapping, persistence schemas, drag-and-drop contracts), read the handoff
READMEs linked from each section.

---

## 1 · The product, in one screen

Single main window. Slim **Navigator** on the left (project tree, capture controls, status).
A **canvas** on the right where the user opens any number of analysis sub-windows: Trace,
Signal Plot, Signal Monitor, Data View, TX, MCP, Frame Inspector. Sub-windows are MDI
children — drag, resize, minimise, maximise, close. A bottom **tray** lists every open one.

The whole product is built around one verb: **drag a signal or message from the tree
onto a view-launcher button** → a view of that type spawns, seeded with what you dropped.

> Note: the older `Ithildin.html` explored a *multi-window* model (separate OS windows
> per view). That direction is **superseded** by the single-window MDI model in
> `Ithildin single-window.html`. Keep the file for context; don't implement from it.

---

## 2 · File map

### Canonical surfaces (build to these)

| File | What it is |
|---|---|
| `Ithildin single-window.html` | **Canonical app shell.** Navigator + MDI canvas + tray + status. The reference for everything that is not its own dedicated handoff. |
| `Signal plot - chosen design.html` | **Canonical signal plot** — tiered left Y-axes, dedicated left signal list pane, crosshair, fit/dbc modes. |
| `Signal plot - stacked panes.html` | **Evolution of the chosen plot** — same primitives, now multiple stacked panes sharing one time axis. Group-by `system` / `unit` / `custom`. Currently in active iteration (see §8). |
| `TX window.html` | **Canonical Transmit window** — two-tier control (TX Master + per-row active), two-pane layout, per-signal-type controls. |
| `Create CAN bus dialog.html` | **Canonical bus-creation modal** — device picker, channel pills, FD switch, listen-only safety panel, advanced timing. |

### Reference / superseded

| File | Status |
|---|---|
| `Ithildin.html` | Multi-window exploration. Reference for child-window catalogue. **Do not implement.** |
| `Ithildin v1 single-window.html` | First single-window draft (tab-based). Superseded by the MDI version. |
| `Signal plot - multi-y axis options.html` | Side-by-side of the four rejected approaches (stacked rows, dual axis, grouped-by-unit, normalised overlay). Keep for rationale. |
| `Y-axis unit treatments.html` | Three considered treatments for the unit label. Picked option 1 (unit chip in header strip). |
| `TX window explorations.html` | Four TX layouts compared. User picked two-pane. |
| `Create CAN bus.html` | Modal vs. two-pane-window comparison. User picked the modal. |
| `Icon explorations.html`, `Icon variants.html` | Brand mark + app icon studies. Outputs in `Ithildin icons/` and the project root `.ico`/`.png` files. |

### Source modules (`src/`)

Each module is consumed by one or more of the canonical HTML files via `<script type="text/babel" src="…">`. They are organised by feature, not by app shell.

| Module | Owns |
|---|---|
| `tokens.css` | **Single source of truth** for colours, type, density. Mirror to Qt as `QPalette` + `QSS`. |
| `chrome.jsx` | `TitleBar`, `MenuBar`, `ToolbarBtn`, `ToolbarSep`, dock primitives, status-bar primitives. |
| `icons.jsx` | Inline SVG icon set. 16px viewBox, 1.25px stroke, currentColor. No emoji. |
| `data.js` | Mock DBC, `fmtHex`, `useTraceStream`. **Mock only** — replace with real DBC-backed model. |
| `single-window.jsx` | The MDI canvas, sub-window chrome, view-launcher dropdowns, drop handling. |
| `multi-window.jsx`, `main-window.jsx`, `detail-screens.jsx`, `spec.jsx` | The earlier multi-window exploration. Reference only. |
| `trace-view.jsx` | Trace list (scrolling / paused / aggregated). |
| `signal-plot.jsx` | Current in-codebase plot (the one being replaced — see §8). |
| `signal-tree.jsx` | Project tree leaves (signals + messages) used in the left dock. |
| `frame-inspector.jsx` | Per-frame decoded-signal panel. |
| `mcp-dock.jsx` | MCP allowlist surface + audit log. |
| `dialogs.jsx` | Older `ConnectBusDialog`. **Replaced** by the Create CAN bus modal (see §7). |
| `tx-shared.jsx`, `tx-controls.jsx`, `tx-window-twopane.jsx`, `tx-variations.jsx` | TX window — shared models, signal-type controls, the canonical two-pane window, and earlier variations. |
| `cb-controls.jsx`, `cb-variations.jsx` | Create-bus modal — form widgets + variation hook. |

### Handoff bundles

Self-contained packages for the engineering team. Each carries its own README that goes deep on Qt class mapping, persistence, drag/drop contracts, and open questions.

| Folder | Subject |
|---|---|
| `design_handoff_ithildin/` | App shell, window model, design tokens, view-type catalogue. Read this first. |
| `design_handoff_signal_plot/` | Multi-Y signal plot (chosen design). |
| `design_handoff_tx_window/` | Transmit window. |
| `design_handoff_create_can_bus/` | Create CAN bus dialog. |

---

## 3 · Design system

### 3.1 · Surfaces (dark)

| Token | Use | Approx hex |
|---|---|---|
| `--bg-0` | Window background | `#1f2933` |
| `--bg-1` | Panel / dock surface | `#252e38` |
| `--bg-2` | Inputs / hover | `#2a343f` |
| `--bg-3` | Active hover | `#324050` |
| `--bg-sel` | Selected row | accent-tinted |
| `--bg-titlebar` | Window titlebar | darker than `bg-0` |

### 3.2 · Text & borders

`--text-0` primary · `--text-1` body · `--text-2` secondary / labels · `--text-3` muted / micro
`--border-0` hairline · `--border-1` panel edge · `--border-2` focused

### 3.3 · Accent — the moonlight

`--accent` = `oklch(0.82 0.10 195)` (a silvered cyan). Used **sparingly**: focused window
border, crosshair, focus rings, MCP "live" indicator, the Navigator status bar fill, the
About orb. The rest of the UI stays restrained slate.

### 3.4 · Status hues

`--ok` (live / allowed) · `--warn` (rate-limited / amber safety) · `--err` (denied / record dot) · `--info` (neutral notice) · `--rx` / `--tx` for trace direction.

### 3.5 · Plot signal palette

`--sig-0` … `--sig-5`. Six perceptually-spaced hues, assigned in insertion order.

### 3.6 · Type

| Family | Use |
|---|---|
| **Inter Tight** | UI chrome. 13px base, 12px dense rows, 11px micro labels, 10px header captions. |
| **JetBrains Mono** | Every number, every signal name, every axis tick, every hex ID, every trace cell. With `font-variant-numeric: tabular-nums`. |

Both loaded from Google Fonts at the top of every HTML file.

### 3.7 · Density

| Surface | Row height |
|---|---|
| Trace row | 20px (mono 12px, 8px horizontal padding) |
| Tree row | 20px |
| Toolbar | 26–30px |
| Sub-window titlebar | 28px |
| Navigator status bar | 22px |

Padding inside rows is `0 8px` or `0 10px`. Buttons sit on a 4px subgrid.

### 3.8 · Iconography

14px in 16-viewBox, 1.25px stroke, `currentColor`. No emoji, no filled glyphs unless the
fill carries semantic weight (the record dot, the moonlight orb).

### 3.9 · Light theme

`tokens.css` ships a `[data-theme="light"]` block. **Out of scope for v1** — the
implementation team builds dark only. Light is present so explorations can preview it.

---

## 4 · App shell (single window, MDI)

Implementation reference: **`design_handoff_ithildin/README.md`**.

The shape:

- **`QMainWindow`** with title bar, menu bar, toolbar (view launchers + capture controls), left dock, MDI canvas, window tray, status bar.
- **Left dock** (`QDockWidget`): filter input + four collapsible accordions — Signals, Messages, Databases, Interfaces. Each leaf is a drag source with MIME `application/x-ithildin-signal` carrying a JSON payload `{ kind, message, messageId, signal, unit }`.
- **MDI canvas** (`QMdiArea` in `SubWindowView` mode): hosts `QMdiSubWindow` children. Active sub-window gets the accent border + elevated shadow. New sub-windows cascade by 28px.
- **Window tray** below the canvas (custom widget, not `QMdiArea`'s tabbed view): one button per open sub-window, including minimised ones. Click to focus / restore.
- **Status bar** (`QStatusBar`): bus state on the left (`🔌 CAN1 · 500k · LIVE`), MCP state on the right (`🤖 MCP ● · 2 agents`).

### 4.1 · The drop matrix (see handoff §5 for the full table)

The view-launcher buttons in the toolbar are themselves drop targets. Drop a signal on the Plot launcher → spawn a new plot seeded with that signal. Drop a message → spawn seeded with **all** of that message's signals (or, for Trace, seeded with an ID filter).

This is the most important interaction in the product. Engineering must implement the MIME contract exactly as specified.

---

## 5 · View kinds

Six analysis surfaces. Each is a `QMdiSubWindow` child.

| Kind | Status | Doc |
|---|---|---|
| **Trace** (`trace`) | Mocked in `src/trace-view.jsx`. Modes: scrolling, paused, aggregated. | App handoff §8.1 |
| **Signal Plot** (`plot`) | Active iteration — see §8 below. | `design_handoff_signal_plot/README.md` |
| **Signal Monitor** (`monitor`) | Mocked. Live value table with min/max + range bar. | App handoff §8.3 |
| **Data View** (`dataview`) | Mocked. Spreadsheet of recent samples, copy-to-TSV. | App handoff §8.4 |
| **TX (Transmit)** | Fully specced. | `design_handoff_tx_window/README.md` |
| **MCP allowlist** | Mocked in `src/mcp-dock.jsx`. Single-instance. | App handoff (child catalogue) |
| **Frame Inspector** | Mocked. Pinned to the active trace selection. | App handoff (child catalogue) |

---

## 6 · Signal Plot — current state

The chosen design (`Signal plot - chosen design.html`) is a **single pane** with N tiered Y-axes on the left edge, one per signal. Key primitives:

- **Two-column layout**: 268px signal list pane on the left, plot canvas on the right.
- **Tiered Y-axes**: each visible signal gets its own 56px axis column. Spine and ticks are coloured to the signal. **The only place the unit appears is a small chip in a 22px header strip** above the canvas — no separate unit label competing with the topmost tick number.
- **Cursor crosshair**: vertical hairline + per-trace dots + a time pill below the time axis. The left pane's "live value" column swaps to the value-at-cursor while the cursor is over the canvas.
- **`fit` vs. `dbc`** Y-range modes in the toolbar — radio, mutually exclusive.
- **Selection**: clicking a row in the left pane emphasises that trace and its axis; everything else dims.

Engineering target: `design_handoff_signal_plot/README.md`. Preserves the existing `<SignalPlot plottedSignals running onRemove …/>` API; only the render is replaced.

---

## 7 · TX window — current state

The Transmit window is fully specced (`design_handoff_tx_window/README.md`). The two
decisions that define it:

1. **Two-tier control**. A single **TX Master** at the top is the bus gate. Each row has
   its own `active` flag. `transmitting = master.ON && row.active`. Toggling master OFF
   doesn't clear per-row flags — resume is one click. Status bar background goes accent
   green when master is ON. This is the loudest signal in the window.
2. **Signal-type → control dispatch**. Every signal renders the right control for its
   kind: `bool` → toggle, ≤4-value `enum` → segmented, larger → dropdown, small range →
   slider + readout, large range → stepper, signed → centered slider with zero detent,
   raw bytes → hex grid, counters/CRC → read-only `auto` badge. The dispatcher lives in
   `TxControl` in `src/tx-controls.jsx`.

Deferred: lock / arm for critical signals, send-on-change, burst, macros, scheduling
visualisation. Reserved 24px right-margin on every signal row for a future padlock.

---

## 8 · Active iterations

Where the design is still moving.

### 8.1 · Stacked-pane signal plot

`Signal plot - stacked panes.html` is the evolution of the chosen plot: same tiered-axis
primitive, but composable. One pane is one row of plot canvas with 1–N tiered Y-axes;
multiple panes stack vertically and share **one** time axis at the bottom. The left list
groups signals by pane.

Decisions made:

- **Group-by** selector in the toolbar — `system`, `unit`, `custom`. Default is `system` (sensible 2 panes for EV/CAN: electrical, mechanical/thermal).
- Each pane gets its own 22px unit-chip header strip.
- Only the bottom pane paints time tick labels.
- 1px horizontal divider between panes is a drag-handle for vertical resize.
- Crosshair runs through every pane; per-trace dots show in each pane the cursor crosses.

Open: **the "merge Y-axes into one"** affordance when every signal in a pane shares a unit.
Six placements are under consideration (pane-local toolbar button, click-the-unit-chip,
inline contextual pill, axis-edge target, global toolbar, right-click). Current lean:
click-the-chip primary, with a small toolbar button as the discoverable fallback.

### 8.2 · Brand mark / app icon

`Icon explorations.html` and `Icon variants.html` are studies for the Ithildin mark — the
moonlight-cyan disc/ring on dark slate. Variants are checked into the project root
(`Ithildin icon.png`, `Ithildin icon light.png`, `Ithildin icon mono on dark.png`,
`Ithildin icon mono on light.png`, plus matching `.ico` files). No outstanding decisions;
keep as the brand palette evolves.

---

## 9 · Implementation notes

### 9.1 · React/HTML is reference only

Every prototype here is built as React + Babel inline. **Do not port the React code.** The
component decomposition, naming, and inline styles are a communication tool, not an
architecture spec. Implement in idiomatic PySide6 (signals/slots, `QAbstractItemModel`,
custom delegates, stylesheet-driven theming).

### 9.2 · `tokens.css` is the truth

Lift the values directly. The dark palette is in `oklch()` — convert to sRGB hex for Qt.
Build a `QPalette` plus a generated QSS stylesheet from the tokens; do not hardcode
colours in widget code.

### 9.3 · Singletons

`BusReader` (the bus IO thread), `Database` (parsed DBCs), `MCPServer`, and
`WindowManager` are application-level singletons. Child surfaces subscribe to their
signals (`frames_received`, `database_changed`, etc.). Children must not hold references
to each other.

### 9.4 · Persistence

Per project (`~/.ithildin/<project>/`): `layout.json` (open windows + geometry),
`allowlist.toml` (MCP rules), `audit.jsonl` (MCP audit, rotated at 50MB). Per user
(`QSettings`): recent DBCs, theme, window-size defaults. A TX setup is a saveable
artefact and should live inside the project file (`*.itp`).

### 9.5 · Out of scope for v1

- Light theme
- Internationalisation
- Touch / tablet
- Multi-bus simultaneous (CAN1 + CAN2 at once) — under review
- The multi-window app shell

---

## 10 · Open questions (collected)

Carried from the handoff READMEs; park against the project owner before implementation.

**App shell**

1. MDI sub-window geometry — remember per-project, per-DBC, or per-session?
2. When a DBC reloads with edits, do open Plot/Monitor instances re-resolve by signal *name* or by stable signal-ID?
3. Trace ring buffer — what's the real upper bound?
4. MCP allowlist — per-project or global persistence?

**Signal plot (stacked panes)**

5. Where does the merge-Y-axes control live? (See §8.1.)
6. Pane reorder UX — drag the divider, or a dedicated handle?

**TX window**

7. Scheduler minimum reliable period?
8. Bus-load: queued TX too, or observed only?
9. Master-OFF + click "send" on one-shot — queue, or refuse with toast?
10. New transmits default `active = false` (safe) or `true` (workflow)?

**Create CAN bus**

11. `Create` (save only) — profile shows as `disconnected` in the bus list, or hidden until first connect?
12. Two profiles on the same physical channel — allow or disallow at creation?
13. Listen-only default for new users — ON or OFF?
14. CAN FD on non-FD device — refuse, warn-and-allow (current), or fall back to Classic silently?

---

*Designed for Mint Engineering · 2026*
