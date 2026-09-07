# ADR 0037 — What the air does to a packet

**Status:** Accepted — 2026-09-07. Amends ADR 0036's "nothing about
propagation is modelled".

## Context

ADR 0036 carried packets between boards and deliberately modelled nothing about
the medium: every board heard every packet, instantly and intact, whatever
channel it was tuned to and whatever else was transmitting. That was honest,
and it was also wrong in ways that matter to the firmware being tested. A mesh
protocol spends most of its effort on taking turns. Against an air where
nothing ever collides, no packet takes any time and a receiver hears traffic
from a channel it is not on, none of that effort is exercised, and a test of it
proves nothing.

The distinction that makes this tractable is between what can be derived and
what would have to be invented.

**Derivable.** How long a packet occupies the channel follows from the
modulation the firmware itself programs — spreading factor, bandwidth, coding
rate — and the packet's length, by the arithmetic in the part's datasheet.
Every input is read out of the radio's own registers. What follows from it is
equally derivable: a radio transmitting for that long cannot hear anything
during it, two packets overlapping in the air land on top of each other, and a
receiver tuned to another frequency is not listening to this one.

**Not derivable.** Range, terrain, antennas, orientation and interference. Two
processes on one machine have no distance between them. Any number for it would
be invented, and a plausible-looking number is worse than none because it
invites conclusions it cannot support.

## Decision

1. **Time on air is computed, not chosen.** From the datasheet's expression,
   over the settings the firmware programmed. Checked against published
   figures for the same settings rather than against what makes anything pass.

2. **A transmission lasts that long.** The packet leaves at once, but the
   channel is held and the flag saying the transmission finished waits. A host
   told instantly would key the radio again before the first packet had left.

3. **A radio is deaf while it transmits**, and two packets overlapping in the
   air are both lost. Both follow from the timing above rather than being
   separate rules.

4. **A frame carries where and when it was sent and for how long**, which is
   what a receiver needs to judge it. A receiver on another frequency discards
   it.

5. **Range is not modelled and not faked.** Everyone within the shared
   directory is in range of everyone else. That is stated here so it is not
   mistaken for a propagation model that happens to be generous. *Superseded by
   ADR 0038*: the distance is now supplied by the scenario and the link
   computed from it.

## Consequences

**Good**

- The firmware's own turn-taking is exercised: it listens before talking, waits
  out its own transmission, and can lose a packet to another.
- The numbers are checkable. A 92-byte packet at the preset Meshtastic uses
  takes 928 ms here, which is what it takes on a real radio.

**Costs, accepted**

- Timing runs on the host's clock while the guest may run slower than real
  time, most obviously under the counted clock (ADR 0027). A packet's airtime
  is therefore real seconds, not guest seconds, and on a slow guest the air is
  effectively faster than the firmware. Nothing yet depends on the difference.
- Range remains absent, so a test cannot express two nodes that should not hear
  each other. That needs a topology the user supplies, not physics this can
  derive.

## Alternatives rejected

**Model range and loss with plausible defaults.** Every number would be
invented, and the resulting behaviour would look like evidence.

**Leave the air perfect.** Simpler, and it silently passes a protocol whose
whole purpose is coping with an imperfect one.

**Deliver a packet at the end of its airtime rather than at its start.** Truer
to the radio, and it needs a scheduler in a model that is polled; the receiver
already knows when the packet ends, which is what collisions actually need.
