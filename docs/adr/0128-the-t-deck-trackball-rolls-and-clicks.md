# ADR 0128 — The T-Deck's trackball rolls and clicks

**Status:** Accepted — 2026-10-03. Extends ADR 0026; replaces the "accepted
and send nothing" treatment of the trackball's directions in ADR 0127.

## Context

The trackball is five lines with pull-ups on plain GPIOs (ADR 0026). The
board drives all five high at start, so the ball rests released. Nothing
drives them after that, so the firmware never sees it roll or click. The
window draws the ball but does nothing with it, and the keyboard model
accepts `UP`, `DOWN`, `LEFT` and `RIGHT` and drops them.

What the lines do comes from two published sources:

- **LilyGO's own test** (`examples/UnitTest` in Xinyuan-LilyGO/T-Deck,
  `mouse_read()`). It reads the four direction lines and the boot pin. Every
  change of level on a line, in either direction, is one step of 24 pixels.
  GPIO 2 moves right, 3 up, 1 left and 15 down; GPIO 0 is the click. So a
  line toggles once per step of roll. It does not return high between steps.
- **Meshtastic on the T-Deck** (`variants/esp32s3/t-deck/variant.h` and
  `src/input/TrackballInterruptBase.cpp`). It attaches FALLING interrupts
  to the same pins and counts them. A direction fires once three falling
  edges have arrived within a second, checked every 50 ms. That is six
  changes of level, since only every other change falls. The click starts
  on GPIO 0's falling edge. It is a short press if the pin reads high again
  within 500 ms and a long press if it stays low, checked on the same 50 ms
  poll.

Two things make the lines hard to drive from the host as they arrive:

- **Edges can merge.** A line's interrupt status latches an edge until the
  firmware clears it. Several changes on one line before its handler runs
  arrive as one interrupt, and a counter sees one step.
- **Clicks can be too short.** A quick click takes about 100 ms of the host's
  time. When the guest runs slower than the host, that can be under one of
  Meshtastic's 50 ms polls, which then never sees the pin low.

## Decision

1. **Each step of roll toggles its direction's line**, as LilyGO's test
   reads it. The line starts high, and each step flips it. A model in
   `emu/devices/tdeck_trackball.py` keeps the five levels. It drives them
   through a callback, so it runs without a guest.

2. **Steps are paced, not delivered at once.** They queue in order. The
   board's input ticker changes one line every 4 ms of host time, so each
   edge is its own interrupt even in a guest several times slower than the
   host. A roll the host asks for is never merged or dropped.

3. **The click holds GPIO 0 low from press to release, for at least 200 ms**
   of host time. The minimum is an allowance like the keyboard's queue
   (ADR 0127), so a quick click still spans a poll. Holding longer is a long
   press, as on the device.

4. **A `trackball` input event** carries either a roll or the click:
   `{"type": "trackball", "roll": "up", "steps": 6}` and
   `{"type": "trackball", "press": true}`. A key event for `UP`, `DOWN`,
   `LEFT` or `RIGHT` is one notch of roll in that direction, so those names
   now do something.

5. **One notch is six steps** (three full cycles of the line). The ball has
   no detents, so a notch is the window's unit, not the device's. Six is the
   smallest roll that the firmware we run, Meshtastic, acts on from a
   standing start. In LilyGO's test it moves the cursor 144 pixels.

6. **The window rolls and clicks the ball.**
   - The mouse wheel over the device rolls it up and down a notch per click
     of the wheel, and with Shift held, left and right.
   - The host's arrow keys roll it a notch per press.
   - Pressing the mouse on the ball clicks it for as long as the button is
     held.
   - The drawn ball lights while it is pressed, and its highlight moves the
     way it rolls.

## Consequences

**Good**

- The firmware's own trackball code sees the edges it was written for.
  Meshtastic's menus move and select from the window, and long presses work.
- Every roll the host asks for reaches the firmware, one interrupt per
  change.

**Costs, accepted**

- Pacing sets a top speed: 250 changes a second, about 40 notches a second.
  That is far beyond what a thumb rolls.
- The notch and the minimum click are choices made for the window, stated as
  such. A firmware with a different threshold sees a different number of
  moves per notch, as it would from a different flick of the thumb.
- How far the ball rolls per step on the device is not published. Nothing
  here depends on it.
