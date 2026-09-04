# ADR 0029 — The touch interrupt line follows the mode the firmware wrote

**Status:** Accepted — 2026-09-04.

## Context

The GT911 has an interrupt output whose behaviour is set by two bits of its
configuration block (register 0x804D, "INT trigger": rising edge, falling
edge, low level, high level). The model learned its line from the vendor
firmware's wire (ADR 0006): that firmware's driver, SensorLib, writes mode 0
there and decides `isPressed()` by reading the line and comparing it with
HIGH. So the board drove the line high while a finger was down and low
otherwise, and the vendor's interface took taps.

Meshtastic reads the same part through LovyanGFX, which never writes 0x804D
and reads the status register only while the line is *low*; while it is high
the driver skips the read and returns what it had. Under the vendor's
convention every press made the line high, so Meshtastic never read a point:
2,547 idle polls, none during a press, in run after run. Typing into its
setup dialog had to be done from the keyboard alone.

What the real part does on that pin, in mode 0, between two report periods,
is not known here: a pulse whose width and rest level would reconcile the two
drivers, or a level held until the host clears the status. Without a scope on
a real board, any single pulse model would be a guess, and one of the two
drivers would misread it.

## Decision

1. **The model records the trigger mode.** A write covering 0x804D sets
   `int_mode` to its low two bits; until then the mode is unknown.

2. **The line's level is the model's to state, from the mode and the finger.**
   `GT911.int_level`:
   - mode unknown: low, always. The only driver seen that writes no mode
     reads only while the line is low, and the module's own default is not
     known;
   - mode 0 (rising edge) or 3 (high level): high while a point is present,
     low otherwise, which is the vendor's convention as before;
   - mode 1 (falling edge) or 2 (low level): low while a point is present,
     high otherwise, by the same driver's table for those modes.

3. **The board drives what the model states**, after every touch event and
   on its input tick, and nothing else decides the line.

## Consequences

**Good**

- Both firmwares seen so far take touches: the vendor's through the level it
  asked for, Meshtastic's through the status register it polls with the line
  at rest.
- The rule is stated in one place and read off the wire: a firmware's own
  configuration write selects its convention.

**Costs, accepted**

- This is not a model of the part's pulse. It is the union of two drivers'
  expectations, keyed on a register only one of them writes. A third driver
  with a third expectation would show it up, and the honest fix then is a
  measurement on hardware, not a fourth convention.
- Modes 1 and 2 have not been seen on any wire. Their levels follow
  SensorLib's reading of those modes, which is knowledge of a driver, not of
  the chip; they are here so a write of those values does something stated
  rather than something stale.

## Alternatives rejected

**A pulse per report period.** Closest to what a datasheet describes, and the
two drivers' expectations of it are contradictory under any width this record
could justify: a level-polling driver wants it long, a skip-while-high driver
wants it short. Rejected until measured.

**Keep the vendor's convention and drive Meshtastic from the keyboard.** It
answered the keyboard question, and left half the board's input dead for the
firmware the board is set against.

**Key the convention on which firmware is running.** The model would have to
know the firmware's name; the register write says the same thing without it.
