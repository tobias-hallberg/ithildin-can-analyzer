# Security Policy

Ithildin can **transmit CAN frames onto a real vehicle or bench bus**,
including under the control of an AI agent through the built-in MCP
server. Sending malformed or unexpected frames onto a live automotive bus
can have real, physical consequences. This document describes the safety
model so you can decide how to deploy it, and how to report issues.

## Reporting a vulnerability

Please report security issues **privately**, not as public issues:

- Preferred: open a [GitHub Security Advisory](https://github.com/tobias-hallberg/ithildin-can-analyzer/security/advisories/new).
- Or email **tobias@mint-engineering.se**.

Include the version, platform, and enough detail to reproduce. We aim to
acknowledge within a few days. Please give us a reasonable window to ship
a fix before disclosing publicly.

## The MCP send model

Every agent-initiated send — `send_signal` and `send_frame` — passes
through a single chokepoint (`AllowlistStore`) before anything reaches the
bus. There is no bypass path.

What the layer **guarantees**:

- **Default-deny allowlist.** A send is rejected (`NO_RULE`) unless it
  matches an explicit `AllowRule` for that CAN ID. Raw `send_frame`
  additionally requires a *wildcard* (`*`) rule for the ID — a
  single-signal rule does not authorise raw frames. A rule scoped to a bus
  matches only sends to that bus.
- **Explicit target bus.** A send must name its bus unless exactly one bus
  is open; otherwise it is rejected (`NO_BUS`) before the allowlist is
  asked.
- **DBC range enforcement.** For `send_signal`, when the rule has
  `enforce_signal_ranges` set, the value must lie within the signal's
  DBC-declared min/max, or it is rejected (`OUT_OF_RANGE`). Non-finite
  values (NaN/±inf) are rejected (`BAD_VALUE`).
- **Frame validation.** `send_frame` rejects out-of-range CAN IDs
  (`BAD_ID`: 11-bit standard / 29-bit extended) and payloads over 8 bytes
  (`BAD_LENGTH`, classic CAN only in v0.1).
- **Per-rule rate limiting.** Each rule may set `rate_hz`; a token bucket
  caps the send rate (`RATE_LIMITED`).
- **Listen-only override.** If the bus was opened listen-only, every send
  is rejected (`BUS_READ_ONLY`) regardless of the allowlist.
- **Degraded-bus override.** While the bus reports bus errors, is bus-off
  or has lost its adapter, every send is rejected (`BUS_UNHEALTHY`)
  regardless of the allowlist.
- **Full audit trail.** Every attempt — allowed *and* denied — is appended
  to `~/.ithildin/<project>/audit.jsonl` (0o600) with a timestamp, the
  agent name, the arguments, the target bus, the decision, what happened
  to the frame (`outcome`), and the matched rule. The MCP dialog tails it
  live, and the `get_audit_log` read tool exposes the same trail to the
  agent so it can see why it was denied.
- **Bearer-token transport auth.** The HTTP transport requires an
  `Authorization: Bearer <token>` header. The token is generated on first
  run and stored at `~/.ithildin/<project>/mcp_token` (0o600) — **never**
  in the shareable `.itp` project file. Rotate it by deleting that file;
  a new one is minted on next launch.
- **Loopback-only bind.** `mcp.host` is validated to a loopback address
  (`127.0.0.1` / `localhost` / `::1`). A non-loopback bind is rejected at
  project load.

What the layer **does not** do — your responsibility:

- **No transport encryption.** The HTTP transport is plaintext. The bearer
  token gates access but does not encrypt traffic. **Do not expose the
  port** beyond the local machine, and do not port-forward or tunnel it to
  an untrusted network.
- **No protection against a mis-scoped allowlist.** A rule you add is a
  grant you made. A wildcard rule on a safety-relevant CAN ID lets an
  agent send arbitrary payloads to it. Scope rules narrowly and set
  `rate_hz`.
- **No semantic safety.** The allowlist authorises *who may send what*; it
  cannot know whether a permitted-but-unwise frame is dangerous in your
  vehicle's current state.
- **`agent_filter` is not authentication.** The `agent` name on a send is
  self-declared by the caller, so a rule scoped to one agent name only
  separates well-behaved agents from each other. Anyone holding the bearer
  token can claim any name. Scope rules by CAN ID and signal, not by agent.

## Deployment recommendations

- Run only on trusted machines. Treat the MCP port as a local-only,
  developer-facing surface.
- When exploring a new bus or a new agent, open the bus **listen-only**
  and/or set `dry_run = true` under `[mcp]` in the `.itp` so sends are
  authorised, range-checked, and audited but never actually put on the
  wire. The tool result reports `DRY_RUN` so the agent knows.
- The server does not start on its own unless `[mcp] enabled = true` is
  set in the project. Leaving it off means the port only opens when
  someone presses **Start** in the MCP dialog.
- Start from an empty allowlist and add the narrowest rules you need, with
  `rate_hz` set. Prefer per-signal rules over wildcard rules.
- Protect `~/.ithildin/` with your OS file permissions — it holds the
  allowlist, the audit log, and the MCP token.
- Review the audit log after agent sessions.

## Supported versions

Ithildin is pre-1.0. Security fixes land on the latest release; there is
no long-term support branch yet.
