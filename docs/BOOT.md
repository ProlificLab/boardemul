# The ESP32-S3 boot chain

What happens between reset and the first instruction of application code on the
T-Deck Max, what each stage demands of the hardware, and which of those demands
the Espressif QEMU fork actually satisfies.

**Every concrete number in this document was measured from the vendor images
pinned in `firmware/MANIFEST.toml`, not copied from a datasheet.** Where
something is derived but not yet observed, it is labelled as such. It was
written before the first run, to make M0's output interpretable. What the runs
corrected is marked where it fell, and the table of what QEMU provides says
which of the fork's gaps this project has since filled, and in which ADR.

---

## The chain at a glance

| Stage | Code lives in | Entry | Ends by |
|---|---|---|---|
| 0. ROM bootloader | masked ROM (unmodifiable) | reset vector | jumping to the second-stage entry |
| 1. Second-stage bootloader | flash `0x0`, copied to SRAM | `0x403C98D0` | jumping to the app entry |
| 2. Application | flash `0x10000`, partly SRAM / partly cache-mapped | `0x40377A8C` | `app_main()` |

All four vendor images share a byte-identical stage 0 to 1 path. They differ
only from the application onward.

---

## Stage 0 — the ROM bootloader

Masked into silicon, so it is the one component we can neither inspect nor
change. At reset it samples the strapping pins to choose a boot mode, and for
SPI flash boot it reads an image from flash, copies each segment to its load
address, and jumps.

**The offset is `0x0`, not `0x1000`.** ESP32 and ESP32-S2 place the
second-stage bootloader at `0x1000`; ESP32-S3, C3, C2 and H2 use `0x0`. This is
a per-chip constant, and it is exactly the kind of thing that must be verified
against the image rather than assumed from family knowledge — which is why
`tools/mkflash.py` will locate structures by parsing, not by convention.

Measured header of our second-stage bootloader at flash `0x0`:

| Field | Bytes | Value | Meaning |
|---|---|---|---|
| magic | `E9` | — | ESP image |
| segment_count | `03` | 3 | |
| spi_mode | `02` | DIO | |
| spi_speed / size | `4F` | 80 MHz, 16 MB | |
| entry_addr | `D0 98 3C 40` | `0x403C98D0` | IRAM |
| wp_pin | `EE` | `0xEE` | write-protect pin disabled |
| chip_id | `09 00` | 9 | **ESP32-S3** |
| min_chip_rev_full | `00 00` | 0 | see eFuses below |
| max_chip_rev_full | `FF FF` | 0xFFFF | unbounded |
| hash_appended | `01` | yes | SHA-256 follows the image |

Its three segments, all in internal SRAM:

```
seg0  load=0x3FCE3808  len=  1100 (0x00044C)   DRAM
seg1  load=0x403C9700  len=  3032 (0x000BD8)   IRAM
seg2  load=0x403CC700  len= 10880 (0x002A80)   IRAM
```

That the bootloader is entirely SRAM-resident is not incidental: it must run
before the flash cache MMU is configured, so it cannot execute from
cache-mapped flash. Only the application does that.

### Stage 0 output — predicted, then confirmed

Because the ROM simply narrates what it loads, its UART output is computable
from the header above *before running anything*. This was derived first and
observed afterwards; the `load:` and `entry` lines matched exactly:

```
ESP-ROM:esp32s3-20210327
Build:Mar 27 2021
rst:0x1 (POWERON),boot:0x4 (SPI_FLASH_BOOT)
SPIWP:0xee
mode:DIO, clock div:1
load:0x3fce3808,len:0x44c
load:0x403c9700,len:0xbd8
load:0x403cc700,len:0x2a80
entry 0x403c98d0
```

The one line that was *not* predicted correctly is the boot mode: `0x4
(SPI_FLASH_BOOT)` rather than the `0x8 (SPI_FAST_FLASH_BOOT)` expected from
Espressif's documented `strap_mode=0x04`. Harmless — it boots — but recorded
because it means the strapping value and the reported boot mode are not the
same encoding, and assuming they were would mislead later.

The three `load:` lines and the `entry` line are the measured segment table and
entry address, in order. `SPIWP:0xee` is the `wp_pin` field; `mode:DIO` is
`spi_mode`. The ROM build date is unknown until we run, since it depends on
which ROM revision the fork ships.

This is the single most useful oracle in M0. If the observed lines differ from
these, the fault is in the flash image or the SPI controller configuration —
**not** a missing peripheral. No device model can change what these lines say.

---

## Stage 1 — the second-stage bootloader

Now running from SRAM, this stage:

1. configures clocks and PLLs,
2. configures or disables the watchdogs,
3. locates and parses the partition table,
4. consults `otadata` to choose between `app0` and `app1`,
5. verifies the selected app image,
6. programs the cache MMU to map the app's flash-resident segments,
7. copies the app's SRAM-resident segments,
8. jumps to the app entry.

