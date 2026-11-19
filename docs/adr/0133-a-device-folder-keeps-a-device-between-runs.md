# ADR 0133 — A device folder keeps a device between runs

**Status:** Accepted — 2026-10-09. Amended 2026-11-19: how the card is
written back (decision 4).

## Context

Every run today is assembled from command-line flags and thrown away when it
ends:

- Flash is assembled into `build/<firmware>/` per firmware and shared by every
  run of it — there is no "this device", only "this image".
- The SD card is generated fresh in memory on every start
  (`emu/sdimage.py`); anything written to it is lost.
- Settings a guest saves survive only by hand: `tools/make_configured_fs.py`
  boots a firmware, drives Meshtastic's setup dialog, carves the resulting
  filesystem out of the flash and hands it back as an `--overlay`. That is a
  workflow for building images, not a device that remembers.

VirtualBox's ordinary case — create a machine, give it storage, start it,
stop it, start it again and find things as you left them — has no
equivalent here.

## Decision

1. **A device is a folder** with nothing else in it:

       devices/<name>/
           device.json     name, board, firmware path and its SHA-256, MAC,
                           PSRAM size and line mode, created date
           flash.bin       the chip's flash, assembled once
           efuse.bin       the eFuses, with the MAC
           sdcard.img      the card, when the board has one

   `emu/device.py` owns the format so tools, tests and the emulator all read
   it one way.

2. **`tools/device.py` manages folders**: `create` assembles the storage once
   (through `mkflash`, which stays the only flash builder), `list`, `show`,
   `flash` re-assembles into an existing device, `remove`. Starting a device
   is `emu/run.py --device PATH` — the CLI is for folders, the emulator is
   for runs.

3. **`--device` replaces the firmware argument, not the run.** Storage comes
   from the folder and nothing is rebuilt; everything that is wiring rather
   than identity — `--bus`, `--air`, `--icount`, timeouts, output
   directories — stays a per-run flag as it is today.

4. **Cold persistence is the file system's job, in both directions.**
   QEMU's mtd drive already takes `flash.bin` by name; guest writes to it
   (NVS, preferences, a filesystem) land in the file, so a stop-and-start
   finds them. The card is the emulator's: its image is loaded from
   `sdcard.img` at start and written back when the run ends, if the run
   changed it. *(Amended 2026-11-19.)* It is written beside the card and
   renamed over it, so a run stopped while it writes leaves the old card
   whole: a stop the container's grace period cut short had left 8 MB of a
   32 MB card, and the device could not start again. A run that failed to
   load the card never writes the board's empty one over it.
   That this write-through actually happens is the slow test's to measure,
   not this ADR's to assume.

5. **Deliberately not here.** Live snapshots need state drawn out of two
   processes — the C models in QEMU and the Python board — and are their own
   decision. A picker in the window is interface orchestration over
   `tools/device.py` and follows once the folders themselves are settled.

## Consequences

**Good**

- "Configure it once" becomes ordinary: create a device from Meshtastic's
  image, boot, fill the dialog, stop, boot again into a node that already has
  its identity. The `make_configured_fs.py` overlay dance remains for
  building images, and stops being the only way to keep a setting.
- A test can say "this device, again" — the same MAC, the same storage, the
  same card — which is what persistence and regression tests both want.

**Costs, accepted**

- Two copies of the truth exist for a while: `build/` keeps its per-firmware
  images for flag-driven runs, and devices keep their own. Neither reads the
  other.
- A device folder binds its firmware by path at creation; moving or
  upgrading the file is `device.py flash`, which re-records the hash — there
  is no silent rebinding.
