# ADR 0123 — One core resets the other, and holds it

**Status:** Accepted — 2026-10-02. Withdraws ADR 0114's exception for a boot
of nothing but the ROM's banner (its decision 3).

**Amended 2026-10-02:** decision 4 is reversed for the probe. It is now a
slow test, `tests/test_restart_probe.py`, which boots
`firmware/restart_probe.bin` as built by `tools/restart_probe/build.py` and
skips when that image is absent. It is the first slow test to boot a
firmware of ours rather than a vendor's (ADR 0012, decision 1). It checks one
ROM banner per restart from each core, on one translation thread and on a
thread per core, and that without `--reboot` a restart from core 1 still ends
the run. Without this ADR's patch the one-thread arm fails all four of its
checks: three banners for core 1's first restart, then an assertion inside
FreeRTOS. The thread-per-core arm passed without it, and the `-no-reboot`
check passes either way by design, so those two guard the patch's other paths
rather than the defect.

**Upstream, 2026-11-18:** Landed upstream reworked (39341cc5) in `esp-develop-9.2.2-20261109`: a held core is released 20 ms of virtual time after the reset rather than at the next stall report, which this ADR had rejected as a guess about the host. The patch is dropped (ADR 0138). On it the restart probe runs its whole plan with one ROM banner per restart, in all three of its runs (2026-11-18).

## Context

ADR 0114 saw the T-Deck boot test's canary reboots print the ROM's banner
twice, three lines apart, before booting once:

    rst:0xc (RTC_SW_CPU_RST),boot:0x4 (SPI_FLASH_BOOT)
    ESP-ROM:esp32s3-20210327
    Build:Mar 27 2021
    rst:0xc (RTC_SW_CPU_RST),boot:0x4 (SPI_FLASH_BOOT)
    SPIWP:0xee

It guessed that the restart's second CPU reset restarted the ROM, forgave a
boot like that, and did not trace it further. This ADR traces it.

**What the firmware does.** The restart is ESP-IDF 4.4's `esp_restart_noos`,
at `0x40377d5c` in build 250620. The panic handler ends in it, and so does
`esp_restart()`. On whichever core runs it, it writes three CPU resets to
RTC_CNTL. Run on core 1, which is where the canary panics:

1. reset core 0 and stall it at once (`0x40377e1a`, `0x40377e1f`): "CPU must
   be reset before stalling, in case it was running a s32c1i instruction";
2. reset the peripherals, drop the CPU to the crystal, clear the APP CPU's
   entry point;
3. reset core 0 again, release it, and reset core 1 (`0x40377ed8`–`ee5`).

The panic handler has also stalled core 0 before printing anything
(`SOC_HAL_STALL_OTHER_CORES`). On silicon core 0 runs a few instructions of
ROM between the first reset and the stall, none between the second reset and
the release, and boots once.

**What QEMU did.** Two things in `hw/xtensa/esp32s3.c`:

- `esp32s3_cpu_stall` was an empty function. RTC_CNTL worked out the stall
  and nothing held the core. The ESP32 machine in the same fork does hold it
  (`esp32_cpu_stall` calls `xtensa_runstall`).
- Every CPU reset was `qemu_system_reset_request`. The main loop paused both
  cores, reset the named core and every sysbus device, and resumed. On one
  translation thread, which every run uses (ADR 0041), core 0 then ran
  first, for a whole time slice, as measured below.

**Measured.** `qemu-patch/experiments/report-cpu-resets.patch` logs each
reset the guest asks for, each stall, and each machine reset.
`tools/restart_probe` restarts the chip once from each core, by
`esp_restart()` and by `abort()`, on the same ESP-IDF as the vendor builds.
A restart from core 1, unloaded:

| guest time | what happened |
| --- | --- |
| 1781.0 ms | core 1 resets core 0; machine reset |
| 1887.7 ms | core 1 stalls core 0, **106 ms later**: core 0 had the slice first |
| 1899.9 ms | core 1 resets core 0 again; core 0 is at `0x4003553e`, deep in ROM |
| 2001.9 ms | core 1 resets itself; core 0 is at `0x403ccbe8`, in the bootloader |

