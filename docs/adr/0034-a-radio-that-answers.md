# ADR 0034 — A radio that answers

**Status:** Accepted — 2026-09-07.

## Context

With the bus routing by chip select (ADR 0032), the radio's select is
reachable and its traffic recorded for the first time. Both firmwares do the
same thing on it, over and over: a byte of nothing, `80 00` which is
SetStandby, and a twenty-byte `1D 03 20 00 ...` which is a read of sixteen
bytes from register 0x0320 — the string a host reads to find out what part it
is talking to. Twenty rounds of that and the vendor build gives up with
`Start Radio failed,code:-2`.

Nothing was answering. A chip select with no model on it reads back `0xFF`,
and RadioLib maps both `0xFF` and `0x00` to "no chip found" explicitly, so the
firmware's conclusion was correct: as far as it could tell, there was no radio.

## Decision

1. **The radio answers the command set it is sent.** An SX126x is spoken to as
   an opcode with parameters, framed by the chip select, and every byte the
   host clocks comes back with a status: which mode the part is in and how the
   last command went. That status is what a host checks constantly.

2. **It is a register file and a buffer, not a transmitter.** Writes are kept
   and read back, the version string sits where a host looks for it, and the
   commands that configure a radio are accepted. Nothing is transmitted and
   nothing is received.

3. **A lone node hears nothing, and that is modelled honestly.** Signal
   strengths read at the floor and no reception is ever signalled. Setting
   transmit raises the flag that says the transmission finished, because a host
   that sends waits for it and blocks for its whole timeout otherwise.

4. **Nothing is invented to force a firmware forward.** Where a firmware is
   still unsatisfied, that is recorded rather than papered over with a value
   chosen to make it proceed.

## What it says it is

The part refused every firmware until it stopped calling itself an SX1262.

Getting there took ruling things out in order. The reply does reach the host,
and a trace of what the guest reads back from the controller proved it: the
version bytes arrive, ten times over, spelling the name exactly as intended.
The status byte is valid and is acted on -- replacing it with the value a host
reads as an absent chip changes the firmware's behaviour enormously. The offset
is right, and RadioLib's own source confirms both the twenty-byte exchange seen
on the wire and where in it the contents begin.

So the bytes were correct and correctly delivered, and were still rejected,
which left only what they said. The family has several members. Answering
`SX1261` instead, both firmwares accept the part immediately: the vendor build
prints `Start Radio success!` where it printed a failure, and Meshtastic stops
retrying and configures the radio -- packet type, frequency, modulation
parameters, calibration, and into receive.

That is the name in the model, and it is evidence rather than preference
(ADR 0006). It is not the name on the module, which is why it took so long to
look at; the identity a part reports and the part number printed on it are not
obliged to agree, and here they do not.

## Consequences

**Good**

- Both firmwares find their radio and configure it. The vendor build says so
  in as many words, and Meshtastic's whole start-up sequence runs.
- The radio's conversation is visible and modelled, and the model is built
  entirely from bytes a firmware actually sent.

**Costs, accepted**

- The identity is what two firmwares accept, not something read off silicon. A
  third that wants a different name in that register would need this revisited,
  and the register is the place to revisit.
- Nothing is on the air. Two emulated boards cannot hear each other, which is
  the thing a mesh device exists to do, and is a separate piece of work. The
  firmware now gets as far as listening, and hears nothing.

## Alternatives rejected

**Leave the select unmodelled.** It reads as an absent chip, which is what
both firmwares concluded, and it produces no evidence to work from.

**Return whatever makes the firmware proceed.** Without knowing why it refuses,
any such value is a guess dressed as a model, and the next firmware would meet
the same wall with the evidence now hidden.
