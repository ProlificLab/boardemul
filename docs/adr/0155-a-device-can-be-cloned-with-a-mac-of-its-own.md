# ADR 0155 — A device can be cloned, with a MAC of its own

**Status:** Accepted — 2026-11-18. Adds to ADRs 0133 and 0149. Amended
2026-11-19: decision 5, a new device too.

## Context

A mesh is several nodes, each set up the same way: a region, a channel, a
name. On a device folder (ADR 0133) that set-up is done once through the
firmware's own dialog, and the flash keeps it. A second node means doing
it again on a second device, by hand, through the same dialog.

A virtual machine manager clones a machine for this. Copying a device folder
by hand gets the set-up, and also the identity, which is wrong twice over:

- the MAC lives in the eFuses (`tools/mkflash.py`, `build_efuse`), so two
  copies are two boards with one MAC;
- Meshtastic draws its node number from the MAC (ADR 0098), so two nodes
  with one MAC each discard the other's packets as their own.

## Decision

1. **`clone` copies a stopped device into a new folder**: the flash, the
   card, the settings and the overlays it was made with, as they are now.
   Its snapshots are not copied; they are the original's history.

2. **The clone gets a MAC of its own.** The eFuses are written again by
   `mkflash`'s `build_efuse` with a new MAC, the first free one counting up
   from the original's, among the devices in the same folder, unless one is
   given. Everything else in the eFuses is what `build_efuse` writes for
   every device.

3. **Its manifest says where it came from**, `cloned_from`, beside the
   firmware it records.

4. **Both doors have it**: `tools/device.py clone NAME NEW [--mac MAC]`, and
   a Clone button in the page, which asks for the new name.

5. **A new device gets a MAC of its own too.** Making two Meshtastic devices
   from the firmware library (ADR 0160) gave both `02:54:44:4D:00:01`, the
   one default every new device had, and two nodes on one air with one
   node number. A new device now takes that default if no device in the
   folder has it, and otherwise the first free one counting up from it. A
   MAC given by hand that another device already has is refused, as for a
   clone.

## Consequences

**Good**

- A configured node becomes a mesh: clone it, give each clone a place on
  the same air (ADR 0145), and they are distinct nodes from their first
  boot.

**Costs, accepted**

- Whatever the firmware derived from the MAC and stored in flash at its
  first boot is the original's until it derives it again. Meshtastic
  derives its node number from the MAC at each boot.
- A clone is as large as its original, about 50 MB with a card.
