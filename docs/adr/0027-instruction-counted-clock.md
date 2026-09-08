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
budget, where the wall-clock run does.

*Resolved 2026-09-05.* That had nothing to do with the clock. The build was
blocked in its audio path, which never drained because the I2S transmit side
stopped following its descriptor ring; it printed the line under either clock
once that was fixed. The suspicion recorded here was wrong, and cost time
later by making the clock look guilty.

## Decision

1. **`run.py --icount`** adds `-icount shift=2,sleep=on -accel
   tcg,thread=single`. Off by default.

2. **It is for firmware whose behaviour depends on exact timing**, which
   means the Bluetooth link layer, and not for a firmware as a whole. The test
   support takes an `icount` flag and no slow test currently sets it.

   *Amended 2026-09-08.* This first said the Meshtastic slow tests run with it,
   which was reasoning from the firmware rather than from what the test needs:
   none of them exercise Bluetooth. They assert on what the guest says and
   draws, not on when, and they pass on the wall clock. The counted clock cost
   them about seven times their runtime -- three fixtures, five hundred and
   twenty-six seconds against eighty-four.

## Consequences

**Good**

- Timing-sensitive guests get the clock the silicon gives them; runs that
  use it are reproducible instruction for instruction.

**Costs, accepted**

- Two ways to run, and a test must say which it means.

**Settled since**

- The 250620 battery marker was the I2S ring, not the clock.
- The counted clock is not the default and should not be: it costs several
  times the runtime and buys determinism that only timing-dependent firmware
  needs.

## Resolved — 2026-09-07

Both of the above are settled, and the first was not about this clock at all.
The 250620 build was losing its battery marker on the wall clock too: its audio
stream never drained, it blocked writing to I2S and starved every other task.
That was a defect in the I2S transmit path, fixed separately; the counted clock
merely made a firmware that was already stuck take longer to look at.

With that fixed, both vendor builds reach the battery read under the counted
clock, and draw hundreds of frames doing it. The clock is no longer suspected
of anything.

It stays opt-in even so. In the same wall-clock budget the counted runs get
through roughly two thirds of the work, which is a real cost on every test that
does not need it, and only the Bluetooth link layer does. The reason to reach
for it is determinism: eight chained Meshtastic runs under it produced
byte-identical output, which is what makes a timing bug reproducible.
