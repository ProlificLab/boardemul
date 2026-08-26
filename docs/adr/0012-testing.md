# ADR 0012 — Two tiers of test: device models in milliseconds, pixels in minutes

**Status:** Accepted — 2026-08-26

## Context

Until now nothing verified this emulator except running it for a few minutes and
looking at a PNG. That is how the expensive bugs survived, and all three were
silent in the same way -- the bus traffic looked healthy and the result was
wrong:

- **A reply rebuilt per read.** One logical I2C read arrives as several calls.
  The touch report was regenerated each time, so the driver got byte 0 where
  byte 6 was expected, failed its magic check, discarded the position, and still
  wrote its acknowledgement. It survived several sessions with the evidence -- a
  two-to-one ratio of reads to acknowledgements -- visible in the output.
- **Undecoded registers returning zero.** Porting GPIO to C dropped the pin
  matrix's read-modify-write. It surfaced as a stack-canary trip in an unrelated
  task.
- **The same split-read bug again, one layer down.** Fixed in the device, it
  came back when the controller moved into C and split the read differently.

None of these would have been caught by a test that only asked "does it boot".
All of them would have been caught by asking a device model a question directly.

There is also a scale problem. There are now four C models and a growing Python
tier, and the ambition is more boards. The next refactor will break something
quietly.

## Decision

1. **Two tiers, separated by whether they need QEMU.**

   *Fast* tests instantiate a device model and ask it a question. No emulator, no
   firmware, no subprocess: 80 tests in 1.5 seconds. This is where regressions
   are actually caught.

   *Slow* tests boot the real vendor firmware under real QEMU and assert on the
   pixels it draws. Two boots, about five minutes. This is the only thing that
   proves the whole stack does its job.

2. **Slow tests are marked and excluded by default.** `pytest -m slow` runs
   them. A suite that takes five minutes will not be run often enough to help;
   one that takes 1.5 seconds will be run constantly.

3. **Every fast test corresponds to a bug that actually happened.** The split
   read is parametrised over every boundary. The keymap is checked byte for byte
   against the vendor's table, including the mirrored columns. An empty event
   FIFO must read zero, because the driver's init flushes with a loop that
   otherwise never terminates. This is a regression suite, not a coverage
   exercise.

4. **Golden frames exclude the status bar.** The clock and charge indicator
   change between refreshes, so a whole-frame hash is not a stable assertion.
   Hashing everything below it is.

5. **The interaction test asserts twice, at different strengths.** That the
   screen changed after a tap, and that it changed into the expected screen. The
   first survives cosmetic churn in the firmware's UI; the second is precise. A
   failure in only the second is a signal to look, not necessarily a defect.

6. **A PNG reader lives in the tests, not in the emulator.** The emulator only
   writes them. Putting a decoder in the product to serve the tests would be the
   tail wagging the dog; the round-trip test is the one place writer and reader
   check each other.

## Consequences

**Good**

- The three bug classes that cost days are now one-line failures.
- Device models can be refactored with confidence, which is the whole reason
  they live in Python.
- The golden frames are executable documentation of what the firmware draws.

**Costs, accepted**

- **Golden frames need regenerating when the panel model legitimately changes.**
  That is a manual judgement -- someone has to look at the new image and decide
  it is right. Automating it would defeat the purpose.
- The slow tests need a built QEMU and the vendor firmware, and skip cleanly
  without them. So a fresh clone runs 80 of 87 tests.
- Five minutes is long enough that these will be run at milestones, not on every
  edit. That is the intended trade, not an oversight.
- pytest is a dependency, which the project otherwise does not have. It was
  sanctioned as dev-only from the start and is installed from the distribution's
  package rather than pip, so the devcontainer stays reproducible.

## Alternatives rejected

**Only end-to-end tests.** Simplest to write and far too slow to run, and when
one fails it says "the picture is wrong" rather than which model lied.

**Only unit tests.** Every individual model can be correct while the assembled
thing draws nothing -- which was true of this project for several sessions.

**Hash the whole frame.** Brittle against a clock that ticks.

**Snapshot testing that regenerates goldens automatically.** Turns a failing
assertion into a silent accept, which is exactly the failure mode -- silent
wrongness -- these tests exist to catch.
