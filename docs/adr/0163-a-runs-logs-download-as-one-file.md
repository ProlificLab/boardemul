# ADR 0163 — A run's logs download as one file, saying what they ran on

**Status:** Accepted — 2026-11-19. Adds to ADRs 0135 and 0162.

## Context

When a firmware misbehaves on BoardEmul, the evidence is the run's
directory: both serial ports, the bus log, QEMU's log and stderr, the input
the person gave, the frames. It lives under `logs/devices/NAME/N/` on the
machine that ran it, beside Unix sockets and pipes that are no use to anyone
else, and nothing in it says which BoardEmul, which QEMU or which device
settings produced it. ADR 0162 gave BoardEmul a version so that a report can
name what it was seen on; a report still has to be assembled by hand.

## Decision

1. **`GET /api/devices/NAME/logs.zip`** returns the device's latest run
   directory as a zip: every regular file in it, frames included, sockets
   and pipes left out. While the run is live it is a snapshot of what is
   written so far; after it stops, or ends by itself, it is the whole run,
   which a person usually wants once the misbehaviour has happened.

2. **The zip says what it ran on**, in `boardemul.txt` at its root: the
   version (ADR 0162), the digest of the QEMU sources (ADR 0159), the host
   platform and whether runs start in a container, and the device's own
   manifest, `device.json`, with its board, firmware hash, MAC and settings.

3. **The run view has a *Download logs* button** beside the frame's own
   save button, and the guide tells a person reporting a problem to attach
   it.

## Consequences

**Good**

- A problem report is one file, and it names the build, the QEMU and the
  device it was seen on.

**Costs, accepted**

- A long run's bus log can be tens of megabytes; the zip is built in memory
  and compressed, which text logs take well.
- Only the latest run is offered; earlier ones are in their directories as
  before, numbered.
