# ADR 0129 — The window shows the T-Deck keyboard's backlight

**Status:** Accepted — 2026-10-03. Extends ADRs 0126 and 0127.

## Context

The T-Deck's keyboard has a backlight. In LilyGO's sketch for the keyboard
(ADR 0127) it is an LEDC output on the C3's GPIO 9: 8 bits at 1 kHz, dark
at boot. The main processor sets it with command 0x01, and Alt+B toggles it.
The model now keeps that duty, but the window cannot see it. The window
only reads the panel's frames (ADR 0010), so nothing the board knows besides
the picture reaches it.

The screen's backlight is a different case. LilyGO's own test drives it as
a 16-level pulse-dimmed chip on GPIO 42: 3 ms low turns it off, and each
low pulse steps it down a level. Meshtastic drives the same pin with LEDC
PWM. GPIO outputs do not leave the machine (the GPIO socket carries levels
into it only), and how that chip answers PWM is not published. When
Meshtastic blanks the screen it also draws it dark, which the window
already shows.

## Decision

1. **The board publishes what the window needs beside the frames.**
   `frames/board.json` holds `{"keyboard_backlight": duty}`, with the duty
   from 0 to 1. The board writes it only when the duty changes, beside the
   real name and renamed onto it, as `latest.png` is, so a reader never sees
   it half written.

2. **The window lights the keyboard as the LEDs would.** It reads
   `board.json` on its frame poll. At a duty above zero the legends brighten
   towards white in proportion, and each cap gets a faint wash. The LEDs sit
   under the caps and the legends are the translucent part, so that is where
   the light shows. At zero the keyboard is drawn as printed.

3. **The screen's backlight is not shown.** It stays out of the window until
   the chip's response to PWM can be taken from a source and GPIO 42's level
   can leave the machine. The window shows the frames as drawn.

## Consequences

**Good**

- Alt+B, typed or clicked, visibly lights the keyboard. So does a firmware
  that sets the brightness itself.
- The board has one file for telling the window about state that is not
  pixels, for whatever comes next.

**Costs, accepted**

- The glow is drawn, not measured: brightness in proportion to duty, with no
  LED curve, because none is published.
- A dimmed screen looks as bright as a lit one. Only a blanked one, which
  the firmware draws dark, looks dark.
