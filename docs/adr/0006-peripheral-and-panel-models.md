# ADR 0006 — Model peripherals in Python, and learn their behaviour from the wire

**Status:** Accepted — 2026-08-21

## Context

With the bridge working (ADR 0005), the firmware needed actual devices behind
it: an I2C controller and its nine devices, a general-purpose SPI master, GPIO
level tracking, and the e-paper panel.

Two things about this work were not obvious in advance, and both changed how it
was done.

**Self-clearing bits are the dominant failure mode.** `I2C_CTR.TRANS_START` and
`SPI_CMD.USR` are triggers: hardware clears them as the transfer begins. A model
that stores the written value verbatim looks fine until the driver does a
read-modify-write of that register, at which point the transaction fires a
second time. In I2C this silently corrupted the FIFOs; in SPI it made the
driver's completion poll spin forever. Both looked exactly like a hung
peripheral and neither was.

**Published documentation disagreed with the hardware twice.** The ESP32-S3's
I2C command opcodes are not the ESP32's — `RESTART` is 6, not 0 — and the
GDEQ031T10 is a UC825x-family controller using `0x10`/`0x13` for its data
planes, not the SSD16xx `0x24`/`0x26` that a search suggests. Both were caught
by logging what the firmware actually sent.

## Decision

1. **Model behaviour, then verify against a capture; never the reverse.**
   Register offsets and bit positions come from ESP-IDF's own headers, and
   anything not in a header is read off the wire before it is implemented. The
   device list came from the vendor's `TDeckMaxBoard.h`, not from datasheets.

2. **Every trigger bit is explicitly masked out of stored state.** New
   peripheral models must state which bits are self-clearing. This is the first
   thing to check when a driver hangs.

3. **A reply is snapshotted when the device is addressed, not regenerated per
   read.** An I2C controller splits one logical read into two commands -- n-1
   bytes acknowledged, the last one not -- so a model that rebuilds its answer
   on each call hands out byte 0 where byte 6 was expected. This cost a long
   time to find because it is silent: the touch driver checks the last byte of
   its report for a magic value, discards the whole report when it does not
   match, and still writes its acknowledgement. On the wire the traffic looks
   perfectly healthy. A one-to-one ratio of reads to acknowledgements is worth
   asserting.

4. **Instrumentation is part of the model, not a debugging afterthought.** Each
   controller keeps a ring of recent transactions and can log decoded command
   lists. Finding the `TRANS_START` bug took minutes with a decoded log after an
   hour of guessing without one.

5. **Chip-select and data/command come from GPIO, not from the SPI
   peripheral.** The board drives them with `digitalWrite`, so the panel cannot
   tell a command from a pixel without GPIO state. `GpioOut` claims only the six
   output registers and `GpioIn` only the two input registers, leaving
   strapping, interrupts and the pin matrix with the machine's own model — which
   the ROM depends on at boot.

6. **Undriven input pins must be answered.** The panel's BUSY line is an input
   nothing drives, so it read low forever and every wait-while-busy loop hung.
   Inputs the firmware polls are given a defined level.

7. **The panel renders on a full frame or a refresh command, whichever comes
   first.** Relying only on a recognised refresh command means an unexpected
   controller variant produces nothing at all; a full frame of data is
   self-evidently a picture and is worth rendering on its own.

8. **Frames are written as PNG using `zlib` and `struct`.** No image library is
   needed: a greyscale PNG is a header, one zlib stream and a CRC.

## Consequences

**Good**

- The vendor e-paper demo runs unmodified and renders legible output.
- Device models are ordinary Python: a wrong assumption is a one-line edit and a
  re-run, with no C rebuild.
- The transaction logs double as documentation of what the firmware really does.

**Costs, accepted**

- Every model is only as complete as the firmware paths exercised so far. These
  are not general-purpose device models and should not be mistaken for them.
- **Only one SPI device is modelled.** The LoRa radio shares SPI2 and is
  distinguished by chip-select; the panel checks its own CS, so radio traffic is
  ignored rather than misread — but it is ignored, not handled.
- The panel's raster geometry (240x320 portrait, displayed with a quarter turn)
  was established by looking at the output, not from a datasheet. It is correct
  for this firmware's usage and may not be the only orientation the panel
  supports.
- I2C devices return plausible constants rather than simulating real parts. Good
  enough to get past probing; not good enough to test firmware logic that
  depends on their behaviour.

## Alternatives rejected

**Implement the peripherals in C inside QEMU.** Faster at runtime, and it would
have made every one of the discoveries above cost a recompile. The whole point
of ADR 0001 was to make this layer cheap to iterate on.

**Model the full GPIO peripheral.** Tempting for completeness, and it would mean
taking over strapping and the pin matrix that the ROM reads at boot. Claiming
two narrow windows gets the same result with none of the risk.

**Guess the panel's command set from its part number.** Tried, and wrong: the
firmware's own traffic said UC825x where the part number suggested SSD16xx.
