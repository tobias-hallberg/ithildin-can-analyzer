# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Application-level singletons.

Owned by ``app.py``, shared across windows. Keeps the multi-window UI
loosely coupled — children subscribe to signals, not to each other.
"""
