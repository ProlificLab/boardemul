# ADR 0114 — The boot test forgives one panic, by name

**Status:** Accepted — 2026-10-01. Extends ADR 0099's decision 3 from
`test_mesh` to `test_boot_tdeck`.

**Amended 2026-10-02:** decision 3's exception for a boot of nothing but the
ROM's banner is withdrawn by
[ADR 0123](0123-one-core-resets-the-other-and-holds-it.md). The doubled
banner was not the second reset request restarting the ROM, as guessed below.
QEMU did not hold core 0 where the restart stalls it, and core 0 ran its ROM
between its two resets. With that fixed, a restart prints one banner, so a
banner-only boot is now an unexplained reset.

## Context

`test_boot_tdeck` boots each vendor T-Deck UnitTest build once, in a module
fixture, and every test reads that one run. On about one loaded boot in
twenty, by ADR 0099's count, build 250620 stops with the panic that ADR
traced to the firmware:

    Guru Meditation Error: Core  1 panic'ed (Unhandled debug exception).
    Debug exception reason: Stack canary watchpoint triggered (ipc1)

ipc1's 1024-byte stack overflows by four bytes when a tick is pending as the
GPIO ISR-service install ends its heap lock. Silicon can hit it too, and a
T-Deck that does reboots and carries on. ADR 0099 let `test_mesh`'s nodes do
the same, and left single-board tests ending at the reset. This test was one
of them, so one panic failed four tests together, as it did in the 2026-10-01
slow suite. The FPU test found "Guru Meditation". The Arduino-layer,
radio bring-up and battery tests found a run that had ended. On 250620 the
panic comes while the radio starts, 13–17 s into an unloaded run. That is
before the station, the first Arduino log line and the first battery read.

None of the four had found a fault in the emulator. But the first of them is
the regression the board exists for. Without `esp-coproc-enable` every build
dies a second in with ADR 0013's "Coprocessor exception". Letting the board
reboot must not turn that into a pass. It must not turn it into a boot loop
that runs to the timeout before failing, either.

## Decision

1. **`test_boot_tdeck` runs with `--reboot`.** `run_emulator` already passes
   its `extra` arguments to `run.py`, so the runner does not change.

2. **One panic is forgiven, by its reason.** `tests/support.py` names it
   (`IPC1_CANARY`): core 1, unhandled debug exception, stack canary watchpoint
   on ipc1. A boot is judged by its first panic, because that is why the boot
   ended. If that panic is anything else, in any boot, the FPU test fails.
   "Coprocessor exception" fails it wherever it is printed. A canary on
   another task's stack is not forgiven, and neither is a panic cut off before
   its reason line.

   A panic after the first one in the same boot comes from the panic handler
   itself. The first real reboot this test saw showed why that matters. On
   250620 the canary's core dump faults, so the UART shows a second
   "Guru Meditation" (`LoadProhibited`), then "Re-entered core dump!
   Exception happened during core dump!", then the reset. The first version
   of this rule judged every panic, and it failed that run. The older logs it
   had been checked against came from a firmware with no core dump, and the
   two lines ADR 0099 quotes stop before it.

3. **Every reboot is accounted for.** Each boot but the last must have ended
   in the forgiven panic (`unexplained_resets`). A reset for any other reason,
   such as a watchdog or an abort, used to end the run and fail the tests
   after it. Now `test_it_reboots_only_from_the_forgiven_panic` fails instead.

   There is one exception, also found under load: a boot that printed nothing
   past the ROM's banner. Both canary reboots that ran to the end printed
   `rst:0xc` twice, three lines apart, before booting once. ESP-IDF's
   restart, run from core 1, resets core 0 and then core 1. QEMU's ESP32-S3
   turns each CPU reset into a request to reset the machine
   (`esp32s3_cpu_reset` in `hw/xtensa/esp32s3.c`). The likeliest reading is
   that the second request restarts the ROM that has just printed its banner.
   That was not traced further. A boot like that ran no code of its own, so
   the boot before it accounts for it, and it cannot be the first boot. The
   boot count still counts banners, so that reboot reads as three boots.

