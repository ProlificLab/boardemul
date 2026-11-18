# ADR 0143 — The FPU is on out of reset, as on silicon, and our device goes

**Status:** Accepted — 2026-11-18. Supersedes ADR 0013. Follows up a
difference ADR 0138 recorded in the QEMU tag it adopted.

## Context

ADR 0013 added a QEMU device of ours, `esp-coproc-enable`, and listed it on
the T-Deck board as `esp-coproc-enable,mask=0x1`. After every machine reset it
wrote CPENABLE = 0x1 on both cores, so that the vendor firmware's float in a
C++ global constructor, run before FreeRTOS, did not panic with "Coprocessor
exception". Nothing in that firmware's ROM, bootloader or app writes
CPENABLE, so the constructor meets the register's reset value. ADR 0013 could
not settle what silicon resets it to. It wrote its decision on the reading
that silicon leaves the FPU on, without proof, and named it the first thing
to check on hardware.

Upstream has checked it on hardware:

- **espressif/qemu#154**, reported on `esp-develop-9.2.2-20260417`: on
  ESP32-S3 silicon, an `rsr.cpenable` as the first instruction of `main`,
  before any software writes the register, reads 0xff. QEMU read 0x00 there.
- **82ff7118** (2026-05-28, PR #155) fixed it in the SoC's reset: after
  `cpu_reset()`, each core's CPENABLE is set to 0xff.
- **39341cc5**, ADR 0123's reset reworked upstream, moved that line into
  `esp32s3_reset_one_core`, so a core reset by the other core gets it too.
  Its comment: measured on both CPUs after a CPU reset, and on the PRO CPU
  after a system reset.

Both are in `esp-develop-9.2.2-20261109`, the tag ADR 0138 adopted. ADR 0013's
reading was the right one: silicon leaves every coprocessor enabled.

On that tag our device now overrides upstream's value with a smaller one.
The SoC's reset handler runs first and sets 0xff. Ours, registered after it
(ADR 0013, decision 5), then writes 0x1. A core reset by the other core does
not run machine reset handlers, so it keeps upstream's 0xff. Silicon has one
value; we had two.

A probe measured it (`tools/cpenable_probe`, built with the vendor builds'
ESP-IDF 4.4, 2026-11-18). It reads CPENABLE in a C++ global constructor on
the PRO CPU, where the vendor firmware's float runs, with upstream's method:
an `rsr.cpenable` before anything writes the register. It does so on three
boots: power-on, after `esp_restart()` (`rst:0xc`) and after a digital
system reset (`rst:0x3`).

- **With the device:** `0x00000001` on all three boots.
- **Without it:** `0x000000ff` on all three, silicon's value.

The two values enable the same instructions here. In this QEMU only the
FPU's 152 opcodes check CPENABLE, all on bit 0. The 258 ESP32-S3 TIE opcodes
check no bit. So the device no longer makes any firmware run that would not
run without it. Its only effect left is to make CPENABLE read 0x1, a value
silicon does not have, after a machine reset and not after a core reset.

## Decision

1. **The T-Deck board no longer lists `esp-coproc-enable`.** QEMU's own
   reset gives every board silicon's CPENABLE, on every reset path. The
   boards' `SOC_DEVICES` list stays, empty on both. It is where a declared
   divergence from the SoC would go, and `tests/test_soc_quirks.py` now
   asserts that neither board declares one.

2. **The device is deleted, not kept off.** `qemu-patch/esp_coproc_enable.c`
   goes, and `tools/build_qemu.sh` stops installing it. The script also
   removes it from a tree that an older script installed it in: both the file
   and its line in `hw/xtensa/meson.build`. The tree then shows only what this
   script installs, which is what ADR 0005 asks of it. Our sources are again
   all in `hw/misc`, and none of them includes the target's `cpu.h`.

3. **ADR 0013 is superseded.** Its account of the panic stands. Its open
   question is answered by upstream's measurement, and its device, the
   decision, is gone.

4. **The probe guards it**, as `tools/restart_probe` does for ADR 0123:
   `tests/test_cpenable_probe.py` asserts 0xff at the constructors on all
   three boots. `test_boot_tdeck`'s FPU test still guards the vendor
   firmware itself.

## Consequences

**Good**

- CPENABLE has silicon's value after every reset, and the same value
  whichever path the reset took.
- One fewer device of ours inside QEMU, and the only one that wrote CPU
  state. ADR 0013's cost, a second `meson.build` of theirs carrying a line of
  ours, is gone.
- The T-Deck's launch no longer prints a `soc :` line. The emulator runs it
  on the same SoC as the Max.

**Costs, accepted**

- The FPU being on out of reset is now upstream's code, not ours. A tag that
  lost it would bring back ADR 0013's panic. That would be loud:
  `test_boot_tdeck` fails on "Coprocessor exception", and the probe reads
  0x00000000.
- The probe reads the PRO CPU only, because ESP-IDF runs the constructors
  there. The APP CPU gets its value from the same upstream function, and no
  firmware we run reads it before the scheduler writes it.
