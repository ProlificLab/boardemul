# ADR 0186 — A dozing station wakes at its target beacon time

**Status:** Accepted — 2026-11-24.

## Context

A firmware whose WiFi station uses modem sleep -- Arduino's default,
`WIFI_PS_MIN_MODEM` -- joined the run's access point, served, then never
received another frame. `examples/wifi-http` showed it: the access point
held the station's frames as ADR 0109 says it should, set its bit in the
TIM of its beacons, filled its hold of 64 and dropped the rest, and the
station never came for them. With `WiFi.setSleep(false)` the same firmware
answered `GET /status` through the forwarded port in 0.2 s.

Read from Espressif's `libpp.a` (IDF 4.4.6, the Arduino core's), whose
objects keep their function names; `tools/annotate_literals.py` prints each
function with the words its `l32r`s load, which are its registers:

- **The TSF and the target beacon time are in the WDEV page, 0x60035000**
  (`hal_tsf.o`, the `tsf_hal_*` functions, four TSFs n = 0..3, the
  station's is 0). `+0x00c` is a command register: bit n latches TSF n
  into `+0x018`/`+0x01c` (low, high), `0x10<<n` loads it from
  `+0x010`/`+0x014`, `0x100<<n` starts its target beacon time from the low
  26 bits in `+0x020`. TSF n's control is `+0x028 + 12n` (0x98000000 on,
  bit 26 its target beacon time), its configuration `+0x030 + 12n` (low
  half the interval in TU, high half the wake-ahead time). Four timers m
  have their control at `+0x05c + 8m` (bit 31 on) and their target at
  `+0x060 + 8m`, the last at `+0x078`, the word before the RNG.
- **They are interrupts.** `wDev_ProcessFiq` reads the MAC's events
  (0x60033c3c) and the "pwr" events (`hal_pwr_interrupt_get_event`:
  0x60035118, enabled by 0x60035110, cleared by writing 0x6003511c): bit
  `0x10>>n` is TSF n's target beacon time (posting event 26), bit
  `0x100>>m` timer m (event 27).
- **They reach the CPU on source 2.** Read from the interrupt matrix during a
  run: the firmware maps source 0 (the MAC) and source 2
  (`ETS_WIFI_PWR_INTR_SOURCE`) to one CPU interrupt.
- **The hardware moves the target on**: `pm_set_next_tbtt` sets only the
  interval, and the driver starts the target on one a beacon ago.
- **The hardware keeps the TSF in step with the access point.** Traced in a
  run: the driver loads 0x1_00000000 into TSF 0, a placeholder, and starts
  its target beacon time on a phase of the interval (0x32000, two
  intervals); it never loads the access point's time itself.
- **Nothing in a run answered any of it**: the page was QEMU's catch-all,
  kept for the RNG at 0x6003507C (ADR 0008), and read zero. Beacons carried
  a zero timestamp, a received frame's header no receive time
  (`wifi_pkt_rx_ctrl_t.timestamp`, word 3, "the local time when this packet
  is received", in esp_wifi_types.h -- not used, see decision 3), and the access point beaconed every
  100 ms of the guest's clock from whenever it last sent anything.
- **And no beacon reached a joined station.** The model took bit 8 of
  `+0x0d8` as the receiver being on (ADR 0058, doubted in ADR 0068). A
  station keeps 0x47 there from its join on, where scanning keeps 0x45 and
  0x155: bit 8 is never set again, so every beacon after the join met a
  "receiver off", and data reached the station only as frames it was
  awaiting.

## Decision

1. **The WiFi MAC device answers the TSF registers, the timers and the pwr
   events**, in two windows of its own (0x60035000-0x6003507B and
   0x60035100-0x6003511F), not an overlay (ADR 0165), leaving the RNG word
   to the machine. TSF n counts microseconds of the guest's clock from the
   value last loaded. A started target beacon time fires its event the
   wake-ahead time before the target and moves on by whole intervals, from
   a start in the past to the first target after now. A timer fires once
   per programming. Events are latched, read through the enable mask, and
   raise interrupt source 2 (`pwr-irq`) while any enabled one is pending.
2. **A beacon sets the station's TSF to its timestamp**, in the MAC, while
   TSF 0 is on, and its started target keeps its phase in the new time.
3. **A beacon or probe response carries its sender's TSF**, the guest's
   clock in microseconds written as it is delivered. A received frame's
   header keeps word 3 at zero: writing the receive time there, as the
   public header names it, made message two of the WPA2 handshake fail its
   MIC on the T-Deck's 250620 and the Max's vendor firmware (found by
   taking the changes out one at a time), and the TSF does not need it.
4. **The access point beacons on its target beacon times**, multiples of
   the interval in TU (now also the interval the beacon states), and not
   sooner than a period after the last frame it offered: a busy access
   point skips a target time and a dozing station listens at the next.
5. **`+0x0d8` is read as a receive filter**: bit 8 everything, bit 1 the
   station's own network. This is inferred from the values above, not read
   in the driver; it is the smallest reading under which a joined station
   hears its access point, which a real one does.

`BOARDEMUL_TSF_TRACE` logs the driver's writes, the loads and starts, the
first events and each beacon that syncs the TSF.

## Consequences

**Good**

- A firmware keeps Arduino's default power saving and still talks:
  `examples/wifi-http`, with no `WiFi.setSleep(false)`, answers
  `GET /status` in 0.2 s, and the access point delivers the frame it held
  instead of dropping its hold.
- A joined station hears its access point's beacons, so beacon loss no
  longer drives it to probe to stay connected.

**Costs, accepted**

- Decision 5 is an inference. If bit 1 means something else, the cost is a
  station that hears beacons it would not have, which is what a joined
  station does anyway.
- What the 16-bit value in `+0x024` is, what `+0x02c` (written at each
  wake) does, and what the radio does between target times are not read;
  the model stores the first two and does not model the third: a dozing
  station hears whatever reaches it, as an awake one does, and wakes for
  its frames because the TIM tells it to.
