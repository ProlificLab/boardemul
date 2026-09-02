# ADR 0027 — An instruction-counted clock, as an option

**Status:** Accepted — 2026-09-02.

## Context

QEMU's virtual clock follows the host's wall clock unless told otherwise.
When the guest runs slower than real time -- under a translation log, a busy
host, or simply because two Xtensa cores in TCG do not make 240 MHz -- its own
clock runs ahead of its code. Most firmware does not notice. The Bluetooth
link layer does: it programs events for a time it read from that clock, and
the core (ADR 0025) skips an entry whose time has passed. Under the
translation log the first advertising entry arrived eight milliseconds late
and was skipped; without it, one of forty-seven still was.

With `-icount shift=2`, the virtual clock advances four nanoseconds per
executed instruction, whatever the host does. Every one of three hundred and
twenty advertising events in a run then ended on schedule, and the run is
deterministic: the same guest time for the same instruction. The cost is a
single TCG thread and guest time that runs at roughly half of wall time on
this host.

It is not free for everything: under the counted clock the vendor firmware's
250620 build did not print its battery-read line within the slow test's
budget, where the wall-clock run does. Whether that is only the slower guest
time or a timing the build depends on is not yet measured.

## Decision

1. **`run.py --icount`** adds `-icount shift=2,sleep=on -accel
   tcg,thread=single`. Off by default.

2. **The Meshtastic slow tests run with it**, and the test support takes an
   `icount` flag. The vendor T-Deck tests keep the wall clock until the
   250620 observation is understood.

## Consequences

**Good**

- Timing-sensitive guests get the clock the silicon gives them; runs that
  use it are reproducible instruction for instruction.

**Costs, accepted**

- Two ways to run, and a test must say which it means.

**Open, as of this record**

- The 250620 vendor build's battery marker under the counted clock.
- Whether the counted clock should be the default once that is understood.
