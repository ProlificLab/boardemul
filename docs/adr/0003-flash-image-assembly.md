# ADR 0003 — Assemble the flash image and eFuse blob by parsing, not by convention

**Status:** Accepted — 2026-08-21

## Context

The vendor images turned out to be complete merged flash images already
(bootloader at `0x0`, partition table at `0x8000`, app at `0x10000`), so the
assembly job originally imagined for M0 mostly evaporated. What remains is
smaller but not trivial:

- QEMU accepts flash sizes of 2, 4, 8 or 16 MB only. The factory image is
  `0x325680` bytes — not one of those — so it cannot be handed to QEMU as-is.
- QEMU needs an eFuse backing file, and an all-zero one gives a MAC address of
  `00:00:00:00:00:00`.
- Above all, M0's entire value depends on being able to say "the image is
  correct" with confidence. If we cannot, every later failure is ambiguous.

That last point is the real work. `docs/BOOT.md` establishes that the ROM's
`load:` and `entry` lines are computable from the image before running
anything. That oracle is only worth having if the thing we computed it from was
parsed rather than assumed.

## Decision

1. **`tools/mkflash.py` locates every structure by parsing, never by
   convention.** The partition table is found by scanning `0x1000`-aligned
   offsets for the `0xAA50` magic and validating the trailing MD5 entry — not
   by assuming `0x8000`. The app is found through the partition table, not by
   assuming `0x10000`. Flash size comes from the image header's size nibble,
   not from the board specification. Every one of these happens to match
   convention for the current images; the point is that we would notice if a
   future image did not.

2. **Validation is exhaustive and fails loudly.** Image magic, chip ID, segment
   load addresses against the ESP32-S3 region map, the XOR checksum byte, the
   appended SHA-256, partition table MD5, and partition overlap. A tool that
   silently accepts a broken image is worse than no tool, because it converts
   an image bug into a phantom hardware bug.

3. **Inputs are never modified.** Outputs go to `build/`. The vendor binaries
   stay exactly as downloaded, and their hashes stay verifiable against
   `firmware/MANIFEST.toml`.

4. **The eFuse blob carries a synthesized MAC.** 1024 bytes, otherwise zero.
   The layout is not guessed: the fork reads the file straight into a flat
   `ESPEfuseBlocks` struct (`hw/nvram/esp_efuse.c`), so the file is a raw
   little-endian dump of block words — BLOCK0 is 6 words, BLOCK1 is 6 words at
   byte offset `0x18`, and blocks 2 upward are 8 words each.

5. **The default MAC is locally administered: `02:54:44:4D:00:01`.** The low
   bit of the second nibble marks it locally administered, which is the honest
   encoding for a device that does not exist. `54 44 4D` is ASCII `TDM`, and
   the last byte is a unit number, so a future multi-device milestone has an
   obvious allocation scheme. A real Espressif OUI was rejected: it would be
   indistinguishable from hardware on a real network, and M2 puts these on
   networks.

6. **QEMU does not interpret the MAC at all** — it serves BLOCK1 words and
   ESP-IDF does the decoding. ESP-IDF's `esp_efuse_table.csv` for esp32s3
   defines the MAC as `EFUSE_BLK1` bits 40, 32, 24, 16, 8, 0 in that order, so
   the six bytes are stored *reversed* relative to the natural word order.
   `mkflash.py` encodes it that way and says so in a comment, because a
   byte-reversed MAC is exactly the kind of thing that gets "fixed" by someone
   who has not read the table.

## Consequences

**Good**

- A validated image means a boot failure after the `entry` line is a hardware
  question, and one before it is our question. That separation is the whole
  point of M0.
- The MAC is pinned, so it is one fewer source of run-to-run variation.
- Parsing rather than assuming means the tool keeps working on OTA images,
  custom partition layouts, or a differently built vendor firmware.

**Costs, accepted**

- More code than a `cat` and a `truncate`. Justified entirely by decision 2.
- **The MAC byte order is derived, not observed.** It follows from the IDF
  eFuse table, but no run has yet printed a MAC to confirm it. It is recorded
  as an open verification item rather than as a fact, and the first firmware
  that prints its MAC settles it.
- Padding to 16 MB makes a 3.3 MB input into a 16 MB output. Irrelevant on
  disk, worth knowing before someone wonders where the space went.

## Alternatives rejected

**Pass the vendor `.bin` to QEMU directly.** Would be ideal, and does not work:
`0x325680` is not one of the accepted flash sizes.

**Hardcode `0x8000` for the partition table.** Correct for all four current
images and wrong in principle. The offset is a compile-time constant inside the
bootloader that no image header records, so an image that moved it would be
misparsed silently — producing exactly the "is it the image or the hardware?"
confusion this milestone exists to eliminate.

**All-zero eFuses.** The fork's default and the best-understood configuration.
Rejected because a `00:00:00:00:00:00` MAC is not inert: Arduino-ESP32 derives
BT and Wi-Fi addresses and often a default device name from it, so a degenerate
MAC risks downstream behaviour that looks like a bug and is not.

**Derive the MAC from a hash of the image.** Deterministic and self-describing,
and rejected as cleverness: it makes the MAC change whenever the firmware
changes, which is the opposite of what pinning is for.
