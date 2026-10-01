# ADR 0108 — The Max's sensor hub boots the firmware it is given

**Status:** Accepted — 2026-10-01. Closes item 5 of ADR 0106.

## Context

The T-Deck Max carries a Bosch BHI260AP. It is a sensor *hub*: the host uploads
a firmware image into its RAM, boots it, and then reads "virtual sensors" out
of its FIFOs. The board model had a register file with one value in it, at
the wrong address: `0x89` at `0x2B`, where the chip id belongs. The factory
demo said so: `reset bhy2 failed! ... Timed out`.

The protocol is published, in Bosch's BSD-licensed BHY2 SensorAPI as SensorLib
vendors it, which is the library the demo uses. From `bhy2_defs.h`,
`bhy2_hif.c` and `bhy2.c` (SensorLib v0.2.4):

- registers: product id `0x89` at `0x1C`, chip id `0x70` at `0x2B`, reset
  request `0x14`, boot status `0x25`, kernel version `0x20`, interrupt status
  `0x2D`;
- a reset is answered by "host interface ready" (boot status bit 4);
- commands go to channel 0 as command, length, and payload padded to four
  bytes, split over writes. The firmware upload counts its length in words.
  The driver checks for "verify done" after the upload, before it asks for a
  boot;
- a parameter read is the parameter with `0x1000` set. Its answer is on the
  status channel as code, length and data, signalled by interrupt-status
  bit 5. `0x011F` is the present-sensor bitmap and `0x0300 + id` a sensor's
  28-byte description, with the event size at offset 20;
- `CONFIG_SENSOR` (`0x000D`) carries the sensor id, a float rate and a 24-bit
  latency;
- each FIFO transfer begins with its two-byte length. Records are a sensor id
  and its event, or system records (a full timestamp `253` of six bytes, a
  large delta `252` of three), in 1/64000 s.

## Decision

1. **`emu/devices/bhi260ap.py`** models that host interface. It identifies
   itself, resets, takes and verifies an upload, boots, answers the
   present-sensor and sensor-info parameters, and runs the sensors it is asked
   to.

2. **The sensors are the accelerometer and gyroscope**, in their pass-through,
   raw and corrected forms (ids 1, 3, 4, 10, 12, 13), with 7-byte events.
   Nothing says the board has a magnetometer, so none is claimed.

3. **Samples come on the board's clock** (ADR 0104), at the configured rate,
   produced when the host looks, timestamped, and bounded by the FIFO size.

4. **Three things are stated choices, not measurements.** The board lies still
   and face up: 1 g on z, no rotation, at the 1/4096 g per count SensorLib
   scales by. The sensor-info fields other than the event size are plausible
   values. The kernel version after boot is 1, which is non-zero, as a booted
   hub's is. The real number is in the uploaded image's build, and no
   document read here says where.

## Consequences

**Good**

- Under its own driver the demo now uploads 103,676 bytes of Bosch firmware,
  boots it (`Boot successful`), lists six sensors online, and enables the
  accelerometer and gyroscope at 100 Hz.

**Costs, accepted**

- A stationary board only. Motion would be a scenario input, like `--gps`.
- Wake-up sensors, the status channel's asynchronous messages and meta events
  are not produced; nothing read here asks for them.
- The demo reads the FIFOs only on its sensor page. That path is covered by
  the fast tests, not by a boot.
