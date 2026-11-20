# ADR 0046 — The controller can be spoken to

**Status:** Accepted — 2026-09-08. A step toward a connectable peer, not one.
Amended by ADR 0171: an advertising frame carries the advertiser's address
between its type octet and its data, as an ADV_IND PDU does.

## Context

ADR 0045 got a board's advertising onto an air and recorded why two boards
cannot see each other over it: only Meshtastic has a Bluetooth stack, and
Meshtastic never scans. Its peer is a central connecting *to* it, which needs
the controller to be able to receive.

The ROM says what receiving looks like, and none of this came from a document:

- Receive descriptors live at exchange-memory offset `0x1000`, ten of them,
  twenty bytes apart, taken round-robin (`r_lld_rxdesc_check`).
- The one the advertising path reads next is named by a byte at offset 216 of
  the link-layer environment, whose pointer is at `0x3fceff78`
  (`r_lld_adv_pkt_rx`).
- In a descriptor, halfword `+0` bit 15 says a packet is there, `+2` must be
  clear of `0x403d` for it to count as good, and the low four bits of `+4` are
  the packet type. Type five is a connection request, and the ROM dispatches
  it to `r_lld_adv_pkt_rx_connect_ind`; type three is a scan request.

## Decision

1. **The core takes frames off the air as an event runs**, which is when the
   controller would have been listening.

2. **A frame is its packet type and then its payload.** Transmitting prepends
   the type; receiving reads it. A receiver that had to infer the type from
   the bytes would be guessing, and the first thing the ROM asks about a
   received packet is what kind it is.

3. **Delivery fills the descriptor the ROM is about to read**: the type from
   the frame, a clear status, and the flag that says a packet arrived.

## Consequences

**Good**

- A packet sent from outside reaches the firmware's link layer. Injecting a
  connection request, the ROM reads the descriptor, sees type five, and takes
  the branch to its connect handler -- traced, not inferred.
- Advertising still works and is still tested, now with the type byte in front.

**Costs, accepted**

- **This is not a connection.** `r_lld_con_start` does not run, and should not:
  the connection request's payload -- access address, CRC seed, window, hop --
  is not yet placed anywhere the handler reads, so it rejects what it is given.
  Getting from here to a connection means finding where the payload belongs and
  forming a real one.
- The environment pointer is a ROM data address written into this model. It is
  stable for this ROM and wrong for any other, and there is no way to find the
  next descriptor without it.
- Nothing checks that a frame was meant for this board: there are no addresses
  in the model yet, so everything on the air is delivered to everyone.

## Alternatives rejected

**Deliver into whichever descriptor is free.** The ROM does not scan the ring;
it reads the one its index names. Filling any other is filling nothing.

**Assume every frame is a connection request.** What the scaffold did before
the type byte, and it made the medium lie about its contents the moment
anything else was sent.
