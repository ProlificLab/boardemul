# ADR 0124 — The Max's test reboots from the same panic

**Status:** Accepted — 2026-10-02. Extends ADR 0114 from `test_boot_tdeck`
to `test_max_parts`.

## Context

`test_max_parts` failed now and then: twice in 52 runs, once with every check
after the radio failing at once, once as `guest exited (code 0)` after 5.5 s.
Neither left logs. Three loops of the test, run side by side with their logs
kept (`logs/max_repeat.sh`), caught it again within about sixty boots. The
demo had just printed `[SX1262] Initializing ... success!`, then:

    Guru Meditation Error: Core  1 panic'ed (Unhandled debug exception).
    Debug exception reason: Stack canary watchpoint triggered (ipc1)

That is ADR 0099's panic, and the Max's build traces the same way. It links
ESP-IDF v4.4.6, and the backtrace runs into `0xa5a5a5a5` stack fill, as there.
Its frames, named with `tools/app_symbols.py`:

- the faulting PC `0x40382773` is `s32i.n a8, a1, 28`, a store to the stack,
  in `spinlock_acquire`;
- its caller is `taskSelectHighestPriorityTaskSMP`, the scheduler choosing a
  task during the context switch a tick forces.

So it is the kernel-lock acquire writing four bytes past ipc1's 1024-byte
stack, during the GPIO ISR-service install that the radio driver's
`attachInterrupt` asks for, with a tick pending (ADR 0099). The overflow is
the firmware's, and silicon can hit it too.

A Max that hits it reboots and carries on. This test runs without
`--reboot`, so the reset ended QEMU and failed every check after the radio,
for a fault that is not the emulator's. ADR 0114 settled the same question for
the T-Deck boot test.

## Decision

1. **`test_max_parts` runs with `--reboot`**, and waits for the demo's last
   check in the last boot.
2. **The board is judged on its last boot.** Each check reads what the demo
   said on its USB serial port since the chip last came out of reset
   (`last_boot`). The ROM writes its banner there too (ADR 0114, decision 4).
3. **Only ADR 0099's panic is forgiven, by name, and every reboot is
   accounted for.** Two new tests use the helpers ADR 0114 introduced:
   - `unforgiven` fails any boot that ended in a different panic;
   - `unexplained_resets` fails a reboot for any other reason.

   A boot that panicked some other way, or reset without a panic, fails as
   before.
4. **A pass that needed a second boot says so**, as a warning with the boot
   count, as the boot test does.

## Consequences

**Good**

- The test fails for the emulator's faults and the demo's verdicts, not for a
  firmware overflow a board survives.
- A different panic or an unexplained reset still fails it, now by name.

**Costs, accepted**

- A run that hits the canary takes one more boot, about ten seconds of wall
  time.
- On the wall clock the emulator makes the canary likelier than on silicon
  (ADR 0099), so this test reboots more often than a board would. Here it was
  4 boots in about 147, three loops sharing the host.

## Measured

The loops ran 95 boots, and this decision took effect partway through.
Before it, one boot failed with the canary: the run captured above. After it,
one hit the canary, rebooted, and passed with the boot-count warning.
