# ADR 0010 — Deliver input by modelling GPIO interrupts and tailing a file

**Status:** Accepted — 2026-08-25

## Context

The viewer (ADR 0009) is one-way. Making the keyboard and touch panel work needs
two separate things solved, and it is worth being clear that they are separate:
a way for a key press on the host to reach the emulator process, and a way for
the emulator to tell the guest that something happened.

Two measurements decided the design.

**The firmware does not poll its input devices.** Over a 240-second run the
keyboard controller was last addressed at line 2,651 of a 196,485-line trace,
and the touch controller at line 1,449. Both are set up once and then never read
again. `BOARD_KEYBOARD_INT` is GPIO 15 and `BOARD_TOUCH_INT` is GPIO 12, and the
firmware reads no GPIO input registers at all — so it is sitting on GPIO
interrupts. Serving a key event when asked is therefore not enough; nothing will
ever ask.

**The fork's GPIO model cannot help.** `hw/gpio/esp32_gpio.c` implements exactly
one register, `GPIO_STRAP`. It calls `sysbus_init_irq` and then never raises it,
and it exposes no input lines to drive. There is nothing to reuse.

**But there is a dense heartbeat.** LVGL runs with `full_refresh = 1`, so the
panel is redrawn continuously; the last 2,000 trace lines of an idle run are all
SPI2. The guest touches emulated MMIO constantly even when it looks idle.

## Decision

1. **Model the GPIO interrupt path in Python.** Claim `0x6000403C` upward —
   `GPIO_IN`/`IN1`, `GPIO_STATUS`/`STATUS1` and their W1TS/W1TC aliases,
   `GPIO_PCPU_INT`/`INT1`, and the per-pin `GPIO_PINn` configuration carrying
   `INT_TYPE` (bits 7-9) and `INT_ENA` (bits 13-17). A pin change is evaluated
   against that pin's trigger type; a match sets its status bit.

2. **`GPIO_STRAP` at `0x38` is deliberately not claimed.** It is the one
   register the fork implements and the ROM reads it at boot. The claimed window
   starts immediately above it.

3. **The interrupt reaches the guest through the existing bridge mechanism.**
   The GPIO region declares `irq = 16` (`ETS_GPIO_INTR_SOURCE`) and its
   `irq_level()` is the OR of enabled status bits. No new C: ADR 0005's reply
   already carries an interrupt level.

4. **Interrupt latency rides the MMIO heartbeat.** The level is sampled on every
   bridge access, and the guest makes hundreds per second, so an injected event
   is delivered in well under a millisecond. This is a real coupling and it is
   recorded as such: a guest that genuinely stopped touching intercepted MMIO
   would never see the interrupt. Nothing this firmware does gets close, and the
   proper fix — a second socket with `qemu_set_fd_handler` — is deferred rather
   than dismissed.

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

- Keyboard and touch become possible without touching the C, without a second
  socket, and without moving the emulator out of the container.
- Modelling GPIO interrupts is reusable: every remaining interrupt-driven
  peripheral on this board needs the same path.
- File-based input is trivially scriptable, which makes automated interaction
  tests possible later without a GUI.

**Costs, accepted**

- Polling the input file adds latency of its own, bounded by the tail interval.
  Irrelevant against a panel that takes about a second to refresh.
- **Event timing must be expressed in guest time, not wall time.** Emulated time
  runs roughly an order of magnitude slower than the host clock, so a
  human-length tap is a few guest milliseconds and invisible to a UI sampling at
  30 ms. Anything generating input has to stretch its intervals to match, and
  the touch report rate is tuned in wall time for that reason.
- We now own a meaningful slice of the GPIO block. If a future fork tag
  implements GPIO properly, this becomes duplicated work and should be dropped
  in favour of theirs.

## Alternatives rejected

**Drive QEMU's GPIO model through `qdev_get_gpio_in`.** Would reuse their edge
detection, if they had any. The model has no input lines and no interrupt logic.

**Make the devices poll-friendly and skip interrupts.** Cannot work: the
firmware stops reading these devices entirely after init.

**TCP from the host app straight into the emulator.** More moving parts than a
file on a mount both sides already see, and it would make the app and the
emulator care where each other run.


## Postscript: what this unblocked, and what it did not

Interrupts now reach the guest correctly and the touch driver services them. It
is still not possible to land a tap on a button, and the reason is not the input
path:

A full-screen e-paper refresh pushes about 19,200 bytes over SPI2, and each byte
costs roughly six bridge round-trips. At the ~7,000 accesses per second the
bridge sustains, that is on the order of **seventeen seconds of wall time per
frame** � and LVGL is configured with `full_refresh = 1`, so it redraws
continuously. The main loop is therefore almost never inside `lv_task_handler`,
and `touchpad_read` is called a handful of times per run. A press and a release
have to be sampled in order for a click to form, and there are not enough
samples.

That makes bridge throughput, not input, the next thing worth working on. The
obvious first move is the per-access cost: the host currently runs a selector
loop, so every access costs a `select` on top of the `recv`/`send` pair, and
QEMU blocks on each one, which makes latency rather than throughput the figure
that matters.
