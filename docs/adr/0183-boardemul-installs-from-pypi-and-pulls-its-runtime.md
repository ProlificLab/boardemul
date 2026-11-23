# ADR 0183 — BoardEmul installs from PyPI and pulls its runtime

**Status:** Accepted — 2026-11-23. Carries out the product interview's Q9
and Q12 (ADR 0180, decision 4).

## Context

BoardEmul runs from a clone (ADR 0159): `python boardemul.py` builds the
runtime image from the clone's sources and keeps devices, firmware, logs
and builds in folders beside the code. Its users are to have it with
`pipx install boardemul` too, and the clone stays for those who change the
emulator. An installed package has no Dockerfile and no QEMU sources
beside it, and the folder its code is in is not where a user's devices
belong.

## Decision

1. **A home for what a user keeps.** In a clone -- the code with
   `docker/runtime/Dockerfile` beside it -- it is the clone, as now.
   Installed, it is `~/.boardemul`. `BOARDEMUL_HOME` names another. Its
   `devices/`, `firmware/`, `logs/` and `build/` are where the page, the
   runs, the firmware library and `tools/device.py` keep theirs
   (`emu/home.py`).

2. **The firmware manifest travels with the code**: it moves from
   `firmware/MANIFEST.toml` to `tools/firmware_manifest.toml`; what it
   fetches goes to the home's `firmware/`.

3. **An installed BoardEmul pulls its runtime image**,
   `ghcr.io/prolificlab/boardemul:VERSION`, the one CI built from the same
   tag, instead of building it. A clone builds its own, as before, because
   its sources are what it runs.

4. **The package** is `boardemul` on PyPI: the `boardemul`, `emu` and
   `tools` packages with the page and the manifest, the standard library
   only, and a `boardemul` command (`boardemul`, `boardemul test`).

## Consequences

**Good**

- `pipx install boardemul` and `boardemul` are the whole installation on a
  machine with Docker; a clone works exactly as before.

**Costs, accepted**

- Two places a user's devices can be, a clone's and the home; a user who
  moves from one to the other moves them (or sets `BOARDEMUL_HOME`).
- An installed version runs only the image published for it.
