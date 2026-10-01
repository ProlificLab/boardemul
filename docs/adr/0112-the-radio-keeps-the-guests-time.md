# ADR 0112 — The radio keeps the guest's time

**Status:** Accepted — 2026-10-01. Moves ADR 0037's timing onto the guest's
clock. Closes two of the costs ADR 0111 accepted. ADR 0113 finishes the work
for several boards.

## Context

ADR 0037 made a packet occupy the channel for as long as the datasheet's
arithmetic says. It measured that time on the host's clock (`time.time()`):

- when a transmission ends, and so when TxDone is raised;
- how long a transmitting radio is deaf;
- how long a packet arriving occupies the receiver, and so what collides;
- when a packet on the air was sent.

The guest does not run at the host's speed. Under the instruction-counted
clock (ADR 0027), Meshtastic ran at about a fifth of wall time. A 100 ms
packet then reads as 20 ms to the firmware. Its airtime accounting, duty
cycle, listen-before-talk and the collisions all come out distorted, and by
how much depends on how busy the host is. `test_air.py`'s collision test fails
when the host stalls for a fifth of a second.

Bluetooth's timers already run on QEMU's clock (ADR 0025), and so do the WiFi
peer's beacons. The radio is the part left on the host's clock.

Two things stood in the way, and both are mechanism, not physics:

- **The radio does not know when a command happened.** A segment arrives on
  the host's clock. Python parts can read the guest's time (ADR 0104), but
  only from a tick every 10 ms that does not wait for anyone.
- **The radio cannot move its interrupt line at a guest time.** DIO1 is driven
  from Python over the GPIO socket (`emu/peripherals/gpio.py`), and the level
  arrives whenever the host delivers it. ADR 0111 met the same wall for BUSY
  and moved that line's timekeeping into the SPI controller.

### What the datasheet says

Source: Semtech DS.SX1261-2.W.APP, Rev. 1.2, June 2019.

- **SetTx (13.1.4).** The PA ramps, then the packet goes out. When its last bit
  is sent, TX_DONE is raised and the chip goes back to STBY_RC. The ramp ends
  when BUSY falls (8.3.1), which ADR 0111 already times from Table 8-2.
- **Fallback (13.1.15, Table 13-23).** After a transmission or a reception, the
  chip goes to STBY_RC by default, or to FS or STDBY_XOSC if
  `SetRxTxFallbackMode` (0x93) said so.
- **CAD (6.1.5, 13.1.8, Table 13-72).** A CAD lasts the number of symbols
  `SetCadParams` (0x88) chose: 1, 2, 4, 8 or 16. The radio then stays in RX
  for about half a symbol more to post-process the result. CadDone is raised
  after that, and the chip returns to STBY_RC. A symbol lasts 2^SF / BW
  (ADR 0037).
- **Not published:** whether leaving TX or CAD early cancels the pending
  TxDone or CadDone.

## Decision

1. **Every SPI segment carries the guest's time.** The header grows from 8
   bytes to 16. The new field is a little-endian u64: QEMU's virtual time in
   nanoseconds when the controller sends the segment. A part reads it as
   `now_ns`. A write-only burst is sent at the latest when the release that
   ends its command arrives, and that is when a command takes effect.

2. **The GPIO block can schedule a pin to change at a guest time.** It keeps one
   pending change per pin, and a newer schedule replaces it
   (`esp_gpio_schedule`). The block only keeps the time; the part decides it.

3. **A select can be given its part's interrupt line**, with the properties
   `irq_gpio`, `irq1_gpio` and `irq2_gpio`. For such a select, the answer to a
   release (ADR 0111) also says what the line does:
   - its level now, which the controller drives at once;
   - the guest time it next rises, if any, which the controller schedules.

   The answer becomes `<IIBxxxQ`: `high_ns, wake_ns, line, rise_at_ns`.
   `line` is 0 or 1, or 0xFF for "leave it". `rise_at_ns` 0 means no rise is
   pending. A ClearIrqStatus therefore lowers DIO1 the instant its window
   closes, and a TxDone raises it the instant the last bit goes out, both on
   the guest's clock. For a select that has an interrupt line, nothing drives
   the line from Python's side.

4. **The radio keeps the guest's time for everything it does by itself.**
   - A transmission starts when BUSY falls after SetTx, lasts its time on air,
     and ends with TxDone. The chip then goes to its fallback mode, unless the
     host has already moved it elsewhere.
   - A CAD lasts its symbols plus half a symbol, then raises CadDone.
     `SetCadParams` is now read rather than ignored. What the sweep detects,
     and the STBY_RC the chip returns to afterwards, belong together in
     ADR 0113: ADR 0111 found that the second without the first loses packets.
   - A flag appears in GetIrqStatus from its due time, not before, and the
     line rises then if the flag is routed to it.
   - Whether leaving early cancels a pending done is not published, so the
     done is still raised, as before.

5. **A packet on the air carries the guest time its sender started it.** A
   board receiving it does not share that clock yet; ADR 0113 gives it one.
   Until then the receiver judges each packet on its own guest clock:
   - the packet starts when the receiver reads it off the air;
   - it occupies the receiver for its time on air, and that window decides
     deafness and collisions;
   - RxDone is raised at its end, so no longer the moment the packet arrives.

   The receiver reads its own clock from the 10 ms tick, so these times are
   that coarse. A reception is the one event that arrives off the air and not
   through a command. If a release has come since it was scheduled, the
   controller was told its rise like any other. If not, Python raises the
   line, and only then. A second, later write could only land after the
   firmware had cleared the flag, and would be a spurious edge.

## Consequences

**Good**

- A lone radio's every event lands at an exact guest time: TxDone after the
  real ramp and airtime, and CadDone after the real CAD, whatever the host's
  load. Under `-icount` it lands at the same instant every run.
- Two of ADR 0111's accepted costs close. The packet leaves when the ramp ends
  rather than at the release, and the chip drops to its fallback mode after a
  transmission.
- The air tests drive the guest's time themselves, so a host stall can no
  longer fail them.

**Costs, accepted**

- Every SPI segment is 8 bytes longer, which is nothing next to a frame.
- Receptions stay approximate across boards until ADR 0113. They are timed
  against the receiver's 10 ms tick, and two boards' guest clocks are not one
  clock.
- Until ADR 0113, DIO1 has a second driver on a radio that receives:
  Python, for a reception the controller was not told of. Its GPIO client
  must then send the level even when its own record says it already did,
  because the controller lowers the line behind its back.