Three runs, two restarts from core 1 in each: 2 and 2 banners, 3 and 4, 3
and 3. Every restart from core 0 printed one. Where there were three, a boot
between them got as far as `entry 0x403c98b8`. That is past the ROM, so ADR
0114's rule would have failed the run. In the
second run core 1 took a `Double exception` at `0x4037a61a`, inside its own
restart. Its backtrace was corrupt, and its return address was an address on
its own stack (`0x3fced3a0`), so its stack had been written over. The only
other thing running was core 0, in the ROM and the bootloader, which load
into internal RAM (`load:0x3fce3808`) with no idea the app is still there.
Core 1 panicked again, and the chain ended in `rst:0x3 (RTC_SW_SYS_RST)`.

So the second banner is not core 1's own reset restarting core 0. That reset
names core 1, and the machine reset leaves core 0 alone. The banners come from
core 0 running between its two resets, which it should not do at all, for a
whole time slice. Load has nothing to do with it: every restart from core 1
does it. Load only made the canary happen, and the canary is the only restart
from core 1 the tests had seen.

## Decision

1. **Carry `qemu-patch/core-reset-and-stall.patch`.** It changes only
   `hw/xtensa/esp32s3.c`:
   - **A stall holds the core.** `esp32s3_cpu_stall` calls `xtensa_runstall`,
     as the ESP32 machine does. Resetting the core does not release it:
     `xtensa_cpu_reset_hold` keeps `runstall`, and on silicon the stall is RTC
     state. A release clears a halt the core never got to take. On one thread
     the release does not kick the scheduler. Nothing is asleep there to wake,
     and a kick would end the releasing core's turn before its next
     instruction, which in this restart is its own reset.
   - **A core that resets the other core resets that core alone, at once.**
     That is what silicon's CPU reset does. On one thread the reset core is not
     running, so it is reset on the spot. With a thread per core it is reset on
     its own thread (`async_run_on_cpu`). There is no machine reset, so the
     core that asked keeps its turn.
   - **The reset core is held until the core that reset it next writes the
     stall controls.** On silicon it runs a few instructions in that gap. Here
     the resetting core can lose its turn there, to any interrupt the main
     loop delivers, and the core it reset would then run for the whole slice.
     RTC_CNTL raises the reset inside a write to `OPTIONS0` and reports the
     stall line in that same write, so the hold outlasts that report and ends
     at the next one. In ESP-IDF's restart that is the very next write, the
     stall. A machine reset ends any hold.
   - **A core that resets itself reboots the machine, as before.** The named
     core and every sysbus device are reset. The cause is now a guest reset for
     either core, so under `-no-reboot` a restart from core 1 still ends the
     run, at its last step rather than its first. A reset from outside the
     cores keeps the old causes.

   Each part was needed, and each was found missing by measuring:
   - Stalling alone: the stall still came after core 0's time slice. All three
     runs hung, two of them at their first restart: core 0 was stalled in the
     bootloader, and core 1 never got to its second reset. The run that got
     past its first restart still printed two banners there.
   - Stalling and resetting alone, without the hold: the probe's 44 restarts
     all printed one banner. But a sketch that restarts from core 1 every few
     hundred milliseconds printed an extra banner about once in a hundred
     restarts. In each one the diagnostic showed core 0 had run into the
     bootloader before its second reset (`0x403c9efa`, `0x403c9ca4`). Core 1
     had lost its turn between the reset and the stall. A throwaway build that
     ends core 1's turn at exactly that point made it happen on every
     restart.

2. **Reported upstream (ADR 0017)** as espressif/qemu#189, and offered as
   #190. That PR is the same diff as the patch, against `esp-develop`
   (`febae182`), from ProlificLab/qemu's `fix/esp32s3-cpu-reset-and-stall`.
   `esp-develop` still had the empty `esp32s3_cpu_stall`, and no issue or pull
   request there mentioned the stall or a restart printing twice (checked
   2026-10-02). Carried until it lands.

3. **ADR 0114's exception for a banner-only boot is withdrawn.**
   `unexplained_resets` no longer excuses one, and `rom_only` is gone. After
   this patch a restart prints one banner, so another one would mean the
   restart had gone wrong again. `test_mesh`'s boot count now counts boots.

