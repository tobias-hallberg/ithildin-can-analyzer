# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""DBC-based decode helpers built on cantools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cantools
import cantools.database


class Decoder:
    """Wrap a cantools ``Database`` and expose lookup + decode."""

    def __init__(self, db: cantools.database.Database) -> None:
        self._db = db

    @classmethod
    def from_files(cls, *paths: Path | str) -> Decoder:
        db = cantools.database.Database()
        for p in paths:
            db.add_dbc_file(str(p))
        return cls(db)

    @property
    def database(self) -> cantools.database.Database:
        return self._db

    def message_name(self, arbitration_id: int) -> str | None:
        try:
            return self._db.get_message_by_frame_id(arbitration_id).name
        except KeyError:
            return None

    def has_message(self, arbitration_id: int) -> bool:
        try:
            self._db.get_message_by_frame_id(arbitration_id)
        except KeyError:
            return False
        return True

    def message_ids(self) -> set[int]:
        return {int(m.frame_id) for m in self._db.messages}

    def decode(
        self,
        arbitration_id: int,
        data: bytes,
        *,
        decode_choices: bool = True,
        allow_truncated: bool = True,
    ) -> dict[str, Any] | None:
        """Decode one frame's payload to ``{signal_name: value}`` or ``None``.

        Returns ``None`` for unknown IDs and for payloads cantools cannot
        decode (e.g. mismatched length when ``allow_truncated=False``).
        """
        try:
            msg = self._db.get_message_by_frame_id(arbitration_id)
        except KeyError:
            return None
        try:
            decoded = msg.decode(
                data,
                decode_choices=decode_choices,
                allow_truncated=allow_truncated,
            )
        except (ValueError, KeyError):
            return None
        # cantools returns a Sequence for container messages; we don't enable
        # container decoding, so a plain mapping is what we expect here.
        if not isinstance(decoded, dict):
            return None
        return dict(decoded)
