# ADR 0149 — A device keeps snapshots of its storage, and its card is yours

**Status:** Accepted — 2026-11-18. Adds to ADR 0133, whose decision 5 left
live snapshots out.

## Context

A device folder keeps a device's identity and storage between runs (ADR
0133): `flash.bin`, `efuse.bin`, `sdcard.img` and the manifest. What a
virtual machine manager does with a machine's disks is not yet possible
with them:

- **Going back.** A device that was set up carefully, a Meshtastic node
  with its channels and its name, is one wrong tap from being set up again.
  A virtual machine is snapshotted before an experiment and restored after.
- **The card.** The board builds an empty FAT16 card of 32 MB once, and the
  run writes it back when it ends (ADR 0133). There is no way to put a file
  on it before a run, to take it out after one, or to give the device a card
  made somewhere else.

ADR 0133 set live snapshots aside, because they need state drawn out of two
processes, the C models in QEMU and the Python board. A powered-off
snapshot needs none of that. When no run holds the folder, its storage is
all the device is: what is in RAM is gone at the next boot either way.

## Decision

1. **A snapshot is a copy of a stopped device's storage**, kept in the
   folder: `snapshots/<label>/` holds `flash.bin`, `efuse.bin`,
   `sdcard.img` when there is one, and `device.json` as it was. Restoring
   copies them back, the manifest included, so the settings come back with
   the storage they were made with. A label is `[A-Za-z0-9._-]`, 1 to 40
   characters; without one, it is the time it was taken.

2. **Only a stopped device is snapshotted or restored.** A run writes its
   card back when it ends and QEMU writes the flash as the guest does, so
   while it runs the files are not a state the device was ever in. The page
   and the CLI refuse, and say why.
   - *Amended 2026-11-20:* a run that was killed never removes its
     `running` marker, and the folder then refused for good. The marker
     names the machine as well as the process; one written on this machine
     by a process that no longer exists no longer holds, and is removed. One
     from another machine or container still holds, since nothing here can
     check it.

3. **The card is the user's, whole or by files:**
   - **out**: a copy of `sdcard.img`, a FAT16 image any computer mounts;
   - **in, as an image**: any image of the card's size replaces it, the size
     the board's card has (ADR 0133 refuses another size at start);
   - **in, as files**: a fresh card of the board's size with the given
     files in its root directory, under 8.3 names, as `emu/sdimage.py` lays
     one out (ADR 0033).
   Replacing the card takes a snapshot first, named `before-card-<time>`,
   so a card replaced by mistake comes back with one restore.

4. **Both doors have them.** `tools/device.py snapshot|snapshots|restore|forget`
   and `device.py card NAME --export|--image|--files`. The page lists a
   device's snapshots, takes, restores and forgets them, downloads the
   card, and replaces it from an image.

## Consequences

**Good**

- A device can be put back exactly as it was, card and settings included,
  which is what makes it safe to try things on it.
- What a firmware writes to its card can be read on the host, and a card
  can be prepared on the host for a firmware to read.

**Costs, accepted**

- A snapshot is the device's whole storage again: about 50 MB for a board
  with a card. They are kept until forgotten.
- Snapshots are of a stopped device only. A device's running state, its
  RAM and its peripherals' registers, is not kept, and a restored device
  boots.
- Files put on a card land in its root directory under 8.3 names; a name
  that does not fit is refused rather than shortened.
