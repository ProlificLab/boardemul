# ADR 0022 — Convert SPI1 dummy cycles with the read mode's line count

**Status:** Accepted — 2026-09-02. Closes the defect ADR 0021 left open.

## Context

Meshtastic could not use its filesystem: `esp_littlefs` reported `Corrupted
dir pair`, `mount failed (-84)`, then `Failed to format filesystem`, and it did
so with the vendor's own filesystem image in place (ADR 0021). The image was
correct -- a valid LittleFS of 864 blocks of 4096 bytes, the partition exactly.

The fork's flash device (`m25p80`, an ISSI `is25lp128`) has trace points for
every command, address byte and returned byte, and the fork's SPI1 controller
clocks every transaction through it, so the whole read path was observable.
What it showed, in order:

- The cache model was not involved: no MMU-table write ever mapped a
  partition page, and code runs from the cache's own copy of the flash, filled
  by block reads and never through SPI1.
- Every partition read reached the device through SPI1 at the right address,
  and the device returned the right bytes -- 620 compared against the image,
  none wrong.
- The guest still saw garbage. The tell was one line of the firmware's own
  log: `Incorrect size of core dump image: -65536`. The firmware had read four
  bytes at the blank coredump partition, and `-65536` is `00 00 FF FF`: two
  bytes of zero, then two of the erased flash. The device trace for that
  transaction showed exactly two data bytes clocked. The 32-byte NVS page
  headers arrived as 30. Every read was shifted right by two bytes.

The cause is one conversion. A quad I/O read (`0xEB`) carries its address and
dummy phases on four data lines; the ESP-IDF driver programs 6 dummy cycles,
which on four lines are 24 bits. The emulated flash counts that phase in
bytes, and expects 3. The fork's `esp32s3_spi_get_dummy` converted cycles to
bytes as if every cycle carried one bit -- `(cycles + 7) / 8` -- and sent 1.
The flash then consumed the first two data clocks as the rest of its dummy
phase, and everything the guest read back landed two positions late.

The bootloader and the code path were never affected: the ROM reads with the
single-line fast read (`0x0B`, 8 cycles, correctly 1 byte) and the app runs
from the cache. Only the runtime flash driver, which uses the quad I/O read,
saw it -- which is why NVS, the coredump check and every filesystem broke
while the firmware itself ran. Upstream's `esp-develop` carries the same code
(checked 2026-09-02: two commits on the file, neither touches this).

## Decision

1. **The dummy phase is converted with the line count the read mode selects**:
   `FREAD_QIO` means four lines, `FREAD_DIO` two, anything else one, and the
   register field holds the cycle count minus one, as the TRM says. Bits are
   cycles times lines, rounded up to bytes. Checked against what the flash
   model expects from an ISSI part: 3 bytes after a quad I/O read, 1 after a
   dual I/O read, 1 after a fast read; and against the ROM's other encoding of
   the same transaction (a 32-bit address phase carrying the mode bits, then 4
   dummy cycles), which comes to the same 6 bytes.

2. **Carried as `qemu-patch/spi1-dummy-lanes.patch`** (reported upstream as
   <https://github.com/espressif/qemu/issues/169>), applied by the build the
   way ADR 0017's patch is, in the same loop, failing loudly if the code moves.
   It was meant to be the second and last change to the fork's own logic;
   ADR 0023 carries a third and says why. All are reported upstream and each
   goes when upstream lands it.

3. **ADR 0021's open defect is closed.** A blank partition now mounts-fails,
   formats, and serves files on the same boot, as a real first boot does. The
   overlay stays: it is what the vendor's installer writes, and it carries
   the files the firmware expects to find.

4. **A second fork defect, seen and not hit, is recorded here** so it is not
   lost (reported upstream as <https://github.com/espressif/qemu/issues/170>): the cache model fills a physical flash page's copy with `0xdeadbeef`
   when one MMU entry unmaps it, without checking whether another entry still
   maps the same page. At reset, entry 0 unmaps page 0 while entry 63 still
   maps it. Nothing read through entry 63 afterwards in the runs seen; the
   first firmware that does will read garbage, and this is where to look.

## Consequences

**Good**

- Meshtastic mounts its filesystem, with the vendor image and without, and
  reads its preferences. The false coredump size is gone.
- The instrument that found it costs nothing to keep: `BOARDEMUL_QEMU_DFLAGS`
  takes QEMU trace names, and `trace:m25p80_*` shows every byte the flash
  sees and returns. Comparing that stream to the image is what separated
  "the device returns wrong bytes" from "the guest receives them wrong".

**Costs, accepted**

- One more carried patch, with the same obligation as the first: re-check it
  on every tag bump, delete it when upstream fixes it.

**Open, as of this record**

- About 40 seconds in, right after the first attempt to *write* a preference
  file, the firmware trips an assertion raised while the flash cache is
  disabled and reboots. An assert in a cache-disabled section is inside the
  flash driver's write or erase path. Next thing to find, by the same method.

## Alternatives rejected

**Model SPI1 ourselves.** The fork's controller is a working device with one
wrong line; replacing it to fix that line is the opposite of ADR 0001.

**Send the byte count the flash wants per command.** Three for `0xEB` would
have worked here and been wrong for a Winbond part, which expects a different
count, and for the ROM's encoding of the same read. The line count is what
the silicon does, and it is right for every case checked.

**Blame the cache model first.** It had a plausible hazard (the `0xdeadbeef`
clear) and the earlier note in ADR 0021 pointed at "a stale copy somewhere".
The trace showed no partition page ever mapped, in one run, before any code
was written against that theory. The hazard is real and is recorded above;
it just was not this.
