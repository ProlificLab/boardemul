# ADR 0026 — The trackball rests released

**Status:** Accepted — 2026-09-02.

## Context

With Bluetooth advertising running (ADR 0025), Meshtastic's panel still only
alternated between two splash screens stamped `>> Programming mode <<`, one
with a six-digit number. Its device-ui source says what that is: a mode the
interface enters when the firmware's Bluetooth configuration arrives with
Bluetooth enabled; and it *enables* Bluetooth itself, with a random fixed
PIN and a reboot, when the boot logo is held for eight hundred milliseconds.
That is the silent reboot at thirty-eight seconds of every first boot, the
number on the screen, and the Bluetooth start-up this whole chain went
through. The first boot's own frames show the long-press arc drawn around
the logo. Nothing had touched it.

The logo is an LVGL button, and LVGL presses buttons from keypad and encoder
inputs as well as touch. The touch model reports nothing when idle and the
keyboard model answers zero when its queue is empty. The trackball is five
push contacts on plain GPIOs, pulled up on the board, pressed low; Meshtastic
maps its press to GPIO 0 (`variants/esp32s3/t-deck/variant.h`: `TB_PRESS 0`,
`TB_UP 3`, `TB_DOWN 15`, `TB_LEFT 1`, `TB_RIGHT 2`). Our GPIO block reports an
input the host never drove as its output latch, zero: a button held from
power-on. The pull-up that would make it read high lives in IO_MUX, which
the fork leaves unimplemented, so the firmware's own `INPUT_PULLUP` never
reaches anything.

## Decision

1. **The T-Deck board drives its five trackball lines high at start**, in
   `board_tdeck.py` next to the touch interrupt, with the pin numbers cited.
   The GPIO client replays levels driven before the machine connects, so it
   costs nothing at run time.

2. **The GPIO client exposes what has been driven** (`levels`), so the test
   pins the fact rather than the effect.

3. **The programming-mode path stays reachable**: holding the logo still
   switches Bluetooth on and reboots, which is now the way to run the
   Bluetooth stack from ADR 0025 on purpose.

## Consequences

**Good**

- A fresh first boot of unmodified Meshtastic 2.7.10 no longer reboots
  itself; it mounts its filesystem, reads its preferences, and shows the
  device-ui's setup dialog, region and names, with the sidebar. The goal for
  this board is met by the firmware it was set against.

**Costs, accepted**

- Idle levels are stated per board, not derived from the firmware's pull-up
  configuration. Until IO_MUX is modelled that is the honest place for them.

**Open, as of this record**

- The partitions extracted for ADR 0024's second-boot reproduction carry a
  Bluetooth-enabled configuration this defect created. They stay as an
  investigation artifact under `logs/`; a Bluetooth-enabled state is now
  produced by holding the logo.

## Alternatives rejected

**Model pull-ups in the GPIO block.** It cannot see IO_MUX, and guessing a
pull-up for every undriven input would hide the next button the other way.

**Leave it and drive the logo release from the input file.** The firmware
would still boot into the wrong mode on every run that sends nothing.
