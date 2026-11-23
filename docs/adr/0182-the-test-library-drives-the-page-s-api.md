# ADR 0182 — The test library drives the page's API

**Status:** Accepted — 2026-11-23. Shapes the first version of what ADR
0180, decision 4, promised.

## Context

A firmware developer is to test their own build from Python and from CI.
Everything a test needs is already reachable through the page's HTTP API
(ADRs 0135, 0139, 0145): devices are made from uploaded images, started
with settings, typed and tapped into, and their serial output and screen
read back. That API already works wherever BoardEmul runs: natively on
Linux, and on Windows and macOS with each run in the runtime image
(ADR 0159).

What the API lacked is the developer's own unit of work, a build: the
application image and, beside it, the filesystem image it mounts, placed at
the offset of its data partition.

## Decision

1. **`boardemul` is a package of the standard library only**, beside `emu/`
   in the repository and on PyPI. It is a client of the page's API: a test
   never imports the emulator.

2. **`Lab` is a page and the devices it made.** `Lab()` uses the page at
   `BOARDEMUL_URL` if one is given, or starts its own (the launcher with
   `--no-open` on a free port) and stops it at the end. `lab.device(name,
   build=..., board=...)` makes a device from a build (and replaces one of
   that name it made before), `device.start(air=...)` starts it, and
   devices started on the same air hear each other's LoRa radios.

3. **A `Build` is read from a PlatformIO build folder** (`.pio/build/ENV`):
   `firmware.bin` is the application; `littlefs.bin`, `spiffs.bin` or
   `fatfs.bin`, when present, is placed at the offset of the first data
   partition of type filesystem in `partitions.bin`, read as ESP-IDF writes
   that table. A folder without a partition table, or a filesystem image
   with no partition to hold it, is refused with that reason.

4. **A device answers for its board**, not its firmware:
   - `device.serial(port)` sends text and waits for a pattern
     (`expect(pattern, timeout)`), on the UART, the USB serial port or the
     USB OTG port;
   - `device.screen` gives the latest frame as a PNG's bytes and its pixels,
     waits for a predicate on it, and compares it with a reference image;
   - `device.tap(x, y)`, `device.key(name)`, `device.type(text)` act as a
     person does.
   Timeouts are wall seconds.

5. **`boardemul test`** runs a build on one device or several on one air,
   exports where they are (`BOARDEMUL_URL`, `BOARDEMUL_DEVICES`) to a test
   command it then runs, and answers with that command's exit status,
   having kept each device's logs in a folder it names. A pytest fixture,
   `boardemul_lab`, gives a test a `Lab` on that page or a new one.

## Consequences

**Good**

- One way into the emulator for the page, scripts and tests; the library
  cannot reach anything the API does not offer, so the API stays complete.
- Nothing to install beside BoardEmul itself, on any of its hosts.

**Costs, accepted**

- A test pays an HTTP round trip per call, and polls for what it waits on.
- WiFi and Bluetooth are not in the library yet (ADR 0180).
