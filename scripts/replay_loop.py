# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Replay a CAN log file in a loop, forever.

Wraps :class:`can.MessageSync` so that when the log ends it starts
again. Mostly compatible with ``python -m can.player`` arguments —
``-i / -c / -b`` work the same — plus a ``--gap`` flag for the pause
between loop iterations.

The bus stays open between iterations, so the receiver sees one
continuous stream (with a short ``--gap`` pause) instead of a brief
disconnect from spawning a fresh process.

Example::

    uv run python scripts/replay_loop.py -i kvaser -c 1 -b 500000 \\
        examples/drive_5min.blf

Stop with Ctrl-C.
"""

from __future__ import annotations

import argparse
import sys
import time

import can


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("logfile", help="Path to the log file (.blf, .asc, .log, …)")
    p.add_argument(
        "-i", "--interface", default="kvaser", help="can.Bus interface (default: kvaser)"
    )
    p.add_argument("-c", "--channel", default="0", help="can.Bus channel (default: 0)")
    p.add_argument(
        "-b",
        "--bitrate",
        type=int,
        default=500000,
        help="can.Bus bitrate in bit/s (default: 500000)",
    )
    p.add_argument(
        "--ignore-timestamps",
        action="store_true",
        help="Send as fast as possible instead of pacing by the log's timestamps.",
    )
    p.add_argument(
        "-s",
        "--skip",
        type=float,
        default=60.0,
        help="Skip gaps longer than SKIP seconds within the "
        "log (default: 60). Has no effect with "
        "--ignore-timestamps.",
    )
    p.add_argument(
        "--gap",
        type=float,
        default=0.5,
        help="Pause this many seconds between loop iterations (default: 0.5).",
    )
    p.add_argument(
        "--error-frames", action="store_true", help="Also send error frames found in the log."
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    # python-can LogReader is a one-shot iterator, so we materialise the
    # whole file once and reuse the list for every loop iteration.
    with can.LogReader(args.logfile) as reader:
        messages = [m for m in reader if args.error_frames or not m.is_error_frame]
    if not messages:
        print(f"No messages found in {args.logfile}", file=sys.stderr)
        return 1

    print(
        f"Loaded {len(messages)} frames from {args.logfile}; "
        f"opening {args.interface} ch={args.channel} @ {args.bitrate} bps. "
        "Ctrl-C to stop."
    )

    bus = can.Bus(
        interface=args.interface,
        channel=args.channel,
        bitrate=args.bitrate,
    )
    iteration = 0
    try:
        while True:
            iteration += 1
            print(f"[loop {iteration}] sending {len(messages)} frames…")
            sync = can.MessageSync(
                messages=messages,
                timestamps=not args.ignore_timestamps,
                skip=args.skip,
            )
            for msg in sync:
                bus.send(msg)
            if args.gap > 0:
                time.sleep(args.gap)
    except KeyboardInterrupt:
        print("\nInterrupted. Shutting down bus.")
        return 0
    finally:
        bus.shutdown()


if __name__ == "__main__":
    sys.exit(main())
