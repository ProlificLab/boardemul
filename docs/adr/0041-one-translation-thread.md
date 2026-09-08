# ADR 0041 — One translation thread

**Status:** Accepted — 2026-09-08.

## Context

A rare crash has followed this project for weeks: QEMU dies with a
segmentation fault a few seconds into a run. It was never reproducible to
order — fourteen runs in a row came up clean when it was hunted directly, and
it never happened under a debugger — so it stayed an open note rather than a
fixed bug.

It reappeared the moment the slow suite got fast. Every previous hunt had run
the guest under the counted clock, and instruction counting requires a single
translation thread. Taking the clock away (ADR 0027, amended) put the suite
back on two threads for the first time in weeks, under a machine that was now
busy, and a run crashed inside the hour. Alone on an idle machine it still does
not reproduce: eight runs, no crash. Load is the difference, which is what a
race between translation threads looks like.

Measuring the two against each other, one thread is not slower. Across paired
runs it reached at least as far into the guest's own clock in the same wall
time, and in one case four times as far. That is not surprising for this
emulator: every peripheral it models is serviced over a single bus socket, so
a second translation thread mostly contends for the same lock.

## Decision

1. **Runs use one translation thread**, always, not only when the counted
   clock is asked for.

2. **The counted clock keeps its own flag** and now adds only the counting;
   the single thread it depends on is already there.

## Consequences

**Good**

- The crash class is gone from every run rather than from the ones that
  happened to ask for the counted clock.
- No measured cost, and on this workload sometimes a large gain.

**Costs, accepted**

- **This avoids the trigger; it does not fix the defect.** Something inside
  QEMU, or in the C this project adds to it, is not safe across two
  translation threads. That bug is still there, and a future change that turns
  the second thread back on would meet it again.
- A guest that would genuinely benefit from parallel translation cannot have
  it here. Nothing this project runs does: both firmwares spend their time
  waiting on peripherals this emulator serialises anyway.

## Alternatives rejected

**Find the race.** The honest first choice, and it needs a reproduction. It
does not reproduce on demand, it does not reproduce under a debugger, and it
took a busy machine and a fast suite to see it once. Turning off the thread
that causes it is what makes runs trustworthy today; the reproduction can be
gone after with a crash that is at least known to be waiting.

**Retry a crashed run in the harness.** It hides the defect precisely where it
would otherwise be measurable, and a test suite that silently repeats a crashed
boot is worth less than one that does not crash.
