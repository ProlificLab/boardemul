# ADR 0050 — What a connection event is told

**Status:** Accepted — 2026-09-09. Names the next wall; does not take it down.

## Context

ADR 0049 got a connection serviced. The next thing to want is data over it, and
the obvious first move -- speak to the link and see whether it survives -- was
tried and did not work, which is worth recording because the reason is
structural rather than a detail.

An empty data packet was put on the air every ten milliseconds for twenty
seconds, nineteen hundred of them. Every connection event delivered one: the
core wrote it into the descriptor the link layer names next, exactly as it does
for a connection request. The link still ended after five events, and
`r_lld_con_rx` never ran once.

The descriptor was never consumed -- every delivery went into descriptor one,
because the index only moves when the ROM takes a packet out. So the packets
were being written somewhere the connection does not look.

**Where it does look.** `r_lld_con_frm_isr` runs once per connection event, and
before it will consider a received packet it reads a **per-connection control
block in exchange memory**: page `0x400`, ninety bytes per link, at
`0x400 + link * 90`. It reads the halfword at `+86`, whose high byte must be
zero, and the halfword at `+24`, whose top two bits must be zero, and only then
goes on. `r_lld_con_rx` reads `+80` of the same block. Nothing in this model
writes any of it.

So the shape of the answer is now clear, and it is not the one advertising has.
An advertising event's received packet is found by walking the descriptor ring;
a connection event's is announced in a structure the hardware maintains for
that link, and the ring is only where the bytes are. Modelling one without the
other delivers packets nobody is looking for.

While reading that path, one thing the model had plainly wrong was fixed. The
descriptor's `+4` is not the packet's type: it is the packet's own **two header
bytes**, the first saying what kind of packet it is -- an advertising type, or
on a connection the LLID and the sequence bits -- and the second how many bytes
follow. `r_lld_con_rx` takes the length out of that high byte and refuses a
zero LLID. The model wrote only the low nibble, so a connection's packets could
not have been read from it whatever else were right.

## Decision

1. **Write the whole header.** The two bytes are the packet's, so they come off
   the frame rather than being composed: the first byte as it arrived, and the
   length that actually follows it.

2. **Stop before the control block.** It is a new structure with at least three
   fields that matter and no reason to think that is all of them. Guessing at
   the rest to make one test pass would put invented state where the ROM
   expects the hardware's, which is the failure mode ADR 0048 already cost two
   documents to.

3. **Record the measurement that would have looked like progress.** Nineteen
   hundred packets delivered into a descriptor nobody reads is exactly what a
   working data path would look like from the model's side. Only the ROM's
   behaviour says otherwise, which is why it is the thing to check.

## Consequences

**Good**

- The next piece of work is named and bounded: the connection's control block
  in exchange memory, its layout read off `r_lld_con_frm_isr` and
  `r_lld_con_rx` the way every other structure here has been.
- A real defect in the receive descriptor is fixed on the way, and the
  advertising path is unaffected -- it reads the low four bits and always did.

**Costs, accepted**

- **Still nothing crosses the link.** A connection lives about six intervals
  and ends because the peripheral hears nothing, which is what the
  specification gives it.
- The length now written for an advertising packet is the frame's, and no
  advertising handler in use reads it -- a connection request's size is fixed
  at thirty-four. It is right rather than exercised.
- The control block's offsets recorded here are the three that were seen. They
  are a starting point for the next attempt, not a layout.

## Alternatives rejected

**Fill the control block with plausible values until `con_rx` runs.** It might
work and would teach nothing about why; a field that happened to satisfy one
check would be carried forward as though it had been read.

**Have the core answer connection events itself.** The same objection as in ADR
0047: the peer belongs on the air, where something other than this emulator
could stand in its place.
