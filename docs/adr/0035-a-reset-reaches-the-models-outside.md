# ADR 0035 — A reset reaches the models outside

**Status:** Accepted — 2026-09-07.

## Context

Resetting this machine resets the peripherals inside it. The ones modelled in
Python are outside it, on the other end of a socket, and heard nothing: a guest
that rebooted met registers still holding whatever it had left there, so its
second boot began from a state its first never saw.

Nothing had gone visibly wrong, which is why this sat open for a while.
Meshtastic reboots in ordinary use, after its setup dialog is filled in, and
comes back correctly. But "no failure observed" is not the same as correct, and
the difference here is exactly the kind that produces a bug reproducible only
on the second run.

Not everything should be reset. A peripheral inside the chip is reset with the
chip. A part on the board with its own supply is not: a card in a slot, a touch
controller, a codec and a display all keep their state across a reset of the
processor beside them, and resetting them would model something that does not
happen. Only the radio is reset, by its own line, and the firmware drives that
itself.

## Decision

1. **The bridge tells the Python side when the machine resets.** A third
   operation alongside read and write, carrying no value and acknowledged by
   nobody, in the same fixed-size frame.

2. **It is registered by hand, not through the resettable phases.** The bridge
   is a plain device with no parent bus, so it is not in the tree those phases
   walk and the hook is never called. This cost a rebuild to discover and is
   the reason the registration looks old-fashioned.

3. **A region returns to what a first boot would find.** The base class defines
   the call and does nothing, so a window that holds no state needs no code;
   the ones that store registers clear them.

4. **Parts on the board are untouched.** They are not regions and never see
   this, which is the correct behaviour rather than an omission.

## Consequences

**Good**

- A guest's second boot starts from the same peripheral state as its first, so
  a reboot is reproducible rather than approximately right.
- The distinction between what is inside the chip and what is beside it is now
  stated in code rather than implied.

**Costs, accepted**

- A third operation on a protocol that had two, and a reset fires once per
  region rather than once for the machine. Each region resetting itself is
  simpler than electing one to reset the rest.

## Alternatives rejected

**Infer the reset from the guest's own output.** The reset banner arrives on
the UART well after the reset, and only some firmwares print one.

**Rebuild the models on reset instead of clearing them.** It loses the counters
runs are judged by, and a model's identity is referenced elsewhere.

**Reset everything, parts on the board included.** Simpler to describe and
wrong: a card does not forget its contents because the processor restarted.
