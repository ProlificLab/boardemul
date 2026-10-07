# ADR 0132 — The T-Deck's keyboard interrupt line is never driven

**Status:** Accepted — 2026-10-06. Resolves the item ADR 0127 left open: the
board pulsed GPIO 46 low on every key press, and nothing on the real board
does.

## Context

When the T-Deck's keyboard was modelled (ADR 0127), the board code pulsed
GPIO 46 low on every key press, "for a firmware that waits on a keyboard
interrupt", while noting that LilyGO's keyboard sketch drives no interrupt
line. The commit that added the pulse already recorded that it did not
achieve its aim: Meshtastic stops polling the keyboard once it gives up on
storage, and no edge brings it back.

Every source is now measured:

- **The schematic** (`schematic/schematic.pdf` in Xinyuan-LilyGO/T-Deck):
  GPIO 46 — module pin 16 — is wired to a net named `C3_INT`, which runs to
  the keyboard's ESP32-C3 with pull-ups. The line exists; the pin is real.
- **The keyboard's sketch** (`examples/Keyboard_ESP32C3`, the behaviour
  itself per ADR 0127): its only output is the backlight, on the C3's own
  pin 9. The matrix pins are inputs. `C3_INT` is driven by nothing.
- **The UnitTest firmware** — the one the emulator runs on this board —
  defines `BOARD_KEYBOARD_INT 46` and never references it again. Its
  keyboard is read by polling: `checkKb()` probes 0x55 once at boot, and
  LVGL's input device calls `keypad_read()` → `keypad_get_key()` on every
  input cycle, which reads the byte over I2C.
- **Meshtastic** reads the same keyboard through `cardKbI2cImpl`, which
  polls 0x55 from its own task loop. (The Max's TCA8418 keyboard is the
  genuine interrupt case: ADR 0010.)

So no firmware this emulator runs waits on GPIO 46, and on the real board —
with LilyGO's own keyboard firmware — nothing drives it. The pulse was a
courtesy to a waiter that does not exist.

## Decision

**The board stops pulsing GPIO 46.** A key press reaches the firmware the
way it reaches a real T-Deck: queued in the keyboard model, read back the
next time the firmware polls 0x55. The keyboard model is unchanged.

## Consequences

**Good**

- The input path says what the board does: one I2C device, polled by the
  guest. Nothing pretends to drive a line that nothing watches.
- The GPIO model's record of the run no longer shows 46 falling on every
  key, which was an artefact of the emulator and would have confused any
  future comparison against a hardware capture.

**Costs, accepted**

- If a firmware turns up that genuinely waits on `C3_INT` — one flashed with
  a keyboard co-processor image that drives it — the board will have to
  model that pin again, from that firmware's own measurement.
