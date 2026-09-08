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

## What is known about it

The defect was then gone after directly, and while it is not fixed, it is no
longer a rumour.

**It reproduces.** `BOARDEMUL_TCG_THREADS=multi`, fourteen runs of the
Meshtastic boot at once, each with its own build directory: about one run in
fifteen dies. It needs the load; alone on an idle machine, twenty-eight runs
came up clean.

**It cannot be watched.** Under gdb it never fires -- the timing shifts and the
window closes -- and core dumps on this host go to a handler that cannot be
redirected. `BOARDEMUL_CATCH_SEGV=1` gets around that by having the process
report its own fault, which is how the first evidence in weeks was captured.

**The signature is a translation entry that has been zeroed.** The fault is
inside generated code, and the address it dies on is a *guest* address in
internal RAM -- 0x3fcb45eb in three of four captures, 0x3fcf14db in the other.
QEMU's fast path reaches host memory by adding an offset to the guest address,
so faulting on the guest address itself means that offset was zero: the entry
was read while it held nothing.

**Two things it is not.** Our own devices map their regions once, at device
creation, and never touch a mapping afterwards, so they are not moving memory
under a running CPU. And the fork's cache model writes guest RAM directly --
straight through the region pointer, both when it fills a page from flash and
when it stamps a discarded one -- without telling QEMU the memory changed,
which is a real hazard; declaring those writes with `memory_region_set_dirty`
changed the crash rate not at all (three faults in fifty-six against roughly
the same before), so that is not this.

What is left is the address-translation path itself, under two threads. That
is QEMU's own machinery rather than anything this project adds to it, and the
next step is upstream with the reproduction above.

## Alternatives rejected

**Find the race.** Attempted, above. It is now reproducible and characterised
but not fixed, and the single thread is what makes runs trustworthy meanwhile.

**Retry a crashed run in the harness.** It hides the defect precisely where it
would otherwise be measurable, and a test suite that silently repeats a crashed
boot is worth less than one that does not crash.
