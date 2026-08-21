# ADR 0002 — Build QEMU from source in a devcontainer

**Status:** Accepted — 2026-08-21

## Context

The development host is Windows 11. QEMU's build system (meson, ninja, glib,
pixman) is a poor fit for native Windows: Espressif produce their own Windows
binaries by cross-compiling with mingw in CI and do not document a native
Windows build. ADR 0001 requires that we build from source, because a prebuilt
binary cannot contain the file we will add in M1.

Three hosts were considered — WSL2 directly, Python-on-Windows driving
QEMU-in-WSL2, and a native mingw build — and a devcontainer was chosen.

The devcontainer settles a second question for free. Ubuntu 24.04 ships Python
3.12.3, which is exactly the version this project was specified against. The
Windows host has 3.13.14. Using the container removes the mismatch instead of
managing it.

**A tension worth naming.** The project constraints say "no Docker". The
reading adopted here is: no Docker *in the product*. The emulator must run as
plain processes, launchable by hand, with no container in the path between the
user and QEMU. The container is a build and development host only. This is a
real distinction and it needs enforcing, not just stating — see decision 5.

## Decision

1. Development happens in a devcontainer built on
   `mcr.microsoft.com/devcontainers/base:ubuntu-24.04`.

2. The Dockerfile installs build dependencies **and nothing else**. It does not
   build QEMU. Baking a fifteen-minute compile into an image layer hides it:
   the build would become something that happens to you rather than something
   you can read, time, and debug.

3. `tools/build_qemu.sh` fetches the pinned tag into `third_party/` and builds
   it. It is short enough to read in one sitting, which is the point.

4. The build is configured `--target-list=xtensa-softmmu --disable-werror
   --enable-trace-backends=log`. The trace backend is pinned explicitly rather
   than left to the default because M0's observation mechanism depends on
   `-d trace:...` working (ADR 0004). A default that silently changed would
   break the report generator in a confusing way.

5. **Nothing under `emu/` or `tools/*.py` may assume the container exists.** No
   hardcoded `/workspaces` paths, no assumption that QEMU is at a fixed
   absolute location, no container-only tooling in the runtime path. The test
   of this rule is that the emulator runs from a plain shell on any Linux host
   with QEMU on `PATH`. If that ever stops being true, decision 5 has been
   violated and the "no Docker in the product" constraint has quietly been lost.

6. `tools/fetch_qemu_oracle.sh` downloads Espressif's official prebuilt binary
   for the *same* tag. It is never used to run the emulator. Its sole purpose
   is differential diagnosis: when our build behaves strangely, running the
   identical image on Espressif's binary answers "is it our build or is it the
   fork?" in one command. Without it, that question costs an afternoon.

## Consequences

**Good**

- One reproducible host. The QEMU that M0 measures is the QEMU that M1 patches.
- Python 3.12.3 as originally specified, without version-managing anything.
- The build is visible: a script that prints what it does, not an opaque layer.
- Differential diagnosis against the vendor's own binary is one command away.

**Costs, accepted**

- Container startup and a one-time ~15 minute QEMU build.
- Decision 5 is a discipline, not a mechanism. Nothing automatically enforces
  it. It will be checked by hand at each milestone boundary until there is a
  reason to automate it.
- The repository lives on a Windows filesystem mounted into the container.
  Filesystem performance is mediocre; irrelevant at our file sizes, and worth
  noting only so it is not rediscovered as a mystery.

## Alternatives rejected

**WSL2 directly, no container.** Fewer moving parts and genuinely tempting. The
objection is reproducibility: a WSL distribution accumulates state, and six
months from now "it works on my WSL" is unfalsifiable. The Dockerfile is a
written record of the host.

**Python on Windows driving QEMU in WSL2.** Rejected on a forward-looking
ground. WSL2 is a virtual machine, so Unix domain sockets do not cross the
boundary. M1's bus bridge would be forced onto TCP loopback, and every path
handed to QEMU would need translation, forever, to save an afternoon now.

**Native Windows build via MSYS2/mingw.** No host boundary at all, which is
attractive. Rejected because Espressif do not test this path, and because
CPython does not expose `AF_UNIX` on Windows regardless — so it fails to
deliver the one advantage that would justify the yak-shaving.

**Prebuilt binary only.** Fastest path to a first boot, and incompatible with
ADR 0001: a prebuilt cannot contain our bus bridge. Kept as an oracle
(decision 6), never as the engine.
