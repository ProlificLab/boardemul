# ADR 0008 — Claim unmodelled peripheral windows even with no model behind them

**Status:** Accepted — 2026-08-21

## Context

The factory firmware died in an `IntegerDivideByZero` immediately after I2C came
up. It survived being given every I2C device, being given none at all, and
having PSRAM fixed — the register dump was byte-identical every time. That
ruled out the devices and pointed somewhere else entirely.

Disassembling the faulting block through QEMU's own `-d in_asm` showed a 64-bit
divide whose divisor was assembled from a value **read back out of a peripheral
register**, inside a function that took a mutex and indexed a small table. That
is `ledc_get_freq()`: the backlight on GPIO 41 and the keyboard LED on GPIO 42
are PWM outputs, and `analogWrite()` computes the frequency by dividing by a
clock divider it reads back from the LEDC block.

The mechanism is the important part. ADR 0004 established that the machine maps
a catch-all region, `esp32s3.iomem`, across the entire peripheral window, and
that access to an unmodelled peripheral lands there silently. What was missed
then is what that region actually *does*: it **discards writes and returns
zero**. So any driver that writes a configuration register and reads it back —
which is most of them — reads back zero and computes with it.

The firmware set an LEDC clock divider, read back zero, and divided by it.

Claiming the LEDC window with a region that does nothing but remember what was
written to it made the crash disappear. There is no LEDC model: reads still
return only what the firmware itself wrote.

## Decision

1. **Unmodelled peripheral windows are claimed by a `TraceRegion`** — a plain
   register file that stores writes and returns them. `emu/board.py` lists them
   under `UNMODELLED`: LEDC, I2S0, LCD_CAM, UHCI, PCNT, SPI0, SPI3.

2. **This is not modelling and must not be mistaken for it.** A `TraceRegion`
   has no behaviour: no status bits that change, no FIFOs, no interrupts. It
   preserves read-modify-write and nothing else. A driver that waits for a
   status bit to flip will still hang.

3. **A window is claimed as soon as the firmware is seen touching it**, whether
   or not there is any intention of modelling it. The cost is one line; the
   benefit is that read-modify-write stops silently corrupting guest state, and
   the traffic becomes visible.

## Consequences

**Good**

- The factory demo runs past the crash, initialises its sensors, and drives the
  panel. This one change is what unblocked it.
- Whole classes of read-modify-write corruption disappear at once, including
  ones we have not hit yet.
- Traffic to unclaimed peripherals is now visible per region, which is what made
  the LEDC involvement obvious the moment the window was claimed.

**Costs, accepted**

- A register file that answers plausibly is a more convincing liar than a region
  that returns zero. Firmware can now get *further* on a peripheral that does
  not exist, and fail somewhere less obviously connected to the cause.
- The list is reactive: a window is claimed after something is seen touching it,
  so the first encounter with a new peripheral still gets the catch-all's
  behaviour.

## Alternatives rejected

**Model LEDC properly.** The correct long-term answer and unnecessary now:
nothing reads a *meaningful* value back, it just needs its own writes returned.
Modelling it would have taken a day and taught us nothing the register file did
not.

**Claim the whole `0x60000000` peripheral window with one region.** Would fix
every read-modify-write at a stroke, and would also shadow the UART, timers,
eFuse and SHA that the fork models correctly — throwing away the main reason for
building on the fork (ADR 0005 decision 4).

**Leave it and work around the crash.** There was no workaround: the firmware is
unmodified by definition, and it divides by what it reads.
