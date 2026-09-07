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

Then two experiments narrowed it to something specific and strange. Replacing
the status byte with `0x00`, which a host reads as an absent chip, changes the
firmware's behaviour enormously: it retries sixteen times as much. So the byte
at index one plainly reaches the host and is acted on. But replacing the
version string with a name no host wants changes nothing at all -- the same
number of reads, the same outcome -- and moving the contents one byte earlier
or later changes nothing either.

That is the shape of the problem. The status byte arrives and matters; the
bytes the host copies out as the version do not appear to influence it. RadioLib
reads those from a fixed offset after the command and the status, and its own
source confirms both the twenty-byte length we see and that offset. So the
question is not what this model returns but whether the read phase of that
transaction reaches the host at all, which is a question about the controller
and the bus rather than about the radio.

It is left here, recorded with the measurements, rather than guessed at
further. Note that these counts vary between runs under host load, so the
figures above are the shape of the difference and not a benchmark.

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
