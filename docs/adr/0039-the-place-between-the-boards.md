# ADR 0039 — The place between the boards

**Status:** Accepted — 2026-09-07. Extends ADR 0038.

## Context

ADR 0038 computed the link between two boards over free space, and said that
terrain, obstruction and antennas were left out because they need a description
of a place rather than a distance.

That is true of terrain in the literal sense — a map — but it is not true of
the effect terrain has, which radio engineering has a standard way of
expressing. The log-distance model says loss grows with distance raised to an
exponent, and that exponent is what distinguishes open ground from a suburb
from the inside of a building. The values for each are quoted in the standard
texts as ranges. Choosing to use that model is not inventing anything; it is
using the model the field uses, with the environment supplied by whoever is
describing the scenario, exactly as the distance already is.

Antenna gain is simpler still. It is a number the person setting up the
scenario knows about their hardware, and it enters the budget by addition.

What genuinely cannot be derived is which of those an unattended emulator is
in. So it is asked for, and the default is the case that needs no choosing.

## Decision

1. **The place is named or given.** `--environment` takes free-space, open,
   suburban, urban or indoor, or an exponent directly. The named values are
   representative figures from the published ranges, and saying so is why they
   are named rather than presented as measurements.

2. **Free space remains the default**, and the model reduces to it exactly at
   an exponent of two. Nothing about any existing run changes.

3. **Antennas are stated per board** with `--antenna`, in dBi, and count at
   both ends of the link. A frame carries the transmitting board's, because
   the receiver cannot know it otherwise.

4. **Obstruction is stated, not drawn.** `--obstruction` adds decibels for
   whatever is in the way. It is deliberately not a random variable: a run that
   cannot be repeated is worth less than one that can (ADR 0004), and a
   scenario that wants a hard case can say so exactly.

## Consequences

**Good**

- A scenario can put two boards a mile apart in a town and have them fail to
  reach each other, then move them to open ground and have them succeed, which
  is the situation a mesh exists for.
- Every number is either the scenario's or the standard model's, and the
  arithmetic is checkable: ten times the distance costs ten times the exponent
  in decibels, exactly.

**Costs, accepted**

- The named environments are single values picked from published ranges. They
  are a starting point for a scenario, not a claim about any particular town,
  and an exponent can be given instead.
- Fading is absent. Real links vary second to second, and modelling that means
  a random process, which would cost reproducibility. Stated obstruction is
  offered instead, which expresses the same worst case deterministically.

## Alternatives rejected

**A terrain map and a diffraction model.** The honest version of "terrain", and
it needs a landscape, a chosen diffraction method and a great deal of
arithmetic whose inputs an emulator has no way to check. The exponent captures
what a mesh firmware actually experiences.

**Log-normal shadowing drawn per packet.** What the textbooks pair with the
exponent, and it makes two runs of one scenario differ. This project spent real
effort making runs repeatable; a stated figure gives the same reach without
that cost.

**Leave it at free space.** Simplest, and it means every scenario is the most
generous case there is, which is the one a mesh protocol never has to work in.