4. **The diagnostic stays an experiment, and the probe stays out of the
   suite** (reversed for the probe: see the amendment above). The slow tier boots vendor firmware (ADR 0012), and the probe is
   ours. Its source is kept in `tools/restart_probe`, with the commands to
   build and run it, so the measurement can be repeated.

## Verification

All in the container. The runs used a QEMU built into a private volume. The
shared `third_party/qemu` carried other sessions' work in progress (logging
in `accel/tcg/*` and `hw/xtensa/pic_cpu.c`, a `blk_drain` in
`hw/misc/esp32s3_cache.c`), so the private copy started from the tag and
took only the patches in `qemu-patch`. Everything below is the final patch
unless it says otherwise. The patch passes QEMU's `checkpatch.pl` with 0
errors and 0 warnings.

- **One restart, traced.** With the diagnostic, a restart from core 1 runs:
  core 0 reset at 2136.92 ms, stall at 2138.44 ms, second reset at
  2151.85 ms with core 0 still at its reset vector (`0x40000400`). Core 0 is
  released, core 1 resets itself at 2152.26 ms with core 0 still at the
  reset vector, and the machine reset follows at 2152.67 ms.
- **The probe:** 20 of 20 restarts with one banner, from both cores, by
  `esp_restart()` and by `abort()`. Three runs were on one translation thread
  and two on a thread per core (`BOARDEMUL_TCG_THREADS=multi`).
- **A sketch that restarts from core 1 every few hundred milliseconds**
  (`esp_restart()` from a task pinned to core 1): 120 restarts with no extra
  banner, half on each thread mode. With the diagnostic, in 24 runs across
  four loaded containers: 361 restarts, and in every one core 0 was still at
  its reset vector when it was reset the second time. No restart printed a
  second banner. The patch without the hold had shown core 0 in the
  bootloader there twice in about 220 traced restarts.
- **The worst moment, forced.** A throwaway build ends core 1's turn right
  after it resets core 0. With the hold: 38 restarts, all clean. Without it:
  every restart printed two banners, with the stall arriving 87 ms late and
  core 0 in the bootloader.
- **Without `--reboot`**, in both thread modes, the first restart from core 1
  ends the run (`guest exited (code 0)`), as before.
- **During `abort()` on core 1**, core 0 is held. The panic's output is the
  same text as before, without the extra boot.
- **The fast tier passes.** `test_forgiven_panic` now asserts that a boot of
  only the banner is an unexplained reset.
- **Rebased onto ADR 0119**, with all nine carried patches, again: the fast
  tier, the probe's 20 restarts, 123 of the sketch's, and `-no-reboot`, all as
  above. Meshtastic, booted for 90 s with the diagnostic, stalled and reset no
  core at all, so outside a restart or a panic none of this runs.
- **Rebased again onto ADR 0122**, with all ten carried patches: the fast
  tier, the probe's 20 restarts, 67 of the sketch's, and `-no-reboot`, all as
  above. `test_boot_tdeck` and `test_mesh` together gave 20 passed, 1 skipped
  and 1 failed: `test_mesh`'s range-and-tuning check. Run alone, `test_mesh`
  passed 4 of 4 twice with this patch and twice without it.
- **The slow suite, rebased onto ADR 0119:** 71 passed, 1 skipped (251113
  starts no station) and 5 failed. Three were `test_meshtastic_touch`, as
  below. Two were `test_gps_boot`, where the firmware never found its L76K.
  That run
  booted once, so no reset was involved. Run alone, `test_gps_boot` passed 2
  of 2 with this patch and 2 of 2 without it. Before the rebase, the final
  patch got 71 passed, 1 skipped and 1 failed: `test_mesh`'s packet crossing.
  There both nodes had booted once, and their receptions were cut off or
  deaf: radio timing, with no restart in either node.
- **ADR 0114's own case, under ADR 0114's load**, rebased onto ADR 0119:
  eight containers, each booting 250620 through `test_boot_tdeck` again and
  again, 104 runs, all passed. Two hit the ipc1 canary. Each printed the
  canary, the core dump faulting and "Re-entered core dump!",
  "Rebooting...", and then one `rst:0xc` banner and a normal boot. That is two banners for the run, not
  three, and the test warned "booted 2 times".

