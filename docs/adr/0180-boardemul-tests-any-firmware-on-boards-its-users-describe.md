# ADR 0180 — BoardEmul tests any firmware, on boards its users will describe

**Status:** Accepted — 2026-11-23. Decided with the product's owner (the
product interview of that day, Q1-Q3, Q10-Q13, Q16-Q23). Reverses the
README's "not a general emulation framework".

## Context

BoardEmul was built as an emulator for one device, the T-Deck Max, with
the T-Deck beside it so that one board could not pass its accidents off as
design; the README said it was not a framework. Its owner wants it to be a
product others use: firmware developers first, then teaching, then
manufacturers. Asked how far that goes, the answers were: any firmware
image, not one project's (Meshtastic was the case it grew on); and boards
its users describe themselves.

## Decision

1. **The product is for testing firmware without the hardware**, in a
   developer's own machine and in CI. Its user is the person who writes
   that firmware; teaching and manufacturers come after, and win a conflict
   only where the first does not lose.

2. **Any image.** Nothing in the product is specific to one firmware
   project: what a test sees and drives is the board's (its serial ports,
   its screen, its inputs, its radio), not a firmware's protocol.

3. **Boards are described by their users**, in a later version: a *board
   description* names an ESP32-S3 and which parts of BoardEmul's catalogue
   are wired to which pins. New parts written by users and other chips are
   not part of it. A part the firmware talks to that the description does
   not wire answers as an empty bus would, and the run's report lists it.
   The T-Deck and T-Deck Max stay as they are until then.

4. **The first public version comes first**, with the two boards: licences
   (ADR 0179), the repository at `github.com/prolificlab/boardemul`, a
   prebuilt image on its registry, a `boardemul` package on PyPI beside the
   clone, and for tests:
   - a Python library -- a device's life (start, stop, restart), its serial
     ports (send, wait for), its screen (capture, wait for a region,
     compare), its inputs (touch, keys, text), and several devices on one
     LoRa air -- with a pytest fixture; declarative scenarios later, built
     on it;
   - `boardemul test`, which runs a build and a test command and answers
     with the test's exit status, and a GitHub Action that calls it.

5. **Words**: `GLOSSARY.md` holds Build, Library firmware, Device, Board,
   Board description, Part and Peripheral, as decided.

## Consequences

**Good**

- A firmware developer can test their own image in CI, whatever it runs.
- What has been built for two boards becomes a catalogue others wire.

**Costs, accepted**

- A board description is a framework's work, which this project avoided on
  purpose; the catalogue of parts limits it, and the two boards stay the
  reference that keeps the catalogue honest.
- WiFi and Bluetooth reach the test library after its first version.
