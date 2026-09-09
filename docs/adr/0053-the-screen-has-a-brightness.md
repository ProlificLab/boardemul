# ADR 0053 — The screen has a brightness

**Status:** Accepted — 2026-09-09.

## Context

"Which hardware is still missing" had been answered by reading, which is how
the T-Deck Max ended up with a satellite receiver nothing exercises. It is
answerable by measurement instead: the bus log records every access to the
windows this project claims but does not model, so counting them across every
run the project has kept says what the firmware actually uses.

| window | accesses | what it is |
| --- | --- | --- |
| `fe` | 536,616 | the WiFi front end |
| `nrx` | 42,418 | WiFi receive |
| `wifi_mac0` | 40,650 | the WiFi MAC |
| **`ledc`** | **35,486** | **the backlight** |
| `bb` | 13,044 | the WiFi baseband |
| `uhci`, `pcnt`, `lcd_cam`, `spi3`, `wifi_mac3`, `wifi_mac4` | 8 or fewer | claimed, never used |

Everything below the baseband is untouched -- those windows are claimed so that
an access to them would be visible, and none ever comes. So after WiFi, which
is one large subsystem, the backlight is the whole of what is left, and it is
not a small thing: the interface fades the panel out before it blanks the
screen, and every step of that fade was landing in a window that only
remembered the last value written.

The registers were read off a run rather than a table. Meshtastic uses channel
seven on timer three at nine bits of resolution, so a full scale of 511. The
duty register carries the duty shifted up by four; it ramps to 307 -- sixty per
cent -- and later steps down two at a time to 2, which is the screen going
dark. `CHn_CONF1` is written with bit 31 alone or with bit 30 as well, and the
duty parameters beside them are all zero: the driver is stepping the fade
itself and using that bit to latch each value, not asking the hardware to fade.

## Decision

1. **The model reports channels, not pins.** Which pin a channel reaches is
   decided in the GPIO matrix, which lives inside the machine's own GPIO block
   and is not visible from Python. Naming one channel "the backlight" would be
   a measured fact about one firmware dressed up as a fact about the board, so
   the model says what each channel drives and leaves the reader to know which
   is which.

2. **Starting a duty clears the bit that started it**, because the hardware
   does and the driver reads the register back. A window that only remembers
   writes hands a driver a bit that never clears, which is the kind of thing a
   driver spins on.

3. **A duty is a fraction of its timer's full scale, or nothing at all.** A
   timer nobody configured has a resolution of zero, and a duty cannot be a
   fraction of that -- which is the same divide-by-zero this window was
   originally claimed to prevent. A channel that was never enabled is not dark,
   it is not driving, and the two are reported differently.

4. **The run records it**, in the summary and in the manifest beside what the
   radio did, so a run can be asked afterwards whether the screen was dimmed
   rather than only watched at the time.

## Consequences

**Good**

- A run now says what the panel's brightness did: `channel 7 at 0%, between 0%
  and 60% over 156 change(s)` -- the interface lighting the screen and then
  fading it out, which nothing could see before.
- Eight unit tests replay the sequence a real run produced, and a slow test
  asserts the firmware's own behaviour: that it lights the panel, and that left
  alone it ramps the backlight to nothing in many small steps.
- The measurement that chose this also cleared five windows: `uhci`, `pcnt`,
  `lcd_cam`, `spi3` and two of the WiFi MAC windows are never touched by any
  firmware here, so nothing is missing behind them.
- **A run now answers the question itself.** Every claimed window and what the
  firmware did with it goes into the manifest, and a chip select the controller
  clocked bytes into with nothing on it is reported beside the I2C addresses
  nobody answered. Both were already known to the run and thrown away, which is
  why choosing this feature meant grepping through months of bus logs by hand.

**Costs, accepted**

- **The rendered frames are not dimmed.** The panel draws what the firmware
  drew, at full brightness, whatever the backlight is doing. Multiplying pixels
  by the duty would be truthful and would change every test that counts them,
  so it is a separate decision from modelling the controller.
- The hardware's own fader -- the duty step, count and scale in `CHn_CONF1` --
  is stored and not acted on. No driver here uses it; they step the duty
  themselves. A firmware that did use it would see its brightness stop moving.
- No frequency is reported. Brightness does not need one, and the divider's
  clock source is selected in a register this model does not decode, so
  reporting a frequency would mean claiming something unverified.
- The Max board gets the same model on the same window, and its firmware drives
  the backlight there too, but which channel it uses has not been read off a
  run.

## Alternatives rejected

**Name channel seven the backlight.** It is what Meshtastic uses and it would
make the reports read better. It is also a fact about one firmware on one
board, and the GPIO matrix that would make it a fact about the hardware is
sitting behind a device that does not expose it.

**Leave it a trace window and dim the panel from the duty writes directly.**
Half the work for a worse result: the duty register is written several times
per settled value and only counts when it is latched, so a panel following the
writes would flicker through values the screen never showed.