The version of the patch without the hold also ran the slow suite, alongside
another session's containers. It got 66 passed, 4 skipped (the usual skips)
and 3 failed, all in `test_meshtastic_touch`, whose setup dialog did not
appear within its 480 s. That test stops its run once the restart notice is
drawn, so it never reaches the restart. It is timing-sensitive with or
without this patch. Run alone, alternating with a build of the same tree
without the patch, it passed 4 of 4 with it and 1 of 2 without; the failure
without the patch was at the keyboard wait. The same version, under ADR
0114's load (eight containers booting 250620 through `test_boot_tdeck`), got
103 runs, all passed. Four of them hit the canary, and each printed one reboot
banner. The canary's core dump still faulted with core 0 held.

## Consequences

**Good**

- One restart, one banner, from either core, as on silicon.
- During a restart from core 1, core 0 no longer runs its ROM and bootloader
  while core 1 is still using its memory. That is the likeliest reason core 1
  died in one run of three.
- During a panic, the other core is held, as on silicon. Before, it kept
  running its tasks while the handler printed and wrote its core dump.
- Boot counts count boots, and a doubled banner fails the T-Deck boot test
  instead of passing it.

**Costs, accepted**

- A tenth carried patch, and it changes reset semantics upstream chose. It
  has to be re-checked at every tag bump, like the others.
- A core's reset of itself is still a machine reset, so every sysbus device
  is reset with it. That is not silicon's CPU reset. It stands in for the
  peripheral resets the restart writes to `SYSTEM_PERIP_RST_EN0/1`, which are
  not modelled, and it is what resets the Python models (ADR 0035). A restart
  from core 1 now makes one machine reset instead of three. The main loop
  carries it out a fraction of a millisecond after core 1 asks, and core 0,
  released by then, runs in between. In the log it was early in its ROM
  (`0x40000454` to `0x40034c17`) when the devices were reset. Before this
  patch the same reset found core 0 in the bootloader.
- A core reset by the other core skips the reset handlers that only a
  machine reset runs. One of them is `esp-coproc-enable`, which sets
  `CPENABLE` (ADR 0013). The machine reset that ends the restart sets it
  again on both cores, before any app code runs.
- The hold ends at the next write to the stall controls. A firmware that
  reset the other core and never touched them again, nor reset itself, would
  leave that core held. ESP-IDF's restart and panic handler always stall,
  release, or reset next.
- With a thread per core, the hold is set from the resetting core's thread
  before the reset is queued, so the core it resets stops at its next
  translation block, a few instructions at most, much as on silicon. That was
  argued rather than traced; 80 restarts on a thread per core printed one
  banner each. The default is one thread.
- The canary's core dump still faults ("Re-entered core dump!", ADR 0114),
  with core 0 now held throughout the panic. So core 0 running was not the
  cause, and ADR 0114's cost of not judging the panic handler stands. The
  probe's own core dump on core 1 never faulted, before this patch or after.

## Alternatives rejected

**Keep forgiving the banner-only boot.** It covered only the two-banner case.
Three banners, a crash inside the restart, and core 0 running during every
panic would all still be there, behind a test that passes.

**Hold the stall and leave the resets alone.** Measured above: the stall
comes a time slice late, and the board hangs.

**Hold the reset core for a fixed time rather than until the next write.**
On one thread the guest clock follows the host's. The reset-to-stall gap was
0.4 to 1.5 ms of it in these logs, and it grows whenever the main loop holds
the lock. Any fixed time would be a guess about the host.

**Change the round robin.** That is accelerator code, outside ADR 0001's
boundary. A machine reset would still pause both cores to reset one.

**Make every CPU reset a reset of that core alone, a core's own included.**
Then nothing would reset the peripherals on a reboot, because their own reset
registers are not modelled. ADR 0035's reset of the board's models comes from
the machine reset too.

**Run with a thread per core.** The stall would still be empty, and ADR 0041
is why runs use one thread.
