# ADR 0045 — Bluetooth gets an air of its own

**Status:** Accepted — 2026-09-08.

## Context

ADR 0044 ended with the packet in hand and nothing to carry it. The link layer
schedules thousands of advertising events a run, and the payload is reachable
from the record the controller writes, through the mapping the controller
itself programmed.

What was left was the medium, and LoRa already has one: a directory in which
every radio leaves a file naming the port it listens on, and transmitting means
handing the frame to everyone else listed (ADR 0036).

Two questions had to be answered before reusing it.

**Whose air?** A Bluetooth radio and a LoRa radio share no band, and a mesh
node must not hear an advertising packet as if it were a LoRa frame. One
medium with a tag in the frame would work and would put the burden on every
reader to filter correctly.

**Spoken from where?** Every other air participant is a Python model. The only
thing that knows a Bluetooth packet exists is the link-layer core, which is C:
it holds the base registers that say where the controller mapped the payload,
and nothing on the Python side can resolve an offset without them.

## Decision

1. **A separate directory, given by `--ble-air`.** Different band, different
   air. Nothing filters, because nothing that cannot hear Bluetooth is in the
   room.

2. **The core speaks the medium itself, in C.** It binds a socket, leaves its
   port in the directory, and sends to the others — the same protocol ADR 0036
   defines, so a Python `AirMedium` can join and listen without knowing which
   kind of radio it is hearing.

3. **Only what the controller points at goes out.** On an event, the record at
   the advertising offset is read, each offset in it resolved through the base
   registers, and each block walked as advertising data — a length, a type, and
   that many bytes — so the payload's extent comes from the payload rather than
   from a constant. Blocks that do not walk cleanly are not sent.

4. **The record is not sent as its own payload.** It holds its own offset among
   the others, and following that puts the record on the air dressed as
   advertising data. It is skipped, as are repeats of a block referenced twice.

## Consequences

**Good**

- A board advertising can be heard: forty-two bytes carrying flags, a sixteen-
  bit service UUID, a hundred-and-twenty-eight-bit one and the complete local
  name, every byte accounted for by a structure that declares its own length.
- Nothing here invents a packet. Every byte comes from memory the controller
  staged and pointed at.

**Costs, accepted**

- **This is advertising, not Bluetooth.** No scanning, no connections, no
  channel hopping, no timing beyond the event that triggers a send. A second
  emulated board cannot yet answer one.
- The device now does network I/O from a device model. It is the same choice
  the bus bridge and the GPIO block already make, and it is confined to a
  directory a scenario has to ask for.
- The advertising record's offset is a constant read off a running controller
  rather than a documented one. If a firmware puts its advertising set
  somewhere else, this finds nothing and sends nothing, which is the right
  failure but an opaque one.

## Alternatives rejected

**One air for both radios, with a kind byte.** Fewer moving parts, and it makes
every listener responsible for ignoring what it cannot hear. Two bands are two
media; the directory is cheap.

**Emit from Python.** It would keep device models free of sockets, and the
Python side cannot resolve an exchange-memory offset: the registers that say
where the controller mapped the payload live in the core.

**Send a whole advertising PDU, header and address included.** More faithful in
shape, and the header and address would have to be synthesised here — inventing
the parts nobody staged, which is what ADR 0044 exists to refuse.
