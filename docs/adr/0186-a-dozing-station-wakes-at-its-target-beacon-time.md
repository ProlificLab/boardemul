# ADR 0186 — A dozing station wakes at its target beacon time

**Status:** Proposed — 2026-11-24. Not yet built; what is below is what the
driver was read to expect, and the plan to give it that.

## Context

A firmware whose WiFi station uses modem sleep -- Arduino's default,
`WIFI_PS_MIN_MODEM` -- joins the run's access point, serves, then never
receives another frame. `examples/wifi-http` shows it (`pio run -e
t-deck-sleep`): the access point holds the station's frames as ADR 0109
says it should, sets its bit in the TIM of its beacons, fills its hold of
64 and drops the rest, and the station never comes for them. Awake
(`WiFi.setSleep(false)`), the same firmware answers `GET /status` through
the forwarded port in 0.2 s.

Read from Espressif's `libpp.a` (IDF 4.4.6, the Arduino core's), whose
objects keep their function names (`hal_tsf.o`, `wdev.o`):

- **The TSF and the target beacon time are in the WDEV page, 0x60035000.**
  `hal_get_sta_tsf` sets bit 0 of `+0x00c` (a latch), reads the low word
  at `+0x018` and the high at `+0x01c`, and clears the bit.
  `hal_set_sta_tsf` writes `+0x010` and `+0x014` and sets bit 4 of
  `+0x00c`. `hal_set_sta_tbtt` puts the target's low 26 bits in `+0x020`,
  the interval in TU (microseconds >> 10) in the low half of `+0x030`, the
  wake-ahead time in its high half, a 16-bit value in the low half of
  `+0x024`, and sets bit 8 of `+0x00c`. `hal_enable_sta_tsf` sets
  0x98000000 in `+0x028`; `hal_enable_sta_tbtt` sets 0x04000000 in
  `+0x028` and 0x40000000 in `+0x05c`.
- **The target beacon time is an interrupt.** `wDev_ProcessFiq` reads the
  MAC's events (0x60033c3c) and the "pwr" events (`hal_pwr_interrupt_get_
  event`: 0x60035118, cleared by writing 0x6003511c); pwr bits 1-4 post
  event 26 to the WiFi task, bits 5-8 event 27. `hal_enable_sta_tbtt` sets
  bit 4 in the enable register 0x60035110 and in the clear register: the
  target beacon time is pwr bit 4.
- **It reaches the CPU on source 2.** Read from the interrupt matrix during
  a run: the firmware maps source 0 (the MAC) and source 2 (`ETS_WIFI_PWR_
  INTR_SOURCE`) to the same CPU interrupt, so one handler serves both.
- **Nothing in a run answers any of it.** The WDEV page is left to QEMU's
  catch-all, because it serves the hardware RNG at 0x6003507C (ADR 0008):
  every register above read zero after the firmware had written them.
- **The beacons carry no time.** `emu/devices/wifi_ap.py` writes eight zero
  bytes where a beacon's timestamp goes; a real access point writes its TSF.

## Decision (proposed)

1. **The WiFi MAC device answers the WDEV registers above**, on the guest's
   clock, by extending its own window rather than adding an overlay (ADR
   0165), and keeps serving the RNG at 0x6003507C exactly as now. The TSF
   counts microseconds from the value last loaded; the target beacon time
   fires pwr bit 4 when the TSF's low 26 bits reach the target less the
   wake-ahead time, then moves the target on by the interval; pwr events
   are latched, enabled by 0x60035110, cleared by 0x6003511c, and raise
   source 2 while any enabled one is pending.
2. **The access point's beacons carry its TSF**, a microsecond count on the
   guest's clock, and go out at its target beacon times, multiples of the
   beacon interval, so a station that loads its TSF from a beacon wakes when
   the next one is due.
3. **Proven by the example**: `t-deck-sleep` answers `GET /status`, and the
   access point's totals show held frames delivered rather than dropped.
   Then the example drops its `WiFi.setSleep(false)` and the CHANGELOG its
   known limit.

What is not read yet, and is to be read before it is modelled, not guessed:
whether the hardware or the driver moves the target on after each firing,
what the 16-bit value in `+0x024` is, and what the station's radio does
between target times (how long it listens for the beacon).

## Consequences

**Good**

- A firmware keeps Arduino's default power saving and still talks.

**Costs, accepted**

- A second interrupt source and a timer in a device that has been the
  most delicate in the project (ADRs 0054-0110); every WiFi test reruns.
