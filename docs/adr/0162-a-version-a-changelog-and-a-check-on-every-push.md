# ADR 0162 — BoardEmul has a version, a changelog and a check on every push

**Status:** Accepted — 2026-11-19.

## Context

BoardEmul has been one branch of commits, each tied to an ADR. For the
person developing it that is enough. For someone running it, three things
are missing:

- **Which BoardEmul is this?** Nothing says, in the page, in the launcher or
  in a run. A report of a problem cannot name what it was seen on.
- **What changed?** The ADR index says why each decision was taken, in
  order, and is a hundred and sixty rows long. It does not say what an
  operator can now do.
- **Is it still green?** The fast tests run on whatever machine their
  developer uses. On 2026-11-19 a clean clone in the build container, Linux,
  found one fast test failing every time: `test_radio_busy`'s
  `test_the_answer_carries_the_interrupt_line`, broken by ADR 0141 and
  skipped on Windows, where the suite had been run, because it needs Unix
  sockets.

## Decision

1. **A version, `emu/version.py`**: the first number moves when a device
   folder or the scripting API changes incompatibly, the second when an
   operator can do something new, the third for fixes. The page shows it
   beside its wordmark, `GET /api/version` answers it, the server prints it
   as it starts, and `python boardemul.py --version` says it. This is
   1.0.0.

2. **`CHANGELOG.md`** says, per version, what an operator can do that they
   could not, each line pointing at its ADRs.

3. **The fast tier runs on every push and pull request**, on Linux
   (`.github/workflows/tests.yml`): it needs neither QEMU nor the firmware,
   and the slow tier keeps skipping without them (ADR 0012). Before a push,
   the same check is a clean clone in the build container.

## Consequences

**Good**

- A run, a page and a report can say which BoardEmul they are.
- A test skipped on one platform still runs on the other before anything
  is shared.

**Costs, accepted**

- The version is moved by hand, with the changelog, when something worth
  telling an operator lands.
- The workflow does nothing until the repository has a GitHub remote; it
  is ready for when it does.
