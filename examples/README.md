# Bench-test examples

## `drive_5min.blf`

5 minutes (300 s, 55 500 frames) of synthetic EV-drivetrain traffic
encoded against `can_database.dbc` at the repo root:

| ID    | Message           | Period | Sender |
|-------|-------------------|--------|--------|
| 0x118 | `JSC_Status1`     |  40 ms | JSC    |
| 0x200 | `Motor_Status`    |  10 ms | MCU    |
| 0x201 | `Battery_Status`  | 100 ms | BMS    |
| 0x202 | `Vehicle_Control` |  20 ms | VCU    |

The drive cycle covers key-on, accel to 60 km/h, cruise, regen-brake to
30 km/h, Sport-mode accel to 80 km/h, regen down to 0, and key-off.
Useful for exercising decode, enum rendering, plotting, and the trace
window without a vehicle attached.

The log is a checked-in fixture; the generator script that produced it is
not part of this repository.

## Bench replay over Kvaser virtual channels

Both ithildin and the replayer talk to the same Kvaser driver; the
virtual-channels feature pairs channel 0 ↔ channel 1, so frames sent on
one are received on the other.

1. **Enable Kvaser virtual channels.** In *Kvaser Hardware* set the
   number of virtual channels to ≥ 2 (default install gives you two).
2. **Start ithildin on channel 0:**

   ```powershell
   uv run ithildin
   ```

   Press **+ Bus** in the Buses panel, pick `kvaser`, channel `0`, bitrate
   `500000`, then **Connect**.
3. **Replay the log on channel 1** (separate terminal). All options
   must come *before* the positional log path — `can.player` treats
   anything after the file as backend `key=value` kwargs:

   ```powershell
   uv run python -m can.player -i kvaser -c 1 -b 500000 examples\drive_5min.blf
   ```

   `can.player` honors the original timestamps, so playback lasts the
   full 5 minutes. Useful flags:

   - `--ignore-timestamps` — replay as fast as possible
   - `-s 60` — skip gaps longer than 60 s (collapses idle periods)
   - `--error-frames` — also send any error frames in the log

4. **In ithildin**, select the bus in the Buses panel, press **+ DBC**,
   and load `can_database.dbc`. Open a Data view on `Vehicle_Control` to
   see `GearPosition`, `DriveMode`, and `HVKeyState` transition through
   their named values. Open a Signal Plot on `Motor_Status::MotorTorque`
   to see positive peaks during accel windows and negative peaks during
   the two regen windows (~120–135 s, ~240–280 s).

## Without hardware

If you don't have Kvaser drivers installed, you can still exercise the
parse + decode path via the in-process Replay window:

1. Start ithildin with `examples/demo.itp` (it pre-declares a `virtual`
   bus) and press **Connect** in the toolbar. Or launch with `--dev` to
   get the virtual loopback as a device in the **+ Bus** dialog.
2. Open the Replay window, point it at `examples/drive_5min.blf`, press
   play. Frames get routed through the in-process `BusReader` and reach
   every open view.

This won't exercise the real driver path but is enough for UI work.
