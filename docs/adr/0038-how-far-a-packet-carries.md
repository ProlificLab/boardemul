# ADR 0038 — How far a packet carries

**Status:** Accepted — 2026-09-07. Completes what ADR 0037 left out.

## Context

ADR 0037 modelled what could be derived from the radio's own settings — how
long a packet holds the channel, and the collisions and deafness that follow —
and deliberately left range alone, on the grounds that two processes on one
machine have no distance between them and any number for it would be invented.

That reasoning was half right. The *distance* is indeed not discoverable: it is
a property of the situation being tested, not of the machine. But once someone
says how far apart two boards are, everything after that is arithmetic, and
arithmetic that can be checked. The free-space loss between two points follows
from the distance and the frequency. What a receiver can still decode follows
from the channel's noise floor and how far under it each spreading factor
reaches. Both are standard expressions, and the second reproduces the
sensitivity figures the part's own datasheet quotes, to within a decibel.

So range divides cleanly: the distance is supplied, and the link is computed.
Nothing has to be invented at all.

Leaving it out had a cost beyond incompleteness. The strengths reported to the
firmware were fixed values at the floor, and a mesh firmware reads those to
decide how to route. Constants dressed as measurements are worse than a link
budget, because the firmware acts on them.

## Decision

1. **Where a board stands is given, not guessed.** `--at X,Y` in metres.
   Boards told nothing stand at the same point, which is every run so far and
   is why they all hear each other.

2. **The link is computed.** Free-space loss over the distance and the
   frequency; a packet is decoded when it arrives above the sensitivity for the
   spreading factor and bandwidth in use. Transmit power is what the firmware
   programmed.

3. **The strengths the firmware reads are the computed ones.** Signal strength
   and signal-to-noise come from the link the packet actually crossed, in the
   units the part reports them in, rather than a floor constant.

4. **The frequency register is converted before it is used as a frequency.**
   The part counts in steps of its crystal, and a propagation sum given the raw
   value would be quietly using the wrong number.

5. **Free space and nothing else.** No terrain, no obstacles, no antenna
   patterns, no fading. Those need a description of a place, which is a
   different kind of input from a distance. *Extended by ADR 0039*: the effect
   of a place is now given as an environment exponent, and antennas and
   obstruction are stated per scenario. Fading is still absent, deliberately.

## Consequences

**Good**

- A scenario can put two nodes far enough apart that they cannot hear each
  other, which is the thing a mesh exists to route around and could not be
  expressed before.
- The numbers are checkable rather than plausible: the sensitivity derivation
  lands on the datasheet's own figures, and ten times the distance costs twenty
  decibels as it must.

**Costs, accepted**

- Free space is the most generous case there is. Two nodes a hundred kilometres
  apart hear each other here, which is correct for line of sight and unlike
  most real deployments. A scenario wanting a realistic town needs terrain this
  does not model.
- Antenna gain is zero at both ends, stated rather than tuned.

## Alternatives rejected

**Pick a range and have packets stop at it.** One invented number instead of a
computed link, and it would not vary with spreading factor or power, which are
the settings a mesh firmware actually trades off.

**Model terrain and fading.** The next honest step, and it needs a description
of a place plus a propagation model chosen from several that disagree. Not
something to guess at inside a board emulator.

**Keep reporting floor constants for signal strength.** The firmware routes on
those numbers, so leaving them fixed is not neutral.
