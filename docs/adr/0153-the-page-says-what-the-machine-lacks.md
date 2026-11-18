# ADR 0153 — The page says what the machine lacks

**Status:** Accepted — 2026-11-18. Carries ADR 0147's doctor into the page.

## Context

`tools/doctor.py` (ADR 0147) says what a machine lacks to run a device:
Docker and its daemon and the build image where runs go into the container,
the QEMU binary, the pinned firmware. A person who opens the page and
presses Start without one of them meets the failure at the start instead:
Docker's own error, or a run that ends at once with QEMU's path in its
console (ADR 0140). Nothing on the page says, before that, what is missing
and what fixes it.

## Decision

1. **The server answers `GET /api/doctor`** with the doctor's findings, each
   its level, what it checked, what it found and the command that fixes it.
   The checks are the doctor's own, so the page and the command line cannot
   disagree.

2. **The page shows a banner when there is an error**, under the search
   field: each error on a line, with its fix, and a button that checks
   again. Warnings, an image not fetched or a probe not built, are not shown
   there: a run needs only the image it boots.

3. **It is read on load and after a start fails**, not polled: the checks
   run Docker commands, which take a moment.

## Consequences

**Good**

- The first thing a new machine shows is what it still needs, with the
  command that gives it.

**Costs, accepted**

- Loading the page runs the doctor's checks once, a second or two when
  Docker has to answer.
