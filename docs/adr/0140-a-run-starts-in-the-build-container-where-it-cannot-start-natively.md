# ADR 0140 — A run starts in the build container where it cannot start natively

**Status:** Accepted — 2026-11-18. Amends ADRs 0133, 0134 and 0135, which
start a run as a child `emu/run.py` on whatever host the window runs on.

## Context

The README makes `python -m emu.web` the front door, and on this project's
Windows host it opens, lists the devices, and cannot start one. Pressing
Start on `pocket` (2026-11-18) left this in the run's `console.log`:

    File "emu/guest_clock.py", line 53, in start
      self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    AttributeError: module 'socket' has no attribute 'AF_UNIX'

Two things stand between a Windows host and a run, and neither is a bug in
the run:

- **Unix sockets.** Every board model talks to QEMU over Unix sockets in the
  run's output directory (`gpio.sock`, `i2c0.sock`, `bus.sock`, ...).
  CPython on Windows has no `AF_UNIX`.
- **QEMU itself.** `third_party/qemu/build/qemu-system-xtensa` is built in
  the build container (`tools/build_qemu.sh`), and is a Linux program.

Every slow test and every hand-run already happens in that container, with
the repository mounted at `/work`. Measured the same day: a Unix socket
binds and connects inside the mount (`/work/logs`) as well as in `/tmp`, so
a run in the container can keep its output directory where the host reads
it.

The page then made it worse: the API reported the run `alive: false`, and
the page went on showing "running 17s" and "waiting for guest output"
forever, with the reason in a file it never shows.

The device's own `device.json`, written on Windows, records its firmware as
`firmware\T-Deck-MAX_V3.1_20260313.bin`. On Linux that is one file name with
a backslash in it, so a run in the container would not find the firmware
for its symbols.

## Decision

1. **`RunHandle` starts a run natively where the emulator can run, and in
   the build container where it cannot.** "Cannot" is a host whose Python
   has no `AF_UNIX`. `BOARDEMUL_RUN_IN=native` or `container` overrides
   that choice. Both front doors start runs through `RunHandle` (the Tk
   chooser, ADR 0134, and the web, ADR 0135), so both get this.

2. **The container run is the native run, inside `docker run`.** The same
   `emu/run.py --device` command, with three mounts so no path is
   translated by guesswork: the repository at `/work`, the device folder at
   `/device`, the run's output directory at `/out`. The image is
   `BOARDEMUL_IMAGE`, by default `boardemul-build:latest`, built from
   `.devcontainer/Dockerfile`. The container is named after the device and
   run number, so it can be stopped by name.

3. **Stopping is still the run's own interrupt.** `docker stop --signal
   SIGINT` delivers the same SIGINT a native stop sends, so the run's
   shutdown path runs and the card goes back to the folder (ADR 0133).

4. **A run that has ended says so, and why.** The run view asks the API
   whether its run is alive. When it is not, it shows "ended" and the last
   lines of the run's `console.log`, which a new route serves. A start that
   fails outright, such as no Docker on the host, shows its error.

5. **A device folder names its firmware with forward slashes**, and reads
   either kind, so a folder made on Windows works in the container and back.

## Consequences

**Good**

- `python -m emu.web` on Windows starts and stops devices: the page, the
  browser and Ctrl-C stay on Windows; only the run moves into Linux.
- The Tk chooser gains the same thing without a line of its own.
- A run that dies at start is visible in the page, with its reason.

**Costs, accepted**

- Docker Desktop has to be running, with the image built and QEMU built
  into the mounted tree. The error says which of these is missing; the
  emulator does not try to build them.
- Starting a container adds a second or two to a run's start.
- A device folder outside the repository works, through its own mount, but
  its firmware path is only meaningful if it is under the repository.
