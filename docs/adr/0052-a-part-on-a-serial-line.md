# ADR 0052 — A part on a serial line

**Status:** Accepted — 2026-09-09.

## Context

Every part this emulator models sits on a bus that needed something added to
QEMU first: a GPIO block, an SPI controller, two I2C controllers, two I2S
engines, a link-layer core. That is why the board's satellite receiver had
never been modelled -- it looked like another one of those.

It is not. The fork's UART controller already speaks to a character device, so
a socket is the whole of it and the C footprint of this ADR is nothing at all.

**What was missing was measured, not assumed.** Taking the parts each firmware
names in its own strings and setting them against what the boards model left
one clear gap on the T-Deck: the receiver. The vendor build says so itself --
`Try to init L76K . Wait stop .` and then `Get L76K timeout!`, because only the
console UART was wired and the other one went nowhere. (Every `RTC` string in
all three firmwares turned out to be the framework's own -- RTC IO, the RTC
clock -- not a part, so there is no clock chip missing.)

**The firmware states the protocol.** All five commands are in its strings,
which is as good a specification as the ROM has been for everything else:

    $PCAS03,0,0,0,0,0,0,0,0,0,0,,,0,0*02    silence every sentence
    $PCAS06,0*1B                            then wait for $GPTXT,01,01,02
    $PCAS04,5*1C                            which constellations
    $PCAS03,1,0,0,0,1,0,0,0,0,0,,,0,0*02    fix and recommended-minimum back on
    $PCAS11,3*1E                            what movement to expect

Only the second is waited on. That is why a line with nothing on it fails in
the way it does: silence satisfies "wait for it to stop" trivially, and then
there is nothing to answer the question.

**Which port it is was measured too.** Run with all three of the machine's
UARTs sunk to files, the guest writes to exactly one of them, and what it
writes is `0xB5 0x62` -- u-blox sync bytes, a factory-reset preamble sent
blindly before the part is identified. So the receiver is on the machine's
second UART, and a firmware probing for a u-blox module there gets no answer
from an L76K, which is the part behaving rather than the model falling short.

## Decision

1. **A serial part needs no device of ours.** `emu/serial_link.py` is a socket
   and a thread. A part on a serial line is never asked for anything, so a
   device gets both a chance to answer what arrived and a chance to speak
   unprompted, which is the whole of what a serial line is.

2. **The receiver is a scenario input, like the air.** `--gps LAT,LON[,ALT]`
   fits one and says where the board is; without it there is no module, which
   is how these boards are sold. The sentences are generated from that position
   rather than replayed, so a run says where it was and the data follows.

3. **Its clock is stated, not the host's.** A part that reported a different
   time on every run could not be compared with itself, and every other source
   of time here is already pinned.

4. **The ordering of `-serial` is the only thing that names the port**, so the
   console is given first and the board's part second. It is fragile in exactly
   one way -- a `-serial` passed through `BOARDEMUL_QEMU_ARGS` would shift it --
   and that hatch is documented as unsupported already.

## Consequences

**Good**

- The firmware finds it and says so: `L76K GNSS init succeeded, using L76K
  GNSS Module`, where before the same build printed `Get L76K timeout!`. It
  then goes on to configure the module, which only a part that answered gets
  asked to do.
- Two tests, one fast and one slow. The fast one drives the firmware's own five
  commands through the model and checks the reply, the checksums, both signs of
  both axes and a command split across reads; the slow one is the firmware's
  verdict.
- **A defect fell out of it.** Every board's `build()` was called through an
  `except TypeError: build(outdir)` fallback. When one board gained a `ble_air`
  argument and the other did not, that quietly dropped the T-Deck Max back to a
  build with no air at all -- `--air`, `--at`, `--antenna`, `--environment` and
  `--obstruction` all did nothing on that board, and nothing said so. Every
  board now takes the same arguments and the fallback is gone.

**Costs, accepted**

- **The module always has a fix.** A real one searches first, and a firmware
  that waits for one would wait forever here rather than a plausible minute.
  Nothing has needed that yet, and making every test wait out a search would
  cost more than it proves.
- Only two sentences are sent, the two the firmware turns on. A firmware
  reading satellites-in-view or course would find them missing.
- `$PCAS04` and `$PCAS11` are accepted and change nothing, because a stationary
  receiver reports the same thing whatever they say. A real module would answer
  them; nothing asks.
- The Max board takes the same part on the same port, wired the same way, but
  no firmware to hand exercises it there -- its own GPS example gives up before
  touching a UART at all. It is plumbed and unproven on that board, and said so
  here rather than left to look tested.

## Alternatives rejected

**Add a UART device of our own, as for every other bus.** There was nothing to
add: the difference between a UART and the other buses is that QEMU already
models the whole of it, and the part on the far end is what was missing.

**Replay a recorded stream of sentences.** Simpler, and it would make the
position a property of the recording rather than of the run -- so two boards
could not be put in different places, which is the thing that made the radio
work worth doing.