4. **The board is judged on its last boot.** The UART is cut at its last
   `rst:0x` banner, the count `test_mesh` uses, and the walls are looked for
   after it. The USB serial port is cut the same way, because the ROM writes
   its banner there too. Frames are the exception. The panel is a part on the
   board, not in the chip, so it does not reset with the chip and its frame
   numbers run on across a reboot. A boot that ended in the forgiven panic had
   drawn 2 or 3 frames by the time it started the radio (measured on three
   boots), against the 100 the test asks for. So the frame count stays the
   run's.

5. **The boot count is reported.** It counts `rst:0x` banners, as `test_mesh`
   does. It is in every failure message. A run that needed more than one boot
   also raises a warning, so a pass that took a reboot shows in the suite's
   summary.

6. **A panic that is not forgiven ends the run once the board is back from
   it.** By then the panic is in the log whole and the FPU test has its
   answer. Waiting longer would only watch a boot loop run to the 300 s
   timeout.

## Verification

All in the container. The slow runs used a QEMU built from this commit's
`qemu-patch` into a private volume. The shared `third_party/qemu` was carrying
another session's uncommitted device changes, and it was rebuilt twice while
this work ran.

- The new fast tests (`tests/test_forgiven_panic.py`, 13) check the helpers
  on the text the firmware prints. The canary is forgiven, and so is its core
  dump faulting after it. ADR 0013's panic, a canary on `loopTask` or on
  ipc0, a panic cut short, a reset with no panic before it, and a boot of
  nothing but the ROM's banner as the first boot are not.
- Both builds, run alongside eight loaded boots: 13 passed and 1 skipped, as
  before. Each booted once.
- With `esp-coproc-enable` removed from the board (in the container only), on
  250620: the Coprocessor exception panicked the first boot, the board
  rebooted with `rst:0xc (RTC_SW_CPU_RST)`, and the run stopped there. It took
  8 s including pytest's start-up, not 300. All seven tests failed. The FPU
  test failed on "Coprocessor exception", and the reboot test failed on
  "boot 1 of 2 ended without the ipc1 canary panic".
- Under load, eight containers each booting 250620 through this module over
  and over: 101 runs. 99 booted once and passed. Two hit the canary, 1 in 50
  at this load, and both printed the doubled banner. The first, under an
  earlier draft of decision 3, failed only the reboot test; on its logs the
  rules above pass all seven. The second ran under the final rules and passed
  all seven, 103 frames in all, with the warning "build 250620 booted 3
  times".
- Before that, 52 runs on the shared QEMU produced one canary reboot. That is
  the run that showed the core dump faulting (decision 2).

## Consequences

**Good**

- One unlucky tick no longer fails four tests. The board does what silicon
  does, and the test watches it come back.
- The FPU regression fails as it did, and about as fast.
- A reboot is never silent: it is counted in failures and warned of in passes.

**Costs, accepted**

- A run that rebooted takes one boot longer, about 15 s more on 250620.
- The forgiven panic is identified by its text. A different overflow of
  ipc1's stack, on core 1, would be forgiven too. The warning is what would
  show it.
- What the panic handler does after the canary is not judged. Only one form
  of it has been seen, the core dump faulting. Another form would also pass,
  behind the canary.
- A boot loop on the forgiven panic itself still runs to the timeout, as ADR
  0099 accepted for `test_mesh`.
- Frames from the panicked boot count towards the run's total, 2 or 3 of the
  100 asked for.

## Alternatives rejected

**Retry the fixture.** That hides a real behaviour instead of modelling it,
as ADR 0099 said of the mesh test.

**Skip or xfail when the canary shows.** It throws away one run in twenty for
a board that would have carried on, and the walls after the reboot go
unchecked in that run.

**Forgive any panic.** A new panic would pass, wherever it came, as long as
the board came back from it. The FPU test would then rest on the Coprocessor
exception's text alone.

**Run under `--icount`.** As ADR 0099 found for the mesh: several times the
runtime, and the panic gets rarer but does not go away.

**Count frames per boot.** The bus log records each frame but logs the chip's
reset only at debug level. Splitting frames by boot would take an emulator
change, or a test that times the banner against the frames. Neither is worth
correcting 2 or 3 frames out of 100.
