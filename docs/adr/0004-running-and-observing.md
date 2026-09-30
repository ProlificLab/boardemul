# ADR 0004 — Launch QEMU deterministically, and observe with the memory-region trace

**Status:** Accepted — 2026-08-21

## Context

The first boot invalidated the observation mechanism the plan assumed.

`-d unimp,guest_errors` produced six lines across a 25-second run and said
nothing about the peripheral the firmware was stuck on. The memory map explains
why:

```
0x60000000-0x600d0fff (prio 0, i/o): esp32s3.iomem
```

The machine maps a catch-all region over the entire peripheral window. Access
to an unmodelled peripheral does not fault and does not reach QEMU's
unimplemented-device logging — it lands in the catch-all, returns zero, and the
guest spins on a status bit that will never set. Silence, indefinitely.

Meanwhile `-d trace:memory_region_ops_read,trace:memory_region_ops_write`
yields, per access, the absolute address, the value, the width, and the name of
the region that serviced it:

```
memory_region_ops_read cpu 1 mr 0x... addr 0x600c0004 value 0x0 size 4 name 'esp32s3.soc.clk'
```

Measured cost: about 1.6 MB and 15,700 lines per 10 seconds, and roughly a 100x
slowdown.

## Decision

1. **The trace is the observation mechanism**, not `-d unimp`. The latter stays
   enabled because it is free, but nothing depends on it.

2. **"Unmodelled" is defined by QEMU, not by us.** An access is unmodelled if
   the region that serviced it is the catch-all `esp32s3.iomem`, or a
   background stub registered at priority -1000. This is derived from the
   emulator's own routing rather than from our reading of the technical
   reference manual, so it cannot drift out of step with the machine.

3. **Two runs per image, not one.** An untraced run produces the UART narrative
   at usable speed; a traced run produces the MMIO census. Merging them would
   force every run to pay the 100x and would distort the timeline the UART log
   is supposed to show. Two runs are also independently reproducible, which one
   mode-switching run would not be.

4. **Determinism is pinned explicitly and recorded per run.** `-seed` for the
   hardware RNG (derived from the board's MAC since ADR 0098, so two boards
   do not share a random stream), `-rtc base=...,clock=vm` for time, `strap_mode` for the boot
   pins, and the eFuse blob from ADR 0003 for the MAC. Every run writes a
   manifest containing the input hashes, the full argv, and the QEMU version.
   A result that cannot be tied to its inputs is an anecdote.

5. **`-icount` is not used.** It is the obvious way to pin timing, and it
   interacts badly with SMP, which this machine needs. Timing is therefore
   *not* deterministic, and no conclusion may be drawn from how long anything
   took. Recorded as a limitation rather than papered over.

6. **QEMU is located by `BOARDEMUL_QEMU`, then the local build, then `PATH`** —
   in that order, with no container-specific paths anywhere, per ADR 0002
   decision 5.

## Consequences

**Good**

- The stall is diagnosable: aggregating trace reads by address turns "it hung"
  into "it read `0x60003008` four thousand times and got zero every time",
  which names the missing peripheral directly.
- The unmodelled set is machine-derived, so it stays correct as the fork gains
  peripherals.

**Costs, accepted**

- The traced run is slow enough that reaching the application stage takes
  minutes rather than seconds. Budgeted for rather than optimised away.
- Two runs mean two chances for run-to-run divergence. The manifest exists so
  divergence is detectable instead of confusing.
- No timing claims. Ever, from any run.

## Alternatives rejected

**Keep `-d unimp,guest_errors` as primary.** Demonstrated insufficient on the
very first boot: the catch-all region swallows exactly the accesses we care
about.

**One traced run with a long timeout.** Simpler, and it makes every run pay a
100x cost even when all that is wanted is the UART output — which is the common
case during development.

**Boot untraced, then enable tracing through the monitor once the application
is reached.** Genuinely the best of both, and deferred rather than rejected: it
needs a QMP control plane, and introducing one here would put machinery in M0
that belongs with the bus bridge. Worth revisiting once that exists.
