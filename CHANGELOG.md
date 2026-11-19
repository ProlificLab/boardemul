# Changelog

What changed for someone running BoardEmul, newest first. The reasons are in
`docs/adr/`, numbered as below.

## 1.1.0 — 2026-11-19

**Faster**
- The two cores take turns of a millisecond instead of QEMU's tenth of a
  second: Meshtastic reaches its home screen about a fifth sooner (0161).

**Reaching a device**
- A port of this computer can reach a TCP port of the device over its WiFi,
  set in the settings sheet (`8080:80`, or `4403:4403` for Meshtastic's
  API). Meshtastic itself has not yet been seen joining the run's network
  from its own interface, so its apps are not yet shown connecting (0164).
- A run's logs download as one zip that names the BoardEmul version, the
  QEMU it was built from and the device's settings, for a report (0163).
- The device list shows every running device's screen, live.

**Fixes**
- Light sleep wakes: on its timer, and on the pins the firmware chose, such
  as the T-Deck's trackball under Meshtastic. A node that light-slept used to
  stay on its last picture for good (0165).
- Two devices made from the library no longer share a MAC, and so a
  Meshtastic node number (0155).
- Keys typed on the page are released: a letter typed twice reached the
  T-Deck once, and Enter and Backspace did nothing (0127).
- A device made from the page or `device.py create` restarts after a panic,
  as the board does (0145).
- `python boardemul.py` rebuilds the runtime image when its QEMU sources
  change, and the image carries every library QEMU links (0159). A second
  start while one runs stops and says the port is in use; on Windows it used
  to share the port with the first, and requests went to either.
- A device's SD card is written back whole or not at all: a run stopped
  while writing it could leave it cut short and the device unable to start
  (0133).
- Text sent to a T-Deck through the scripting API is typed, not refused.

## 1.0.0 — 2026-11-19

The first version for operators.

**Getting it going**
- `python boardemul.py` checks the machine, builds the runtime image with
  QEMU inside once, fetches the pinned firmware and opens the page (0159).
- `tools/doctor.py`, and the page's red banner, say what is missing and
  what fixes it (0147, 0153). The firmware is fetched from the commits it
  is pinned at, and kept only if its hash matches (0147).
- `docs/GUIDE.md` is the operator's guide.

**Devices, like virtual machines**
- A device keeps its flash, eFuses, card and run settings between runs
  (0133, 0145), is made from the firmware library on its own board (0160),
  and can be snapshotted, restored and cloned with a MAC of its own (0149,
  0155). Its SD card can be downloaded, replaced, or made from files (0149).
- A firmware brings the filesystem its installer writes (0150), and its
  PSRAM is presented in the line mode its own driver expects (0144).

**Using a device**
- The page shows the panel live, takes touch, keys and the trackball,
  reads and types into either serial port (0156), plays what the speaker
  played and feeds the microphone a WAV file (0146), and dims the T-Deck's
  screen with its backlight (0151).
- A serial port can be put on a TCP port for the host's own tools (0158).
- `device.py start` runs a device from a shell on every host (0140).

**Radios**
- Devices that share an air name hear each other's LoRa radios, across
  containers on Windows too (0145). WiFi joins a WPA2 access point and can
  reach the real internet (0084–0097); the Max's scan lists networks (0152).
- Bluetooth advertises and holds a connection, and carries no data (0157).

**Fidelity**
- Chip behaviour taken from Espressif's QEMU and its silicon measurements:
  CPENABLE at reset (0143), resets of the digital core (0142, 0154), the
  Max's SENS block (0152), and I2S's update and owner bits (0146).
