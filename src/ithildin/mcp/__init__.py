# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Built-in MCP server.

Exposes capture and (gated) transmit tools to AI agents over
streamable-HTTP on loopback, behind a bearer token. All send paths route
through ``permissions.py`` — no bypass.
"""
