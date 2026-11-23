# ADR 0178 — The guest counts its time in instructions by default

**Status:** Accepted — 2026-11-23. Decided with the product's owner (the
product interview of that day, Q15). Revises ADR 0027's default; ADR 0161's
core turn now applies only to the host's clock.

## Context

Without `-icount` the guest's clock is the host's, while its code runs
several times slower than the chip's. Firmware that keeps a deadline then
misses it whenever the host holds the emulator or the guest falls behind,
and two such failures of Meshtastic were reproduced on demand (ADR 0141):

- RadioLib's one-second wait for BUSY times out, `err=-705`, and the
  firmware asserts and restarts;
- a transmission's end arrives before Meshtastic has enabled the edge it
  waits for, is lost for good, and sixty seconds later the firmware
  declares a hardware failure and restarts.

Under `-icount` neither can happen: a pause on the host is no time to the
guest. Bluetooth already needed it (ADR 0157). Measured the same day,
Meshtastic's interface was configured in 28.2, 30.3 and 28.2 s of wall time
under `-icount`, against 20.2, 13.1 and 15.0 s on the host's clock: about
1.8 times slower, and steadier.

## Decision

1. **Every run counts the guest's time in instructions** unless asked not
   to: `run.py`, devices from the page and `tools/device.py`, and the
   commands built on them. `--icount` is still accepted and changes
   nothing.

2. **`--host-clock` asks for the host's clock**: faster, less faithful. It
   is a run setting a device keeps (ADR 0145), and the page's settings
   offer it under that description.

3. **What measures the host's clock asks for it**: `tools/speed.sh`, whose
   milestones are wall time.

## Consequences

**Good**

- A firmware's own timeline is a board's by default, and the reproduced
  failures of ADR 0141 cannot occur.
- Runs repeat: the same boot reaches the same places at the same guest
  times.

**Costs, accepted**

- Boots take about 1.8 times as long in wall time.
- ADR 0161's one-millisecond core turn, which applies only without
  `-icount`, no longer speeds up the default run.
