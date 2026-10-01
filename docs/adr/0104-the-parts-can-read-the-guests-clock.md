# ADR 0104 — The board's parts can read the guest's clock

**Status:** Accepted — 2026-10-01.

## Context

Every part modelled in Python keeps time by the host's clock. Three ADRs have
already paid for that:

- ADR 0037: a LoRa packet's airtime is real seconds, not guest seconds, so on
  a slow guest the air runs faster than the firmware.
- ADR 0052: a satellite receiver streams a fix every second from power-on, and
  the L76K model could not. On a guest running slower than the host, one fix
  per host second floods the line, and the firmware's "wait for it to stop"
  step gave up. So the model says nothing until the firmware has spoken, which
  a real module does not do.
- ADR 0064: the WiFi access point's beacons had to be driven from a guest-clock
  timer in C, because "a beacon rate that drifted with host load would change
  the one thing every WiFi test depends on".

The next part is the T-Deck Max's u-blox receiver (ADR 0105). Its firmware
does nothing but listen, so it cannot be kept silent until spoken to. It has
to stream once per second, and that second has to be the board's.

QEMU's virtual clock is that second. It runs with the guest: slower than the
host when the guest is slow, and exactly the instruction count under
`--icount`.

## Decision

1. **`qemu-patch/esp_guest_clock.c`**, a device of ours with no registers. On a
   virtual-clock timer it writes the current virtual time, eight bytes
   little-endian in nanoseconds, to a Unix socket that Python listens on. It
   writes every 10 ms of guest time. If the socket is full it skips the tick
   rather than block the guest.

2. **`emu/guest_clock.py`** is the other end. `seconds()` gives the latest guest
   time heard, or `None` before the first tick.

3. **Both boards have one**, and a part that needs guest time is handed the
   clock when it is built. A part that is not handed one keeps the host's
   clock, so nothing changes until a part asks.

4. **Resolution is the tick.** A part sees guest time in 10 ms steps, which is
   finer than anything here needs: a fix a second, a beacon every 102.4 ms.

## Consequences

**Good**

- A Python part can keep the board's time. The receiver in ADR 0105 is the
  first to use it, and the L76K, the LoRa air and the BLE central can follow.

**Costs, accepted**

- One more device and one more socket per run, and a hundred eight-byte
  writes per guest second.
- Guest time reaches Python late by up to a tick plus the socket. A part
  cannot act *at* a guest instant from Python. Anything that needs that stays
  in C, as the access point's tick does.
