# ADR 0151 — The T-Deck's screen shows its backlight

**Status:** Accepted — 2026-11-18. Takes up what ADR 0053 left as a cost:
"the rendered frames are not dimmed".

## Context

ADR 0053 modelled the LED controller, so a run can say what a channel's duty
did. The panel the window and the page show stays at full brightness
whatever the backlight does. On the T-Deck that hides something a person
sees on the device: Meshtastic fades its screen out 30 seconds after
starting, and the emulator went on showing a lit screen that, on the
board, is dark.

ADR 0053 did not name a channel the backlight, because which pin a channel
reaches is the GPIO matrix's to say, and the matrix lives in our C GPIO
block, out of Python's sight. The backlight's pin is the board's, and
published: GPIO 42 (`BOARD_BL_PIN` in LilyGO's `utilities.h`, `TFT_BACKLIGHT`
in `emu/board_tdeck.py`).

The firmware here drives that pin in two ways:

- **LilyGO's UnitTest** toggles it as a plain GPIO. Its `setBrightness`
  (examples/UnitTest/UnitTest.ino) drives a driver of sixteen steps: the
  pin held high is the brightest step; each short low pulse steps one down,
  the lowest wrapping round to the brightest; held low for 3 ms the
  backlight is off.
- **Meshtastic** routes an LED controller channel to it, and ramps its duty
  (ADR 0053).

The matrix register that says which, `GPIO_FUNCn_OUT_SEL_CFG` at GPIO +
0x554 + 4n, is in ESP-IDF's `gpio_reg.h`: the signal in bits 8..0, 256 for
the pin's own output register (`SIG_GPIO_OUT_IDX`), 73 to 80 for LED
controller channels 0 to 7 (`LEDC_LS_SIG_OUT0_IDX`, `gpio_sig_map.h`).

## Decision

1. **The GPIO block reports a board's chosen pins.** Given `report=PATH`
   and `report_pins=MASK`, it sends a record, on a stream, for each pin in the mask
   whenever its output level changes or its output-select register is
   written: the pin, its level, its selected signal, and the guest time in
   nanoseconds. Nothing else changes in the block, and a board that asks for
   nothing gets nothing.

2. **The T-Deck asks for GPIO 42, and works out the backlight from it:**
   - routed to an LED controller channel, the brightness is that channel's
     duty over its full scale, as ADR 0053 computes it;
   - routed to the pin's own output, it is LilyGO's sixteen steps, read off
     the edges by their guest time: a low shorter than 1 ms is a step, and a
     low of 1 ms or more is off. 1 ms lies between the two lengths LilyGO's
     code uses, microseconds for a step and 3 ms for off; it is not a figure
     from the driver chip's datasheet, which this project does not have.

3. **The brightness goes beside the frames**, in `board.json` as
   `screen_backlight`, 0 to 1, as the keyboard's does (ADR 0129). The page
   and the Tk window dim the panel by it, and show it dark at 0.

4. **The frames themselves stay as the firmware drew them**, as ADR 0053
   decided, so every test that compares pixels is untouched.

## Measured

On 2026-11-18, on the T-Deck board:

- **Meshtastic 2.7.10** routes LED controller channel 7 to the pin (signal
  80). The page's brightness rose to 0.60, held while the interface was in
  use, then fell to 0.004 within about forty seconds, which is ADR 0053's
  ramp, read now as the screen going dark.
- **UnitTest 250620** drives the pin itself. Its boot runs LilyGO's fade-in,
  `setBrightness(16)` then `setBrightness(0..16)`: 484 edges, 240 step
  pulses and two offs, as the code computes. Under `--icount` the model
  stepped 1, 2, … 16 and ended at full brightness, every step pulse low for
  3 µs or less.
- **Datagrams lost edges.** The first trial sent each report as a Unix
  datagram, which Linux queues ten of by default, and the bursts arrived cut
  short. The reports go on a stream, whose buffer holds thousands.

## Consequences

**Good**

- The screen goes dark when the firmware darkens it, and comes back when it
  lights it, in the page.
- Which channel drives the backlight is read off the matrix, not assumed
  from one firmware.

**Costs, accepted**

- A PWM duty is shown as a brightness. How the board's backlight driver
  responds to PWM on its enable pin is the chip's, and not published here;
  the duty is what the firmware asks for.
- Without `--icount` the guest's clock follows the host's, and a host stall
  inside a step pulse lengthens it in guest time. One run without it saw a
  5.7 ms pulse, which reads as off, as it would to the chip: the brightness
  is then wrong until the firmware sets it again. Under `--icount` that
  cannot happen.
- The Tk window has no alpha, so it lays a stippled black veil over the
  panel in four densities. The page dims it smoothly.
- The Max has an e-paper panel and no backlight, so nothing changes there.
