# ADR 0034 — A radio that answers

**Status:** Accepted — 2026-09-07. The chip-identification step is unresolved;
see "Where this stands".

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

## Where this stands

The firmwares still do not accept the part, and the reason is not yet known.
What has been ruled out, by reading the bytes on the wire:

- The reply reaches the host. The exchange is duplex and the twenty bytes come
  back as `24 24 24 24 53 58 31 32 36 32 00...`: four status bytes, then
  `SX1262`, which is the string and the offset the datasheet's framing gives.
- The status byte is valid. `0x24` is standby with no error, and it is neither
  of the two values a host reads as an absent chip.
- The alternative offset was tried, with the data one byte earlier, and the
  firmware refused that too.

So the part answers correctly and is still rejected, which points at something
outside the register read: the reset line, the busy line, or a step of the
host's sequence not yet identified. It is left here, recorded, rather than
guessed at further.

## Consequences

**Good**

- The radio's conversation is visible and modelled, and the model is built
  entirely from bytes a firmware actually sent.
- The status a host checks is now a real one rather than the empty-bus value
  that told it the part was missing.

**Costs, accepted**

- The goal was for the firmware to find its radio, and it does not yet. The
  model is a step towards that, not the end of it.
- Nothing is on the air. Two emulated boards cannot hear each other, which is
  the thing a mesh device exists to do, and is a separate piece of work.

## Alternatives rejected

**Leave the select unmodelled.** It reads as an absent chip, which is what
both firmwares concluded, and it produces no evidence to work from.

**Return whatever makes the firmware proceed.** Without knowing why it refuses,
any such value is a guess dressed as a model, and the next firmware would meet
the same wall with the evidence now hidden.
