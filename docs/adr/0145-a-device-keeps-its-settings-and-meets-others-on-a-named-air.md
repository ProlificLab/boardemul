# ADR 0145 — A device keeps its run settings, and meets others on a named air

**Status:** Accepted — 2026-11-18. Amends ADRs 0036, 0045 (the air's
addresses), 0133 (the device manifest) and 0139 (the settings a page may
pass). Amended 2026-11-19: decision 6.

## Context

A device folder keeps a device's identity and storage between runs (ADR
0133), and the web page starts it (ADR 0135). What a run is told about the
world around the device is not kept anywhere:

- **The page passes no settings at all.** `POST /api/devices/<name>/start`
  takes per-run settings, held to the list ADR 0139 allows (`--wifi-ap`,
  `--gps`, `--at`, …), but the page sends none, and has nowhere to enter
  them. A device started from the browser has no access point to find, no
  satellite fix and no place on any air.
- **No two devices can share an air from the page.** `--air DIR` names a
  directory, which ADR 0139 keeps off the page, because a page must never
  name a path. So a two-node mesh is a command-line affair.
- **And on Windows it cannot work at all.** Each run moves into its own
  build container (ADR 0140). The air is a directory of files, each naming
  the UDP port a board listens on, and frames go to 127.0.0.1 (ADR 0036; the
  Bluetooth air in C does the same, ADR 0045). Every container has its own
  loopback, so two runs in two containers each send to themselves.

A virtual machine manager keeps this kind of thing with the machine: its
network, its position, its peripherals are settings of the machine, applied
at every start. A device here should do the same.

## Decision

1. **The manifest keeps the device's run settings.** `device.json` gains
   `settings`, a list of run arguments held to the same list a page may
   pass, which now lives in `emu/device.py`. Every start of the device
   passes them. A start may still add settings for that one run, after the
   kept ones. `tools/device.py settings NAME -- …` sets them from a shell, and
   `POST /api/devices/<name>/settings` from the page.

2. **An air is named, not located.** `--air NAME` and `--ble-air NAME` join
   the list, with NAME a plain name (`[a-z0-9][a-z0-9-]{0,31}`). Where the
   name's directory is depends on where the run is, and the run handle
   decides it:
   - a run on the host gets `<devices>/.air/lora/<NAME>`, beside the
     folders, and `.air/ble/<NAME>` for Bluetooth;
   - a run in the build container gets `/air/lora/<NAME>` and
     `/air/ble/<NAME>`, on a Docker volume, `boardemul-air`, mounted at
     `/air` in every run's container.
   `run.py --air DIR` still takes a directory: a person at a shell names
   whatever directory they like.

3. **A board on the air says where it can be reached.** Its file names its
   port, as before, and its address when that is not 127.0.0.1:
   `PORT [timed] [host=ADDRESS]`. A file without an address means
   127.0.0.1, so every air written so far still reads the same. The
   address comes from `BOARDEMUL_AIR_HOST`: unset is 127.0.0.1, and `auto`
   is the address the host's own name resolves to. `run.py` resolves it
   once; the Python air reads it, and the C Bluetooth core is given it as a
   property. Both tell peers apart by address and port. A board's file is
   named after its run's tag, the device's name, and no longer after its
   output directory: in a container every run's directory is `/out`, and
   the first trial put two boards on one air under one name, each
   overwriting the other's file.

4. **Containers on an air share a network.** A run in the container with an
   air setting joins a Docker network, `boardemul-air`, made the first time
   it is needed, and runs with `BOARDEMUL_AIR_HOST=auto`. Each board then
   advertises its container's address on that network, and the others reach
   it there. A run without an air setting is started exactly as before.

5. **The page gets a settings sheet per device**: an access point (name,
   password, WPA2), a GPS position, the air's name and the device's place on
   it, and the instruction-counted clock. Saving it writes the manifest; the
   next start uses it.

6. **A device made by an operator restarts after a panic** (amended
   2026-11-19). The page and `tools/device.py create` give a new device
   `--reboot` among its settings, because a board restarts after a panic and
   a run without it ends instead: a Meshtastic node that hit ADR 0099's
   canary simply stopped, which is not what the board does. It is one tick
   in the settings sheet to take away. A device made directly with
   `emu.device.create`, as the tests do, starts with none, so a test still
   ends on a panic it did not forgive.

## Measured

On 2026-11-18, on Windows with Docker Desktop, `tools/air_between_containers.py`
made two devices from Meshtastic 2.7.10 and a configured filesystem, gave
them the air `between` at 0,0 and 50,0, and started both as the page does.
Each run went into its own container on `boardemul-air`, at 172.23.0.2 and
172.23.0.3. Node one heard a 102-byte packet from node two at −43.6 dBm, at
guest 196 s. That is free space over 50 m at 906 MHz from 22 dBm.

## Consequences

**Good**

- Two devices made from the page and given the same air name hear each
  other, on Linux and on Windows alike, without a path ever leaving the
  page.
- A device's WiFi, GPS and place are part of the device, the way a virtual
  machine's network is part of the machine.
- An air written by an older run, with no addresses, reads as before.

**Costs, accepted**

- The air's frames now cross a Docker bridge between containers instead of
  a loopback. The timed air (ADR 0113) still meets at barriers, so its
  guest time is unaffected; only wall time per barrier grows.
- A board advertising a container address is reachable only from that
  network. A run on the host and a run in a container cannot share an air.
  On one machine the runs are all one or all the other (ADR 0140), so this
  does not arise from the page.
- The settings a page may pass stay a list in code. A new kind of setting
  needs a line there, with a reason, as ADR 0139 required.
