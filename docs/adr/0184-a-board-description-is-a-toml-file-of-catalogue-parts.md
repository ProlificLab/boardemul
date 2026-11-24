# ADR 0184 — A board description is a TOML file of catalogue parts

**Status:** Accepted — 2026-11-24. Carries out ADR 0180, decision 3.

## Context

ADR 0180 decided that users describe their own boards: an ESP32-S3 and
which of BoardEmul's parts are wired to which pins, with no parts of their
own and no other chip, and that a part the firmware talks to which the
description does not wire answers as an empty bus would, and is reported.

Today the two boards are Python modules (`emu/board.py`, the T-Deck Max;
`emu/board_tdeck.py`, the T-Deck), each a `build()` that makes its parts and
the `-device` lines QEMU's models are configured by. Most of what either
holds is the chip's: the windows nothing models, the WiFi MAC, the Bluetooth
core, I2S, USB. What is the board's is short: which part is on which select
of SPI2 and at which address of which I2C bus, which pins are its lines,
what is on the second UART, and what is wired to plain GPIO.

QEMU's SPI master (`qemu-patch/esp_spi_master.c`) has three selects, each
with its own busy, reset and interrupt lines, and one data/command line; the
I2C masters hand Python every transaction, and an address nobody answers is
already a NACK, counted per bus (`emu/i2c_link.py`).

## Decision

1. **A board description is a TOML file**, read with the standard library's
   `tomllib`. It names the board and its chip, `esp32s3`, the only one, and
   lists parts by their catalogue name:

   ```toml
   name = "my-tracker"
   chip = "esp32s3"

   [spi2]
   dc = 11                      # the data/command line, when a screen needs one

   [[spi2.parts]]
   part = "st7789"
   cs = 12
   backlight = 42

   [[spi2.parts]]
   part = "sx1262"
   cs = 9
   busy = 13
   reset = 17
   irq = 45

   [[i2c0.parts]]
   part = "gt911"
   irq = 16

   [uart1]
   part = "l76k"
   ```

   Up to three parts on `spi2`, at most one of them a screen; any number on
   `i2c0` and `i2c1`, at their catalogue address unless `address` says
   another; one on `uart1` and one on `uart2`, of which at most one is a
   satellite receiver; and `[[gpio.parts]]` for parts on plain pins
   (the trackball). A pin is a number from 0 to 48. Two parts on one
   select, two on one address, or one pin given two jobs is refused, and so
   is any key a part does not take: a description is read strictly,
   because a typo that is ignored is a wire that is silently missing.

2. **The catalogue is what the two boards are built from**, each part with
   the bus it sits on, the pins it takes and which of them it needs:
   the T-Deck's `st7789`, `sx1262`, `sdcard`, `gt911`, `tdeck-keyboard`,
   `es7210`, `l76k` and `tdeck-trackball`, and the Max's `epaper`,
   `es8311`, `cst328`, `xl9555`, `bhi260ap`, `tca8418`, `bq27220`,
   `drv2605`, `sy6970`, `ublox-m10` and `a7682e`. A part's behaviour is its
   model's, unchanged; an e-paper's BUSY line rests high from power-on, as
   on the Max, and the Max's touch controller and keyboard take input the
   Max's way.

3. **The chip is the T-Deck's**: a described board gets the same windows,
   WiFi, Bluetooth, I2S and USB as the T-Deck, which is the board that
   modelled the chip most completely.

4. **Both boards are written as descriptions** (`emu/boards/tdeck.toml`,
   `emu/boards/tdeck-max.toml`). A test holds that the T-Deck's builds the
   same QEMU devices and the same parts on the same selects and addresses
   as `emu/board_tdeck.py`, and that the Max's has `emu/board.py`'s parts,
   selects, addresses and UARTs; its chip windows are the T-Deck's, by
   decision 3. Each boots its vendor firmware as its hand-written board
   does. That is what keeps the described path honest, until the modules
   can be retired.

5. **Unwired is reported.** At the end of a run a described board lists
   the I2C addresses the firmware asked for and nobody answered, and the
   SPI selects it used that carry nothing, under "not wired".

6. **A device keeps its description**: a device made from a description
   holds a copy as `board.toml` in its folder (ADR 0145), so it runs the
   same wherever the folder goes, the runtime container included; its
   `board` is the description's name.

## Consequences

**Good**

- A firmware developer whose board uses catalogue parts on other pins can
  test on it without writing Python.
- The described T-Deck is checked against the hand-written one, part by
  part.

**Costs, accepted**

- Three parts on SPI2 is QEMU's model's limit, not the chip's; a board
  with more waits for the model to grow.
- A screen's touch mapping and the window's look are those of the board
  the parts come from: the page shows a described board with the Max's
  e-paper, touch controller or keyboard in the Max's face, and any other
  in the T-Deck's. A described board with another panel geometry shows
  the frames as the part draws them.
