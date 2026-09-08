# ADR 0040 — Stop a run when the evidence is in

**Status:** Accepted — 2026-09-08.

## Context

Every slow test booted a firmware and then waited out a fixed timeout,
whatever the guest had already done. The vendor board's tests ask for a
battery reading that arrives around thirty-eight seconds and an interface that
keeps drawing, and then sat watching an idle guest for another eighty. Across
the suite that was most of eighteen minutes.

The timeout was doing two jobs at once: bounding a run that goes wrong, and
deciding how long a run that goes right should last. Only the first is what a
timeout is for.

## Decision

1. **A test says what it came to see.** `run_emulator` takes `until(uart,
   frames)` and stops the run the moment it holds. A tap or a scripted
   interaction finishing says the same thing, so those stop too.

2. **The timeout goes back to being a ceiling.** With an early stop, a
   generous ceiling costs nothing on a good run and rescues a slow one, so the
   budgets rise rather than fall.

3. **A terminated runner takes its guest with it.** Python ends a process
   outright on a termination signal without unwinding, so the teardown that
   stops QEMU never ran and the emulator was orphaned. The runner now turns
   that signal into an exception so its existing teardown runs.

## Consequences

**Good**

- The suite runs in about ten minutes rather than eighteen, doing the same
  work and asserting the same things.
- Killing a run by hand no longer leaves an emulator eating a core, which was
  true before this and simply had not been noticed.

**Costs, accepted**

- A test that asserts something *did not* happen now watches for a shorter
  time. Those assertions were always bounded by the timeout, so this is a
  change of degree; the point at which a firmware has shown what the test
  describes is the honest place to stop, and a firmware that panics does it on
  the way there rather than while idling afterwards.
- Each `until` has to name every marker its module asserts, and a module that
  gains an assertion about something later must extend it or wait for the
  ceiling.

## What it cost to find

Stopping early made the last test in the suite fail, and the guest in it was
running at three per cent of real time. That was the orphaned emulators:
one per early-stopped run, each holding a core, starving whatever came next.
The suite had been slow enough before that nothing ever overlapped.

## Alternatives rejected

**Shorten the timeouts.** The same guess in the other direction, and it makes
a slow host fail rather than a fast one quick.

**Run the slow tests in parallel.** They are emulators competing for cores,
and the failure above is what that looks like.
