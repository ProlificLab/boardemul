# ADR 0010 — Deliver input by modelling GPIO interrupts and pushing them out of band

**Status:** Accepted — 2026-08-25

## Context

The viewer (ADR 0009) is one-way. Making the keyboard and touch panel work needs
two separate things solved, and it is worth being clear that they are separate:
a way for a key press on the host to reach the emulator process, and a way for
the emulator to tell the guest that something happened.

Two measurements decided the design.

**The firmware does not poll its touch controller.** Over a 240-second run it was
last addressed at line 1,449 of a 196,485-line trace — set up once, then never
read again. `BOARD_TOUCH_INT` is GPIO 12, and the firmware reads no GPIO input
registers at all, so it is sitting on a GPIO interrupt. Serving a report when
asked is not enough; nothing will ever ask.

(The keyboard turned out to be the opposite case: it is polled, and nothing
listens on `BOARD_KEYBOARD_INT` at all. Its model therefore needs a correct FIFO
and nothing else.)

**The fork's GPIO model cannot help.** `hw/gpio/esp32_gpio.c` implements exactly
one register, `GPIO_STRAP`. It calls `sysbus_init_irq` and then never raises it,
and it exposes no input lines to drive. There is nothing to reuse.

## Decision

1. **Model the GPIO interrupt path in Python.** Claim `0x6000403C` upward —
   `GPIO_IN`/`IN1`, `GPIO_STATUS`/`STATUS1` and their W1TS/W1TC aliases,
   `GPIO_PCPU_INT`/`INT1`, and the per-pin `GPIO_PINn` configuration carrying
   `INT_TYPE` (bits 7-9) and `INT_ENA` (bits 13-17). A pin change is evaluated
   against that pin's trigger type; a match sets its status bit.

2. **`GPIO_STRAP` at `0x38` is deliberately not claimed.** It is the one
   register the fork implements and the ROM reads it at boot. The claimed window
   starts immediately above it.

3. **Configuring a pin must not synthesise an edge.** A level trigger on a line
   already at that level latches immediately, because hardware is level
   sensitive. An edge trigger must not: firing one at `gpio_config()` time
   raises an interrupt before any handler exists, so nothing ever clears it, the
   line stays asserted, and no later edge is ever seen. This was a real bug and
   it presented as the interrupt simply never arriving.

4. **Interrupts are pushed out of band, over a second socket.**

   The first version of this ADR had the level ride the request/response path,
   reasoning that the guest makes hundreds of accesses per second so delivery
   would be prompt. That misread the mechanism. The bridge applies a level
   inside its own transaction handler, so a region's interrupt line only moves
   when the guest accesses **that region's window**. For a peripheral the guest
   polls this is fine — its own access is what raised the interrupt. For one it
   *waits* on it is a deadlock, because the handler that would touch the window
   is exactly what the interrupt is supposed to start.

   Measured before the fix: 499 edges latched, **one** delivered, on the single
   occasion the guest touched GPIO for unrelated reasons.

   QEMU now reads a second socket from its main loop through
   `qemu_set_fd_handler`, so the callback runs with the BQL already held and can
   raise any interrupt source at any moment. The same test afterwards: 758 level
   changes pushed, 5,113 handler runs, and the guest clearing status normally.

5. **Events arrive as a line-delimited JSON file** that the host appends to and
   the emulator tails. It crosses the container boundary on the mount that is
   already there, needs no protocol change, no TCP, and no new dependency —
   symmetric with the viewer, which already watches a file in the other
   direction.

6. **A reader thread tails the file into a queue; the bus thread drains it.**
   Device state stays owned by the bus thread, so there is no locking around the
   models. The queue is the only shared object.

## Consequences

**Good**

- Interrupt-driven peripherals are now serviceable at all, which is a
   prerequisite for the radio and the IMU as much as for touch.
- File-based input is trivially scriptable, so interaction tests need no GUI.
- The C footprint grew by one socket and one callback, and still contains no
  knowledge of any peripheral.

**Costs, accepted**

- Polling the input file adds latency of its own, bounded by the tail interval.
  Irrelevant against a panel that takes about a second to refresh.
- **Event timing must be expressed in guest time, not wall time.** Emulated time
  runs roughly an order of magnitude slower than the host clock, so a
  human-length tap is a few guest milliseconds and invisible to a UI sampling at
  30 ms. Anything generating input has to stretch its intervals to match, which
  is why the touch report rate is tuned against the wall clock rather than set
  to the datasheet's 100 Hz.
- We now own a meaningful slice of the GPIO block. If a future fork tag
  implements GPIO properly, this becomes duplicated work and should be dropped
  in favour of theirs.

## Alternatives rejected

**Drive QEMU's GPIO model through `qdev_get_gpio_in`.** Would reuse their edge
detection, if they had any. The model has no input lines and no interrupt logic.

**Make the devices poll-friendly and skip interrupts.** Works for the keyboard,
which is genuinely polled, and cannot work for touch: the firmware stops reading
it entirely after init.

**TCP from the host app straight into the emulator.** More moving parts than a
file on a mount both sides already see, and it would make the app and the
emulator care where each other run.

## Postscript: what this unblocked, and what it did not

Interrupts now reach the guest correctly and the touch driver services them. It
is still not possible to land a tap on a button, and the reason is not the input
path.

A full-screen e-paper refresh pushes about 19,200 bytes over SPI2, and each byte
costs roughly six bridge round trips. At the ~7,000 accesses per second the
bridge sustains, that is on the order of **seventeen seconds of wall time per
frame** — and LVGL is configured with `full_refresh = 1`, so it redraws
continuously. The main loop is therefore almost never inside `lv_task_handler`,
and `touchpad_read` is called a handful of times per run. A press and a release
have to be sampled in that order for a click to form, and there are not enough
samples for that to happen reliably.

That makes bridge throughput, not input, the next thing worth working on. The
first move is the per-access cost: the host runs a selector loop, so every
access pays a `select` on top of the `recv`/`send` pair, and QEMU blocks on each
one — which makes latency, not throughput, the figure that matters.
