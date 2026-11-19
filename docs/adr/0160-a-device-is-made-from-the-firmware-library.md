# ADR 0160 — A device is made from the firmware library

**Status:** Accepted — 2026-11-19. Adds to ADRs 0135 and 0147.

## Context

The page's *New device* sheet takes a firmware file dropped on it, uploads
it, and makes the device from the upload (ADR 0135). The vendor images a
person usually wants are already on the machine: `tools/fetch_firmware.py`
fetched them, by hash, into `firmware/` (ADR 0147). To use one they had to
find it in a file dialog, upload a copy of it, and pick the right board for
it by hand. A Meshtastic image made into a Max device fails without saying
why.

The manifest knows each image, but not its board. The two sections it is
written in are the T-Deck Max's repository and the T-Deck's, which is what
an image is built for.

## Decision

1. **Each flash image in the manifest names its board**, `board =
   "tdeck-max"` or `"tdeck"`. The filesystem image names none: it is not a
   firmware a device runs.

2. **`GET /api/firmware` lists the library**: each image that names a
   board, with its file name, its role and whether it is fetched.

3. **A device is made from a library image by its name**: `POST
   /api/devices` with `{"name", "image"}`. The server finds the file and
   takes the board from the manifest. The page names an image, never a
   path (ADR 0139). An image not fetched is refused.

4. **The sheet lists the fetched images above the drop zone.** Picking one
   selects its board too; dropping a file instead clears the pick.

## Consequences

**Good**

- Making a device from a vendor image is a name and a click, on the board
  it was built for, with no copy of the image uploaded.

**Costs, accepted**

- An image the manifest does not pin still goes through the drop zone, and
  its board is the person's to choose.