What it requires from hardware: the SPI1 flash controller, the flash cache and
MMU, UART0 for its log, eFuse block 0 (chip revision plus the secure-boot and
flash-encryption flags), the timer-group watchdogs, the clock and system
registers, and the SHA-256 accelerator if image hash verification is enabled.

**It does not require PSRAM.** PSRAM is brought up by `esp_psram_init()` during
`esp_system` startup, which happens *after* the jump to the app entry. This
matters for scoping: reaching app entry is a cheap milestone; PSRAM is the
hurdle immediately after it.

---

## The partition table

32-byte entries, little-endian, terminated by an MD5 checksum entry:

```
magic(2) = 0xAA50 | type(1) | subtype(1) | offset(4) | size(4) | label(16) | flags(4)
```

The MD5 entry uses magic `0xEBEB`. Unused space is `0xFF`.

**The table's own offset cannot be derived from the binaries.** It is a
compile-time constant baked into the second-stage bootloader — conventionally
`0x8000`, but nothing in any image header records it. `tools/mkflash.py` will
therefore scan `0x1000`-aligned offsets for the `0xAA50` magic and validate the
MD5 entry before trusting what it finds, rather than hardcoding `0x8000` and
being silently wrong on some future image.

Measured table (found at `0x8000`, MD5 entry present):

```
label      type  subtype   offset      size
nvs        data  nvs       0x9000      0x5000
otadata    data  otadata   0xE000      0x2000
app0       app   ota_0     0x10000     0x640000
app1       app   ota_1     0x650000    0x640000
spiffs     data  spiffs    0xC90000    0x360000
coredump   data  0x03      0xFF0000    0x10000
```

The entries tile a 16 MB flash exactly, with no gaps or overlaps — consistent
with the `4F` size nibble in both image headers.

---

## The application image

Same container format as the bootloader, different segment mix. Measured for
the factory demo at `0x10000`:

```
seg0  load=0x3C0D0020  len=2278684 (0x22C51C)  DROM   (flash, cache-mapped)
seg1  load=0x3FC973F0  len=  15060 (0x003AD4)  DRAM   (copied to SRAM)
seg2  load=0x42000020  len= 851668 (0x0CFED4)  IROM   (flash, cache-mapped)
seg3  load=0x3FC9AEC4  len=   9020 (0x00233C)  DRAM   (copied to SRAM)
seg4  load=0x40374000  len=  78824 (0x0133E8)  IRAM   (copied to SRAM)
seg5  load=0x600FE000  len=     44 (0x00002C)  RTC FAST
```

Every load address falls inside a valid ESP32-S3 region, and the image
terminates exactly at the file's end (`0x31565C` of payload, then 3 bytes of
padding, a checksum byte and a 32-byte SHA-256, reaching `0x325680` = the file
size). Both are strong evidence the image is well-formed.

The two cache-mapped segments are why the MMU matters: `0x3C0D0020` and
`0x42000020` are never copied anywhere. The bootloader programs the MMU so that
reads of those addresses are served from flash, and the app executes in place.

Application descriptor (`esp_app_desc_t`, at image offset `0x20`):

```
magic        0xABCD5432
project_name arduino-lib-builder
idf_ver      v4.4.6-dirty
built        16:40:08  Oct  4 2023
```

So this is Arduino-ESP32 (core 2.0.14) on ESP-IDF 4.4.6. Worth flagging: the
fork's ESP32-S3 machine is validated against IDF 5.x, and this firmware is a
generation behind. Not a blocker, but if the bootloader misbehaves in a way the
image cannot explain, IDF version skew is the first hypothesis to test.

**The `spiffs` partition is empty.** 3.5 MB is reserved at `0xC90000` and the
merged image contains no data for it. If the firmware loads fonts or UI assets
from spiffs, the resulting mount or open failure is an *image* problem, not a
missing peripheral, and must not be filed as one.

---

## eFuses

One-time-programmable configuration read during boot. The boot path cares about
four things: chip revision, whether secure boot is enabled, whether flash
encryption is enabled, and the MAC address.

QEMU takes a 1 KiB file as eFuse backing store; unspecified means all-zero,
which models a virgin chip — no secure boot, no flash encryption, chip
revision 0, MAC `00:00:00:00:00:00`.

An all-zero blob is normally a hazard here, because the bootloader refuses to
boot an app whose `min_chip_rev_full` exceeds the revision the eFuses report.
**Our images declare `min_chip_rev_full = 0`, so a revision-0 blob is
accepted.** That failure mode cannot fire for this firmware — worth recording,
because "app requires chip revision X" looks nothing like a hardware problem
and would otherwise cost a day of misdirected debugging.

