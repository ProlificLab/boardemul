# ADR 0159 — One command starts BoardEmul, on a runtime image with QEMU inside

**Status:** Accepted — 2026-11-19. Amends ADR 0002, decision 2, for people
who run BoardEmul rather than develop it; ADR 0147's three commands remain
the developer's.

## Context

ADR 0147 made a new machine ready in three commands: build the image, build
QEMU, fetch the firmware. The second is the one an operator meets as a wall.
On Windows it compiles QEMU over the repository's bind mount, and on
2026-11-18 a rebuild that touched a `meson.build` took about forty-five
minutes there. It also leaves the operator with a development tree and an
image full of compilers.

ADR 0002, decision 2, kept QEMU's compile out of image layers on purpose:
"the build would become something that happens to you rather than something
you can read, time, and debug". That is right for whoever works on the
emulator. For whoever runs it, the compile is something that happens to them
either way, and once is better than on every machine they use.

## Decision

1. **A second image, `boardemul-runtime`**, from `docker/runtime/Dockerfile`:
   - its first stage is the build image's own dependencies, and runs the same
     `tools/build_qemu.sh` on the same pin, in the image's file system instead
     of over a bind mount, then installs the result into a staging prefix;
   - its second stage holds Python and the libraries QEMU links against, and
     the installed QEMU under `/usr/local`, and names it in
     `BOARDEMUL_QEMU`, which `run.py` already prefers.
   The development image and `tools/build_qemu.sh` are unchanged. A
   `.dockerignore` keeps the repository out of the build context but for the
   script and `qemu-patch/`.

2. **`python boardemul.py` is the one command**, from a clone:
   - it says what is missing, by the doctor's checks (ADR 0147), and stops
     at what it cannot fix: Python, Docker, Docker's daemon;
   - it builds the runtime image if there is none, saying it does it once and
     how long it may take;
   - it fetches what the firmware manifest pins and is missing;
   - it starts the page with runs in the runtime image, and opens it.
   A second run of it finds everything in place and only starts the page.

3. **The doctor knows the runtime image.** When the image a run uses carries
   QEMU (`BOARDEMUL_QEMU` in its environment), the local QEMU build is not
   needed and is not asked for.

4. **The image says what it was built from.** The launcher passes a digest of
   `tools/build_qemu.sh`, the runtime Dockerfile and every file under
   `qemu-patch/` (line endings made alike), and the image keeps it as the
   label `org.boardemul.sources`. When a clone's digest differs, after a
   pull that changed a device written in C or moved the pin, the doctor
   calls the image an error rather than letting runs meet a QEMU that lacks
   a device `run.py` now asks for, and the launcher builds it again, saying
   why. Docker's layer cache keeps that rebuild to QEMU's compile: the
   package layers are unchanged.

## Consequences

**Good**

- A machine with Docker and Python goes from a clone to the page with one
  command, and the compile happens once, where it is fastest.
- Runs no longer depend on `third_party/` for anyone who does not develop
  QEMU's side.

**Costs, accepted**

- A second image to keep in step with the pin. It is built by the same
  script from the same pin, so a pin bump that the build script takes is
  one rebuild of each, and decision 4 makes that rebuild happen rather than
  be remembered.
- Any change under `qemu-patch/`, its README included, rebuilds the image.
  Docker's own cache already behaves so, since the directory is one `COPY`;
  the label only says it out loud.
- The runtime image is still built on the operator's machine; no registry
  serves it. Publishing it is a choice for whoever owns one.
