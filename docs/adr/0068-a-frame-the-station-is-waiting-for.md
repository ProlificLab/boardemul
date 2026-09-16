# ADR 0068 — A frame the station is waiting for

**Status:** Accepted — 2026-09-16. Completes what ADR 0067 started, and makes
explicit an approximation ADR 0064 took without naming it.

## Context

ADR 0067 got the handshake's first message asked for. It is still not
delivered: offered three hundred and three times in one run, put on the air
**zero** times. Every one was dropped by the receive gate.

Which half of that gate, counted apart for this purpose:

| gate | stalls |
| --- | --- |
| the receiver switched off | 14336 |
| the MAC held | 771 |

So it is overwhelmingly the receive-enable bit at `+0x0d8`, not the MAC hold.
That distinction is the whole decision here, because the two mean opposite
things. A held MAC is a radio retuned to another channel: a frame put on the
air into that is a frame that could not have arrived, and delivering it would
be a lie about the hardware. A receiver switched off is the case **ADR 0064
already delivers into anyway**, for answers, and it had to:

> An answer skipping the receive gate is what makes it prompt, and a station
> turns its receiver off for about a hundred milliseconds right after it
> authenticates, so an answer that waits for the gate misses the window and no
> association follows.

That page measured it -- thirty-three silent scans in thirty-five with the
exemption off -- and kept it. So the exemption exists, it is load-bearing, and
the handshake's first message simply has no way to claim it. The device cannot
tell a beacon from a handshake message, and ADR 0064 is explicit that it must
never learn to.

**And the blanket version does not work.** ADR 0064 measured that too: exempting
everything starves beacons, because the receiver is off three quarters of the
time and an exempt beacon takes the delivery a real frame needed. Ten silent
scans in fourteen. So this cannot be "exempt every frame the peer offers".

What is missing is a way for the peer to say which frames are awaited, without
the device learning what any of them are.

## Decision

1. **The reply carries flags.** The frame channel's reply grows a field:

       reply     length:u16  flags:u16  frame[length]

   Bit zero says *a station is waiting for this frame*. Nothing else is
   defined; the remaining bits are zero and are room for the next thing.

2. **An awaited frame is exempt from the receive-enable gate, and only that
   one.** A held MAC still stops it. The peer is allowed to say a frame
   matters; it is not allowed to claim the radio is tuned somewhere it is not.

3. **The peer sets it on what a station is actually waiting for** -- the
   handshake's messages -- and never on a beacon. A beacon is a broadcast
   nobody is waiting for, and another follows in twenty milliseconds.

4. **The device still learns nothing about protocol.** It learns that a frame
   is awaited, which is a property of the air, not of EAPOL. It cannot tell
   which frame this is and has no reason to.

## Consequences

**Good**

- The handshake's first message can reach a station that has its receiver
  momentarily off, which is the only state it is ever offered in.

- **Every later awaited frame is covered by the same flag.** Message three, a
  group rekey, an ARP reply, a DHCP offer: each is a frame a station is waiting
  for, and none needs its own arrangement.

- The blanket exemption stays rejected, so beacons keep their share of the
  deliveries and the scan keeps working.

**Costs, accepted**

- **This is an approximation, and it is worth saying so plainly.** If the
  station's receiver is really off, the frame did not really arrive. The model
  delivers it anyway because the alternative is measured not to work, and
  because the station evidently does collect it -- which is itself the
  interesting part, below.

- **It says our reading of the receive-enable bit is probably wrong.** If
  delivering into a closed receiver is what makes the driver work, then either
  bit eight of `+0x0d8` does not mean what this project takes it to mean, or
  the window it describes is narrower than the bit suggests, or the driver
  re-enables the receiver between our sample and the delivery. That is the real
  question underneath both this page and ADR 0064's exemption, and neither
  page answers it. What this one adds is that the approximation is now
  *declared* -- a flag with a name -- instead of a side effect of being an
  answer.

- **An asymmetry, kept deliberately.** An answer is exempt from both halves of
  the gate; an awaited frame is exempt from one. That is inconsistent, and the
  reason is that the answer path is measured and working (ADR 0061, ADR 0063)
  and this page will not disturb it on a tidiness argument. Narrowing the
  answer exemption to match is a change with its own measurement, and it
  belongs to whoever answers the question above.

- **The reply format changed.** Two bytes became four, on a protocol with one
  implementation at each end, both in this repository and versioned together
  (ADR 0064). Nothing outside `emu/devices/wifi_ap.py` touched the peer's
  return values, so the cost was confined to the two files that speak it.

## Alternatives rejected

**Let the device look at the frame.** One `if` on the subtype -- a beacon is
0x80, anything else is directed -- and no protocol change at all. It is
tempting and it is the thin end of exactly the wedge ADR 0064 drove out of the
C tier: the next frame that needs different treatment is a data subtype, and
then the device is parsing 802.11 to decide scheduling.

**Exempt anything the peer offers on a tick.** Measured by ADR 0064 and it
starves beacons; ten of fourteen scans heard nothing.

**Make the peer wait for the receiver instead.** It cannot see the gate, and
telling it would put a register in the protocol -- the one thing the frame
channel exists to avoid. It also would not help: the peer would be waiting for
a bit that, per the cost above, may not mean what it appears to.
