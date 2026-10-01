# ADR 0099 — A board that panics may reboot, as silicon does

**Status:** Accepted — 2026-10-01. Found while chasing the `test_mesh` node
that sometimes never initialised its radio.

## Context

Under load, the vendor's 250620 build and Meshtastic 2.7.10 both sometimes
stopped with the same panic. The frame chain was the same in both, with the
same stack pointers:

    Guru Meditation Error: Core  1 panic'ed (Unhandled debug exception).
    Debug exception reason: Stack canary watchpoint triggered (ipc1)

It happened on about one loaded boot in twenty. In `test_mesh` it was the node
that never initialised its radio, about one run in ten. The addresses below
are the vendor build's. Meshtastic was measured the same way, over eight boots
with one panic. Its ipc1 guards the same stack end and goes as deep on every
boot, and its panic's stack holds the return into the same IPC function
(`0x420f8110`). The backtrace ran three
frames into `0xa5a5a5a5` stack fill, which looked like a watchpoint firing for
nothing, so QEMU's Xtensa data breakpoints were the suspect.

**They are not at fault.** The `report-watchpoint-hits` experiment (see
`qemu-patch/experiments/README.md`) logs every hit against what the guest had
asked to watch. It also adds a second watch, which the guest never sees, that
logs how close each stack comes to its end. Over 79 loaded boots, four
panicked, and every hit was real:

- On every boot, ipc1 runs the GPIO ISR-service install that the radio driver's
  `attachInterrupt` asks for: `gpio_isr_register_on_core_static` →
  `esp_intr_alloc` → `heap_caps_malloc` → `multi_heap`. ESP-IDF 4.4 sends that
  through `esp_ipc_call_blocking` even for the calling core. ipc1's stack is
  1024 bytes (`movi a12, 0x400` at its creation). At the deepest point of this
  path, 216 bytes are left.
- Every tick on core 1 ends in a context switch. In an ISR on core 1, this
  firmware's `xTaskIncrementTick` returns true without looking (`0x403807ea`).
  The switch runs on the interrupted task's own stack, below the 192-byte frame
  the interrupt saved there.
- When a tick is pending as that path's heap lock ends, it fires at the ROM's
  interrupt-level restore, with SP 300 bytes from the end. The frame and
  `vTaskSwitchContext`'s first two calls need 304 bytes, so the kernel-lock
  acquire stores 4 bytes past the stack. That store is inside the canary, and
  the watchpoint is right to fire.

So the overflow belongs to the firmware, and silicon can hit it too. The
emulator makes it more likely: on the wall clock, a loaded guest gets far
fewer instructions per tick than a 240 MHz core (ADR 0027). A tick is then
much more likely to be pending when the critical section ends.

**What the emulator got wrong was afterwards.** After the panic the firmware
prints `Rebooting...` and resets the chip. On a T-Deck that is the end of it:
the board boots again and carries on. Every run here has passed `-no-reboot`
since the first launcher, so the reset ends QEMU instead. One unlucky tick
leaves a dead board, which is not what the hardware would do. Allowed to
reboot, a board that panicked printed `rst:0xc (RTC_SW_CPU_RST)`, initialised
its radio again and went on to join its access point. Its peripherals came back
from the reset as ADR 0035 arranged.

## Decision

1. **No QEMU change.** The watchpoint emulation is correct. The diagnostic
   stays an experiment, so the next canary panic starts from it rather than
   from suspecting QEMU again.

2. **`run.py --reboot` lets a guest reset restart the chip** by leaving out
   `-no-reboot`. It is off by default. A reset that ends the run is how a
   single-board test sees a reboot happen: `test_meshtastic_touch` waits for
   the restart its setup dialog asks for. A run with `--reboot` ends on its
   stall watchdog or its timeout instead. The manifest's `argv` records which
   was used.

3. **`test_mesh` runs its nodes with `--reboot`.** A node in a mesh is a
   device, and a device that panics at boot comes back. The mesh is judged on
   what it does after that. Each node's boot count, read from the `rst:` lines
   in its UART, goes into the fixture's result and into the message of any
   assertion that fails, so a reboot is never hidden.

4. **The crash's rate is not made faithful here.** `--icount` would do that
   (ADR 0027), at several times the runtime of a test that does not otherwise
   need it.

## Consequences

**Good**

- The one known cause of `test_mesh`'s dead node is gone, by doing what the
  board does rather than by avoiding the firmware path.
- The canary panic is explained down to the store, and the explanation is
  reproducible from a named experiment.

**Costs, accepted**

- A `--reboot` run can pass with a board that panicked once. The boot count
  is what keeps that visible.
- A firmware stuck in a boot loop under `--reboot` prints all the time, so the
  stall watchdog never fires and the run goes to its timeout.

## Alternatives rejected

**Grow ipc1's stack, or skip the tick, from the emulator.** Either one changes
the firmware's behaviour to suit the emulator. That is the line ADR 0006 draws.

**Run `test_mesh` under `--icount`.** It brings the panic down to the rate
silicon would see, but not to zero, and a board that panics still dies under
`-no-reboot`. It also costs about seven times the runtime (ADR 0027's
measurement).

**Retry the run.** That hides a real behaviour instead of modelling it.
