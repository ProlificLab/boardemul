# ADR 0013 — Enable the FPU at reset, per board, and say so out loud

**Status:** Superseded by [ADR 0143](0143-the-fpu-is-on-out-of-reset-as-on-silicon.md)
— 2026-11-18 (accepted 2026-08-27). Upstream measured silicon: CPENABLE resets
to 0xff, and QEMU now does the same itself, so the device this ADR added is
gone. The account of the panic below stands, and so does the reading it chose.

## Context

The T-Deck's own vendor firmware panics about a second in, before printing
anything of its own:

    Guru Meditation Error: Core 0 panic'ed (Coprocessor exception).

Three builds spanning June to December 2026 do it identically, and so does plain
QEMU with none of our devices attached, so it is upstream of everything we wrote.

What actually happens:

- The firmware executes one floating-point instruction, `lsi f0, a1, 12`, at
  `0x4204e844`. The function around it stores a literal into `*this` and spills
  an argument across a call before reloading it as a float: a C++ constructor
  for a polymorphic class taking a `float`.
- The backtrace runs ROM to second-stage bootloader to `call_start_cpu0`+0x22d,
  still on the bootloader's stack. That is `do_global_ctors`, before FreeRTOS
  exists.
- QEMU logs exactly one user exception in the entire run, at that PC, with zero
  interrupts before it. The scheduler had never started.
- FreeRTOS on Xtensa uses a lazy FPU. Its context switch ends
  `movi.n a3, 0; wsr.cpenable a3` -- it disables the FPU on purpose for every
  task, so the first float traps and the handler assigns ownership. Outside a
  task there is no save area, so IDF takes its "coprocessor exception outside a
  thread" path, overwrites EXCCAUSE with its own reason code 4, and panics. That
  is why the register dump reads `EXCCAUSE: 0x04` while printing "Coprocessor
  exception": 4 is IDF's reason code, not an exception cause.
- The firmware never reads or writes CPENABLE anywhere -- not in ROM, not in the
  bootloader, not in the app. It assumes the FPU is already on.

QEMU is faithful on every point that could have been the bug. CPENABLE resets to
zero, which is the architectural value; the `= 0xff` in `target/xtensa/cpu.c` is
inside `#else` and applies to user-mode emulation only. Every floating-point
opcode gates on coprocessor 0, matching silicon and matching what IDF's handler
expects. The exception raised is an ordinary user exception. There is no defect
here to fix.

**What we could not settle.** Whether ESP32-S3 silicon resets CPENABLE to zero.
We have no hardware and no TRM. LilyGO ships this as the board's own
verification firmware in three builds across six months, which argues that
silicon leaves the FPU enabled. But a float in a constructor is also a known
ESP-IDF failure mode on real hardware, and newer IDF does not enable CPENABLE at
startup either. Both readings survive the evidence we have, and this ADR is
written on the first one without being able to prove it.

What bounds the risk either way: the scheduler writes CPENABLE explicitly on
every context switch, so whatever value reset leaves is overwritten as soon as
the first task runs. The only window this can affect is between reset and the
scheduler.

## Decision

1. **A device we add,** `esp-coproc-enable`, writes CPENABLE on every CPU after
   reset. No edits to QEMU's own logic.

2. **Off by default.** A board opts in by listing the device, so the divergence
   is declared in the board file where anyone reading that board can see it,
   rather than applying silently to hardware that never asked. The T-Deck Max
   and Meshtastic do not need it and do not get it.

3. **`mask` is a property, defaulting to `0x1`** -- coprocessor 0, the FPU. That
   is the minimal claim the evidence supports. A board that needs more has to
   say so and justify it.

4. **It lives in `hw/xtensa/`,** not in `hw/misc/` with the other four, because
   it touches `CPUXtensaState` and only target-specific sources can include the
   target's `cpu.h`.

5. **Ordering comes free from registration order,** not from a notifier dance.
   The SoC registers its own reset handler -- the one that calls `cpu_reset()` --
   during machine init. Devices named with `-device` are created afterwards, in
   `qemu_create_cli_devices()`. Reset handlers run in registration order, so
   ours is appended second and runs after the CPU has been reset. A
   guest-initiated reset goes through `qemu_system_reset_request()` and therefore
   through the same list, so it is covered too.

## Consequences

**Good**

- The T-Deck's own vendor firmware boots instead of panicking in its first
  second, which is the thing this emulator exists to do.
- The inaccuracy is one line in one board file and one ADR, not a behaviour
  buried in a patch to someone else's CPU model.
- Guest-initiated resets are covered by the same handler, so a firmware that
  restarts itself does not regress on the second boot.
- Our C footprint stays in files we own. QEMU's tree still carries only build
  lines.

**Costs, accepted**

- **We may be more forgiving than silicon.** If the ESP32-S3 really does reset
  CPENABLE to zero, we now run firmware that a real T-Deck would panic on. That
  direction of error can hide a genuine firmware bug from someone testing
  against us; it cannot break firmware that is correct. If hardware ever becomes
  available, this is the first thing to check.
- A second `meson.build` of theirs now carries a line, so the tree diff goes
  from four lines to five.
- The ordering argument leans on QEMU internals -- registration order, and CLI
  devices being created after machine init. The tag is pinned, and if a rebase
  ever broke it the failure is loud: the firmware panics exactly as it does
  today.

## Alternatives rejected

**One line in their tree,** in `hw/xtensa/esp32s3.c` beside where they already
set `PRID`, or in `target/xtensa/cpu.c`. Trivial to write and arguably where it
belongs. Rejected because the boundary in ADR 0001 is what keeps this project
rebasable onto a new fork tag, and it survives only while nothing is scattered
into files we do not own. One line is how that starts.

**On by default for every board.** Simpler, and no board file would mention it.
Rejected precisely because no board file would mention it: the T-Deck Max and
Meshtastic never execute a float before the scheduler, so they would be silently
made more permissive in exchange for nothing.

**Leave it, and target Meshtastic on the T-Deck.** Honest, and it was the
fallback. Rejected because the vendor's own firmware is the benchmark this
project set itself, and a third-party build reaching further is not the same
achievement.

**Patch the guest.** Enabling CPENABLE in the firmware image would mean no
longer running the unmodified vendor binary, which is the premise of the whole
exercise.
