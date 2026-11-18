# ADR 0150 — A firmware brings its filesystem, as its installer would

**Status:** Accepted — 2026-11-18. Adds to ADR 0021 and ADR 0147's
manifest.

## Context

Meshtastic's T-Deck build is two images. The flash image holds the
bootloader, the partition table and the app. A LittleFS image holds the
interface's files, and the vendor's own `device-install.sh` writes it at
0xc90000 (ADR 0021). The manifest pins both, separately.

A run gets the filesystem only when told: `run.py --overlay
0xc90000=firmware/littlefs-….bin`, or `device.py create --overlay …`. The
web page's create form has no overlay at all. So a Meshtastic device made
from the page has an empty filesystem partition, and measured on 2026-11-18
it never gets anywhere:

    E (6901) esp_littlefs: … Corrupted dir pair at {0x0, 0x1}
    E (6932) esp_littlefs: mount failed,  (-84)
    [ 17537][E][vfs_api.cpp:105] open(): /littlefs/static/static does not exist …

and its screen is still black after 240 s. With the filesystem in place the
same build reaches its setup dialog (ADR 0026). A person flashing a real
T-Deck with the vendor's installer never meets the empty partition, because
the installer writes both images.

## Decision

1. **The manifest says which images go together.** An image may name
   `companions`: other entries of the manifest, each with the `offset` it is
   written at. The Meshtastic build names its LittleFS image.

2. **When nothing says otherwise, a firmware brings its companions.** When a
   run or a new device is given a firmware and no overlay, and the firmware's
   SHA-256 is an image the manifest pins with companions, each companion that
   is present and has its pinned hash is overlaid at its offset. The launch
   says so (`overlay : 0xc90000 = …, the firmware's own filesystem`), and a
   device records it among its overlays.

3. **Anything given wins.** An `--overlay` on the command line, or overlays
   passed at creation, replace the companions entirely, as before: the tests
   that boot Meshtastic with a configured filesystem keep booting with it.

4. **A missing companion is said, not invented.** If it is not fetched, the
   launch says which one and how to fetch it, and the run goes on without
   it.

## Consequences

**Good**

- A Meshtastic device made from the page boots to its setup dialog, as a
  T-Deck flashed by the vendor's installer does.

**Costs, accepted**

- The firmware is hashed at each launch to find its entry: about 3.5 MB,
  milliseconds.
- Only a firmware the manifest pins is recognised. Another Meshtastic
  build, unpinned, still needs its filesystem passed by hand.
