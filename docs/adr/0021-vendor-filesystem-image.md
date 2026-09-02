# ADR 0021 — Assemble the flash with the vendor's filesystem image where the vendor's installer puts it

**Status:** Accepted — 2026-09-02

## Context

Meshtastic on the T-Deck boots, draws, answers input for about a minute, and
then gives up on storage: `esp_littlefs` cannot mount its partition, tries to
format it, reports `Failed to format filesystem`, logs `File system is not
mounted` on every file operation, and eventually reboots. Everything after that
is a firmware coping with a device it believes has no storage.

Two facts settle where this belongs.

**The vendor ships the filesystem separately.** Meshtastic's release for this
build carries `littlefs-t-deck-tft-2.7.10.94d4bdf.bin` -- exactly `0x360000`
bytes, the partition's size -- and its own `device-install.sh` writes it at
`0xc90000` for a 16 MB part, which is precisely the `spiffs` entry in this
image's partition table. A real T-Deck running this firmware has that image in
that place. Our flash image, built from the app binary alone, did not. The app
is unmodified either way; what changed is that the flash now looks like what
the installer produces, which is the whole of what ADR 0003 is for.

**The format failure is an emulator defect, recorded and not yet explained.**
After a run, block 0 of the partition holds a correct LittleFS superblock --
the `littlefs` magic, 4096-byte blocks, 864 of them, the partition exactly --
persisted to the backing file. The guest wrote it and then could not read it
back as valid. The fork's SPI1 controller serves reads by transferring to the
flash device, which should be current, so the stale copy is somewhere else.
A fresh device formats on first boot; this one cannot, and that remains open.

## Decision

1. **`mkflash` takes overlays**: `--overlay OFFSET=FILE`, spliced into the
   padded image after assembly, each checked against the parsed partition table
   -- it must fall inside a data partition and fit it. The check uses the same
   parser the rest of the tool trusts (ADR 0003), so an overlay in the wrong
   place fails loudly before a run rather than silently corrupting one.

2. **`run.py` passes them through** (`--overlay`, repeatable), and the run
   manifest records them alongside the firmware hash so a run stays
   reproducible.

3. **The image is pinned in `firmware/MANIFEST.toml`** with the release zip it
   came from, the member name inside it, and its hash. It is a vendor artifact,
   fetched and verified like the firmware itself.

4. **The format defect stays open** in this record until it is understood. The
   overlay makes Meshtastic runnable; it does not make first-boot formatting
   work, and a firmware without a shipped image would still hit it.

## Consequences

**Good**

- Meshtastic gets the storage a real device has, with nothing in the firmware
  changed.
- The mechanism is general: any data partition a vendor populates separately --
  an SD card image on a board that has one, a SPIFFS web-UI bundle -- is an
  overlay at an offset the partition table can check.

**Costs, accepted**

- Another vendor download, pinned like the others.
- A real first boot on a blank part still fails under this emulator, and the
  overlay hides that from anyone who does not read this ADR.

## Alternatives rejected

**Fix the format path first.** The right fix eventually, and the evidence is
gathered here for it; but the vendor's own installer does not rely on
formatting either, so the overlay is the faithful thing to do regardless of
whether formatting works.

**Format the image ourselves.** A LittleFS formatter in standard-library Python
is a project of its own, and it would produce an empty filesystem where the
vendor's carries the files the firmware expects to find.