The MAC address matters for a different reason: it is a boot-path input that
varies per physical device, so it must be pinned rather than left to chance.

---

## What QEMU provides, by stage

| Requirement | Stage | Modelled by the fork? | Since then, here |
|---|---|---|---|
| Xtensa LX7 core | all | yes | CPENABLE resets to silicon's 0xff (ADR 0143) |
| SPI1 flash controller | 0, 1 | yes | |
| Flash cache + MMU | 1, 2 | yes | |
| UART0 | 0, 1, 2 | yes | overflows as a serial line does (ADR 0122) |
| eFuse block 0 | 1 | yes | |
| Timer-group watchdogs | 1 | yes | |
| SHA-256 accelerator | 1 | yes | |
| RTC watchdog | 1 | **no** — absent from the fork | still absent |
| Octal PSRAM + MMU | 2 | yes (`-m 8M`, `is_octal=true`) | the line mode a firmware needs is per firmware (ADR 0007) |
| I2C controller | 2 | **no** | ours, a transaction at a time (ADRs 0011, 0121) |
| General-purpose SPI (SPI2/SPI3) | 2 | **no** | SPI2 is ours, several devices on it (ADRs 0011, 0032, 0120) |
| GPIO matrix / IOMUX | 2 | **no** | levels and interrupts are ours (ADR 0011); pull-ups are not (ADR 0026) |
| I2S | 2 | **no** | both directions, at the sample rate (ADRs 0016, 0119) |
| Wi-Fi / Bluetooth | 2 | **no** | WiFi MAC to a LAN (ADRs 0054–0097); Bluetooth core and link layer (ADRs 0025, 0045–0051) |
| USB | 2 | **no** | USB-Serial-JTAG (ADR 0100); the OTG controller is not |

The absent RTC watchdog is benign: a watchdog that does not exist cannot fire,
so its absence is safer than its presence. The absences from the app stage
onward were the substance of the M0 report, and the last column is what the
ADRs since have done about them.

---

## The UART trace as a boot oracle

Because both bootloader stages narrate themselves, the log alone separates
"our flash image is wrong" from "a peripheral is missing" — the two failures
that otherwise look identical.

**Correction from the first run: stage 1 does not narrate itself here.** This
firmware is built with the bootloader log level compiled down, so nothing
appears between the ROM's `entry` line and the application's first message. The
rows below that depend on a bootloader banner or a runtime partition dump can
never fire for these images, and the partition table cross-check has to come
from `tools/mkflash.py` parsing the image offline instead. Stage 0's oracle is
unaffected, and it turns out to be sufficient on its own.

| Observed | Verdict |
|---|---|
| no `ESP-ROM:esp32s3-...` banner | QEMU arguments or machine wrong; the image was never reached |
| ROM banner, then `invalid header` | image wrong at offset `0x0` |
| `load:` / `entry` lines differ from the predictions above | image wrong, or SPI mode/speed misconfigured |
| bootloader banner + partition dump matching the table above | image correct through the partition table |
| `invalid magic byte` / `checksum failed` on the app | image wrong at `0x10000` |
| `has invalid chip revision` | eFuse blob wrong — **cannot fire for these images** |
| spiffs mount or file-open failure | empty spiffs partition; an image gap, not hardware |
| **`entry 0x40377A8C` printed** | **the boundary.** Everything before is the image domain; everything after is the emulation domain |

That last row is the one that matters. Before it, suspect ourselves. After it,
suspect the hardware model.

---

## Known divergences from real silicon

Places where a correct emulation still behaves differently from the board on
the desk. Recorded so they are not rediscovered later as bugs.

- **Uninitialized RAM is zeroed.** QEMU zeroes memory; real silicon holds
  garbage. Firmware that accidentally depends on uninitialized values will
  behave *better* here than in reality. This cannot be pinned.
- **No RTC watchdog.** Code relying on an RTC watchdog reset to recover will
  hang instead of resetting.
- **No GPIO matrix or IOMUX in the fork.** Our GPIO block models pin levels,
  directions and interrupts (ADR 0011), but not the pull-ups that live in
  IO_MUX, so each board states its pins' idle levels itself (ADR 0026).
- **Secure Boot is unsupported** on the fork's S3 machine. Irrelevant here —
  our eFuses do not enable it — but it forecloses testing secure-boot images.
- **Timing.** Bus transfers take the time their wire would, on the guest's
  clock (ADRs 0118–0121). By default that clock follows the host's, so a
  loaded host stretches a run. With `--icount` it counts instructions instead,
  and two runs of the same inputs are the same run (ADR 0115). Either way a
  claim about time is a claim about the model: the instruction rate is QEMU's,
  not the LX7's cycle count.
