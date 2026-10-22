# ADR 0136 — A teardown that hangs leaves its own autopsy

**Status:** Accepted — 2026-10-20.

## Context

A run's shutdown is a cascade of bounded stops — the bus server, the input
tail, the interrupt channel, the board's links — each with a join timeout of
a few seconds. Bounded individually is not bounded together: on one host,
node two of the mesh test spent more than the test's fifteen seconds of
patience somewhere in that cascade and was killed outright. No manifest, no
traceback (the suite's run output goes to the void), sockets left behind —
a wall recorded as a flake.

Whatever blocks, the run has already lost: QEMU is gone, the models are
stopped. What it should not also lose is the answer to *where*.

## Decision

**The shutdown is armed with a watchdog** (`faulthandler.dump_traceback_later`):
if teardown has not finished within `TEARDOWN_WATCHDOG_SECONDS` (twelve),
every thread's stack is dumped into the run's output directory as
`teardown.log`, and the process exits — a hung teardown forfeits the
manifest and leaves the autopsy instead. The watchdog is cancelled the
moment teardown completes; a normal stop never knows it was there.

## Consequences

**Good**

- The next teardown stall names its line — on any host, with no debugger
  attached — instead of costing an afternoon of forensics.
- A hung run cannot linger: the exit is unconditional.

**Costs, accepted**

- A teardown that trips the watchdog also skips the manifest: past fifteen
  seconds the choice between a record of the run and a record of the hang
  has already been made, and this decision picks the hang.
- Twelve seconds is shorter than some of the suite's own teardown patience;
  a watchdog firing during a merely slow stop is a false positive — and its
  stacks will say so.
