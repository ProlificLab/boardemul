# ADR 0174 — What QEMU writes during a run is kept on local disk and copied out

**Status:** Accepted — 2026-11-20. Revises ADR 0164's account of Meshtastic's
WiFi join, and much of what ADRs 0161, 0170 and 0172 measured as speed.

## Context

QEMU's CPU thread was found busy only about a third of the time during a
Meshtastic boot. Sampling where it slept (`/proc/<pid>/task/<tid>/wchan`,
4,000 samples; `logs/dbg/wchan_wifi.sh`) during the stretch between
Meshtastic's WiFi.begin and its first authentication, about 65% of the
samples were in `p9_client_rpc`: a file operation over 9P, the protocol
Docker Desktop on Windows uses for a folder shared from the host (`/work`
is `9p ... aname=drvfs` in `/proc/mounts`). The thread that runs the guest
was waiting on Windows' filesystem.

What it writes there, while the guest runs:

- its log (`-D`), with `-d unimp,guest_errors` on: our models' notes and
  QEMU's own complaints, line-buffered, so one write per line. In that
  stretch, about 32,000 SHA operations by DMA each left six lines, Espressif's
  GDMA model reading the descriptor at address 0 after the end of a list
  (ADR 0164); hundreds of thousands of lines;
- the UART and USB Serial/JTAG files, written as the guest writes the port.

Measured with the same device folder and firmware, WiFi.begin to the access
point's first authentication:

| Device folder | Run's output | Guest seconds | Wall to authentication |
| --- | --- | --- | --- |
| shared (`/work`) | shared | 97 to 104 | 112 to 125 s |
| container disk | shared | 97 | 112 s |
| shared | container disk | 7.8 | about 22 s |
| container disk | container disk | 7.1 | 22 s |

The flash file being shared costs almost nothing; the output folder costs
everything. The slow join of ADR 0164 was this, not the CPU: under `-icount`
it had looked right in guest time (5.3 s) because blocked I/O costs no
instructions.

Every device run that `tools/device.py` or the page starts on Windows (or
macOS) writes into folders shared from the host (ADR 0140's mounts). The
tests did not see it: their temporary folders are on the container's disk.

## Decision

1. **Where a run's output folder is on a host-shared filesystem** (9p,
   virtiofs, FUSE, CIFS, NFS — read from `/proc/mounts`), the files QEMU
   writes during the run — its log, the UART's and the USB Serial/JTAG port's
   — are written to a temporary folder on local disk instead
   (`emu/local_mirror.py`).
2. **They are copied into the output folder as they grow**: what is new,
   every half second, in one write per file; and whatever is left once QEMU
   has stopped, before the run's manifest is written. Readers of those files
   during a run (the page's console, a test waiting for a line) see them up
   to half a second late.
3. **What reads them to keep the run going reads the local file**: the stall
   watchdog (ADR 0004's liveness) and the serial port bridge (ADR 0158).
4. **On local disk nothing changes**: the files are written where they always
   were, and nothing is copied.

## Consequences

**Good**

- On Windows, a device run is no longer held up by its own log: Meshtastic
  joins WiFi five times sooner in wall time, and its own timeline shows the
  seven seconds a board takes rather than a hundred.
- The measurements that blamed the CPU for what was I/O can be redone; the
  tests, on local disk, were already measuring the CPU.

**Costs, accepted**

- A run that is killed outright (no Python teardown) loses up to half a
  second of those files' tail; ADR 0136's watchdog bounds the teardown that
  copies the rest.
- Twice the bytes are written for those three files on a shared folder.
