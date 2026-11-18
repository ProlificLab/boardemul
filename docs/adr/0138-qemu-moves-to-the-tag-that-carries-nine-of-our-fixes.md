# ADR 0138 — QEMU moves to the tag that carries nine of our fixes

**Status:** Accepted — 2026-11-18. Completes the pin committed on 2026-11-10
(de76647), which cited this ADR before it existed and could not build.
Ends the carried-patch part of ADRs 0017, 0022, 0023, 0066, 0083, 0101,
0105, 0122 and 0123.

## Context

`tools/build_qemu.sh` built Espressif's fork at tag
`esp-develop-9.2.2-20260417` (40edcca) plus eleven carried patches, each
reported upstream. On 2026-11-10 the script was changed to pin commit
7411393a on `esp-develop` "rather than by tag", saying Espressif had stopped
cutting dated tags. Measured on 2026-11-18:

- **The tag exists.** `esp-develop-9.2.2-20261109`, published as a release on
  2026-11-09, names commit 7411393a: the pinned commit. It is 105 commits
  past the old tag and none behind.
- **The pin was never built.** `third_party/qemu` is still at 40edcca with
  the eleven patches, and its binary dates from 2026-10-02. Run now, the
  script stops: the tree is not at the pin, and on a fresh clone the second
  patch neither applies nor reverse-applies. Its error message names
  `${QEMU_TAG}`, which no longer exists, so under `set -u` it dies on an
  unbound variable instead of saying which patch.
- **Espressif took nine of the eleven on 2026-11-06**, as its own commits:
  - Unchanged, so our patch reverse-applies: `gdma-out-eof-desc` (0017),
    `tlb-flush-owning-thread` (0066), `uart2-chardev` (0105).
  - Reworked, on by default for the ESP32-S3, fixing the same defect:
    - `spi1-dummy-lanes` (0022): the same byte counts in every case ADR 0022
      checked;
    - `system-bt-lpck-regs` (0023): the same registers, with a FRAC reset of
      0x02001001 where ours had 0x02001000; the SDK's `system_reg.h` agrees
      with upstream;
    - `gdma-reaches-psram` (0083): ADR 0083's failing address included;
    - `intmatrix-level-mirror` (0101): the same design;
    - `core-reset-and-stall` (0123): our offered fix, reworked.
  - Reworked behind a property, off by default: `uart-rx-overflows` (0122),
    as `rx-overflow` on the UART.
- **Two remain ours**, applying cleanly to the new tag:
  `icount-sleep-off-deterministic` (0115, QEMU-core,
  gitlab qemu-project/qemu#4632) and `flash-writes-synchronously` (0115,
  espressif/qemu#188, still open).
- **What upstream does differently**, read from its diffs:
  - The UART, with `rx-overflow` on, overwrites the oldest byte of a full
    FIFO where ours dropped the newest, and RXFIFO_RST now empties the FIFO.
  - A core held in reset by the other is released after 20 ms of virtual
    time, where ours held it until the next stall report. ADR 0123 rejected
    a fixed time; 20 ms is ten times the gaps it measured.
  - A peripheral reset of the interrupt matrix now clears its input levels,
    where ours kept them.
  - A core reset sets CPENABLE to 0xff, which overlaps ADR 0013's
    `esp-coproc-enable`.
  - Commits we never carried change the timer groups, the systimer, the GDMA,
    PSRAM address wrap and the SPI_MEM chip-select defaults.

Going back to the old tag would build again, but would keep carrying nine
patches upstream has taken, and it would miss every fix since April.

## Decision

1. **The pin is the tag and the commit together.** The script fetches
   `esp-develop-9.2.2-20261109` and refuses to build unless it names
   7411393a. A tag says which release; the commit makes a moved tag an
   error, not a different build. An existing tree is checked by its commit.

2. **The nine patches are deleted**, not kept beside the tree. Each ADR
   whose decision was "carry this patch" records that the fix is upstream
   and the patch gone. The ADR's account of the defect stands.

3. **The two remaining patches stay**, with their reports.

4. **The UART's overflow is turned on**: `emu/run.py` passes
   `-global driver=esp32s3_soc.uart,property=rx-overflow,value=on`. ADR
   0122's decision, a serial line keeps no backlog, still stands; upstream's
   way of losing a byte replaces ours.

5. **The new build is proven before it replaces the old one.** It is built
   in its own Docker volume, mounted over `third_party/`, and the fast tier
   and the slow tests nearest the changed code run against it: the restart
   probe (0123), the T-Deck and Max boots, GPS (UART), WiFi (GDMA, timers)
   and the web boot. Only then is the shared tree rebuilt at the new tag.

6. **The behaviours that differ are followed up, not assumed away.** The
   restart probe measures the 20 ms hold. The matrix's cleared levels, which
   can swallow the next interrupt from our three devices that only forward
   changes (`esp_gpio_block`, `esp_i2c_master`, `esp_ble_core`), and
   `esp-coproc-enable`'s overlap with CPENABLE are each a change of their own.

## Consequences

**Good**

- Nine of eleven carried patches are gone, because the fixes are now
  Espressif's. The two left are the two Espressif cannot take alone: one is
  QEMU's own core, one is still open.
- The build script builds again, and says the truth about its pin.

**Costs, accepted**

- Everything Espressif changed since April arrives at once. The suite is
  the measure; what it does not cover is not proven.
- The UART now loses the oldest byte where it lost the newest. Either
  loses a byte; upstream's choice is the one this project can stop owning.
