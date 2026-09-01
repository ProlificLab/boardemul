# ADR 0001 — Use the Espressif QEMU fork as the CPU and SoC engine

**Status:** Accepted — 2026-08-21

**Amended 2026-08-25:** decisions 2 and 3, the boundary rule, are superseded by
[ADR 0011](0011-transaction-granularity-boundary.md), which moves the boundary from
register accesses to bus transactions and restates the rule as *no board-specific
knowledge in C*. The decision to adopt the fork as the CPU and SoC engine, and
everything in the Context and Alternatives sections, stands.

**Amended 2026-09-01.** The boundary now admits exactly one patch to their own
logic, applied from a declared patch file and argued in ADR 0017. The rule was
never "no patches"; it was "no undeclared drift".

## Context

Running unmodified vendor firmware means executing Xtensa LX7 machine code.
There are only two ways to get that: write an LX7 interpreter, or adopt one.

An interpreter is months of work, carries high correctness risk, and is the
least differentiated part of this project. Nobody needs another Xtensa
interpreter. What this project is actually *about* is the device models — the
e-paper controller, the keyboard, the LoRa radio — and none of that work starts
until instructions execute.

Espressif maintains a QEMU fork that already models the ESP32-S3 core, the
flash cache and MMU, and enough of the SoC to boot ESP-IDF.

The danger in adopting a component that large is not the dependency itself. It
is drift: our logic gradually migrates inside it, patches accumulate across its
tree, and within a few months rebasing onto a newer upstream is impossible.
That is how a tool becomes an architecture. Preventing that is the entire point
of this ADR.

## Decision

1. Adopt `espressif/qemu`, pinned to tag `esp-develop-9.2.2-20260417`
   (QEMU 9.2.2 base), as the CPU and SoC engine. It is the only large external
   component in the project.

2. **The boundary rule.** Our C code inside QEMU lives in files we add, and
   nowhere else. Concretely, the permitted footprint is:

   - at most one new source file under `hw/misc/`
   - one registration line in `hw/misc/meson.build`
   - one entry in `hw/misc/Kconfig`

   No edits to any existing QEMU source file. Not to `hw/xtensa/esp32s3.c`, not
   to the machine definition, not anywhere.

3. That added file's only job is to forward bus transactions out of the QEMU
   process. It holds no device behaviour: no register decode, no state
   machines, no timing, no interrupt logic.

4. All device intelligence lives outside QEMU, in Python.

5. QEMU is not vendored into this repository. It is fetched by tag and built
   (ADR 0002).

The mechanism that makes rule 2 achievable is deliberate and worth recording,
because it is the reason the footprint can be this small. Our device registers
as a plain `TYPE_DEVICE` rather than a `SysBusDevice`. A `SysBusDevice` cannot
be instantiated from the command line unless the machine sets
`has_dynamic_sysbus`, which would force an edit to the machine file. A plain
`TYPE_DEVICE` has no such restriction: it can be created with `-device`, and
its `realize()` claims MMIO by calling `memory_region_add_subregion_overlap()`
on `get_system_memory()` at a base and size given by properties, at a priority
above the machine's own models. Nothing in the machine definition needs to know
we exist.

## Consequences

**Good**

- Bumping to a newer fork tag is a two-line diff plus one file. The upgrade
  path stays open indefinitely.
- Device models are written, tested, and reloaded in Python with no C rebuild.
- Everything the fork already models correctly — flash cache and MMU, SHA, AES,
  RSA, eFuse, timer groups, octal PSRAM — we get for free and never write.

**Costs, accepted**

- Every bus transaction crosses a process boundary. Throughput is bounded by
  IPC, not by QEMU. For a single device at human interaction rates this is the
  right trade; for a thousand simulated nodes it would not be. We are building
  the former.
- We inherit the fork's bugs and its release cadence. Mitigated by pinning, and
  by the differential oracle in ADR 0002.
- Once the guest stalls waiting on an external round-trip, QEMU's virtual clock
  no longer relates to the guest's view of elapsed time. Timing-sensitive guest
  code is a known future problem, deliberately deferred rather than solved
  here.

**Constraint this places on M0**

M0 writes zero C. If M0's observation mechanism required a patch, we would have
taken on the C dependency before we needed it and before we understood the
shape of the problem. The mechanism must therefore be achievable with stock
QEMU command-line flags (ADR 0004).

## Alternatives rejected

**Write our own Xtensa LX7 interpreter.** Months of work, high correctness
risk, and zero differentiation. It would delay the first interesting problem by
a quarter.

**Upstream QEMU.** No ESP32-S3 machine exists. We would be writing the SoC
ourselves, which is most of the fork's value.

**Renode.** A capable framework, and that is the objection: the framework owns
the structure. Device models are written to its API, its config language, and
its assumptions. It inverts the prime directive — we would understand our
models but not the system they live in.

**Fork QEMU and edit freely.** The obvious short-term path and the one that
ends the project's upgradability. Rejected explicitly so that the temptation is
on the record as already considered.

**Wokwi or similar hosted simulators.** Closed, and not extensible to this
board's peripheral set.
