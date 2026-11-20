# ADR 0173 — A Bluetooth frame carries its access address

**Status:** Accepted — 2026-11-20. Takes the part of ADR 0157's step 2 that
keeps transmissions apart; amends the frames of ADRs 0046, 0168 and 0171.

## Context

Everything on a Bluetooth air (ADR 0045) reaches every radio on it. With one
board and one central that was invisible. With two boards on one named air
(ADR 0145) it is not:

- a central's connection request reached both boards, and both would take
  it: the controller does not check whom a request names. ADR 0169's
  central sent a placeholder for the advertiser's address and was accepted.
  On silicon the radio filters that before the controller sees it;
- an advertising board read a connection's packets as advertising ones. A
  data packet's first octet carries its LLID and sequence bits where an
  advertising packet carries its type, so a central's control packet with
  both bits clear (`0x03`) would read as SCAN_REQ.

On the air a radio tells these apart by the access address before anything
else: every packet starts with one, advertising packets with `0x8E89BED6`,
each connection's with the one its connection request chose, and a receiver
correlates only on the one it expects. The core already keeps it where the
controller wrote it: control structure +12 (ADR 0168's census), on link 0
the advertising one (`d6be 898e` in the dump ADR 0171 read).

Channels are the other half of step 2, and are left: all of a run's
Bluetooth is on one channel, with no collisions modelled, so a channel on
the frame would select nothing until there is more than one connection per
access address, which there never is.

## Decision

1. **A frame on a Bluetooth air starts with its access address**, four
   octets, least significant first, then what it carried before: the header's
   first octet and the payload (ADR 0168), an advertisement's type, address
   and data (ADR 0171).
2. **The core sends each event's own**: control structure +12 of the link the
   event is for, advertising or a connection.
3. **The core hears only its own.** A frame arriving in an event whose
   access address is not the event's link's is not received, as a radio
   correlating on another address does not see it.
4. **On its advertising events the core takes a scan or connection request
   only if it names this board**: the request's AdvA (its payload's octets
   6 to 11) is control structure 0's +6..+11 and its RxAdd bit is the
   advertising descriptor's TxAdd, as the radio's own address filter has it.
5. **The central does the same**: it sends a connection request on the
   advertising address and its connection's packets on the address that
   request chose, and takes only those, and advertisements, from the air.

## Consequences

**Good**

- Two boards on one air: a connection request names its advertiser (ADR
  0171) and only that board takes it; a connection's packets reach only the
  board in it, and no advertising board takes a central's control packet
  for a scan request.
- A capture of the air reads as a sniffer's does: address, header, payload.

**Costs, accepted**

- Every Bluetooth air user changes with the format: the central, the tests
  that listen, and the investigation scripts in `logs/`.
- White lists and resolvable private addresses are not filtered on: a
  board advertising on a random address it resolves would need them.
