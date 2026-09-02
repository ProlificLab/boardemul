# ADR 0025 — The link-layer core's clock and timers run in C, on QEMU's clock

**Status:** Accepted — 2026-09-02. Continues ADR 0024.

## Context

With the controller's start-up checks answered (ADR 0024), the link layer
synced with the host and started advertising, then armed a timer for the
event and waited for an interrupt nothing raised. The user chose to model the
core's timer interrupt.

The timer block was read off the ROM the same way as everything before it:
the three setters (`r_rwip_timer_10ms_set`, `_hs_set`, `_hus_set`), the
dispatcher (`r_rwip_isr`), the clock sampler (`r_rwip_time_get`) and the
blob's interrupt handler, which takes its status words from a FIFO at
`+0x2D8`. The interrupt source came from the fork's interrupt-matrix writes:
the controller mapped RWBLE, source 8, and the blob's handler there is the
dispatcher; source 5's handler reads the baseband's status on the other page
and knows nothing of these timers, which one wrong guess proved with an
interrupt storm.

A first model in Python worked as far as raising the interrupt: the
dispatcher popped the FIFO, the ROM acknowledged the timer, requested a
software interrupt (control register bit 27, cleared by the core) and ran the
arbiter. The arbiter then **cancelled every event**. Its event-start routine
(ROM `0x4002ccd8`) cancels an event unless the handler runs within the same
half-slot, 312.5 microseconds, as the event's time, and the programming delay
this firmware configures is zero. A timer fired from Python through a socket
and a thread arrives milliseconds late by construction: the arbiter asked for
a target two hundred microseconds ahead and the request itself took longer
than that to reach Python. On the silicon the interrupt is microseconds late
and lands in the same half-slot.

## Decision

1. **The core page is a C device, `qemu-patch/esp_ble_core.c`**, on
   `QEMU_CLOCK_VIRTUAL`: the clock in half-slots and half-microseconds, the
   three targets and their status bits, the enable mask, masked and raw
   status, write-one-to-clear acknowledge, the dispatcher's FIFO, the two
   self-clearing bits of the control register, the version constant, and
   store-and-return for everything else on the page, the exchange-memory base
   registers included. The timer fires at the target and the guest samples
   the clock microseconds later, inside the half-slot. The Python model was
   the prototype; its semantics moved over line for line and it is gone.

2. **The line is RWBLE, source 8**, high while anything pending is enabled
   or an entry waits in the FIFO; raised from a bottom half, as the I2C
   master does, so the interrupt never lands inside the write that armed it.

3. **ADR 0011's tiering held**: silicon with timing in C, in a file we add.
   The bridge's Python side keeps the windows it had.

4. **The bridge's PC-watch hatch also logs writes**, with the value; a timer
   being armed is a write, and which code armed it mattered as much as which
   code polled the result.

## Consequences

**Good**

- The address-renewal event starts instead of being cancelled, the alarm
  timers run, the advertising set is configured, and the link layer pushes
  its first advertising event into the core's exchange table
  (`r_sch_prog_ble_push`). Everything up to "the radio runs the event" is
  there.

**Costs, accepted**

- No Python unit tests for this device; the pinned outcome is the slow
  Meshtastic boot. A device that talks to nothing but the guest cannot be
  poked from outside QEMU without a debugger the container lacks.

**Open, as of this record**

- **The core executing the event.** The exchange table lives in RAM behind
  the base-register mapping. What the link layer writes there, what status it
  reads back, and which end-of-event bit it waits for are the next things to
  read (`r_sch_prog_push`, `r_sch_prog_end_isr`, `r_emi_get_mem_addr_by_offset`).
- Meshtastic's panel is still at its boot screen; the I2C keyboard poll
  times out once per run. Whether that is the loop task waiting on
  Bluetooth or something else is not yet measured.

## Alternatives rejected

**Fire early from Python by the measured latency.** The guest's own clock
would then disagree with the interrupt, and the arbiter reads that clock.

**Read the exchange table from Python.** A second reason for C: the event
model will need guest RAM, which the bridge does not carry.
