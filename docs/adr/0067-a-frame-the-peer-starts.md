# ADR 0067 — A frame the peer starts, and a radio that loses it

**Status:** Accepted — 2026-09-16. Follows ADR 0064, and fixes the wall ADR
0064 left standing once ADR 0066 removed the other one.

## Context

ADR 0066 made `thread=multi` usable, which ADR 0064 said was the way through to
the WPA2 handshake. It is, as far as it goes: on two translation threads the
station now **authenticates and associates repeatedly** -- twelve
authentications and eleven associations in three hundred seconds -- and the
guest spinlock deadlock ADR 0064 measured does not happen. The scan is healthy,
which was the standing risk.

The handshake still does not start, and the reason is on this side.

**Message one is never asked for.** The peer arms the handshake on every
association and the access point's own log says so, but its `eapol 1` counter
is *absent* from a three-hundred-second run. Instrumenting the tick with the
guest clock says why, and it is not subtle:

    tick 5386 at 81454 ms
      81466 ms  02:54:44:4d:00:01 associated; handshake armed
                <-- 1027 ms, not one tick -->
      82463 ms  02:54:44:4d:00:01 disassociated
    tick 5387 at 82481 ms

Zero ticks across the whole window the handshake had to fit inside, resuming
eighteen milliseconds after the station gave up. Six windows in that run, each
between one and six seconds, all the same.

**The cause is that the device's delivery gates guard the conversation, not
just the delivery.** `esp_wifi_mac.c` checks four things before it asks the
peer anything -- a ring, a listening receiver, a free descriptor, a buffer --
and every one of them returns early, *above* the `wifi_ask(FRAME_TICK, ...)`
call. So when the radio cannot put a frame on the air, the peer is not told
that time passed at all.

For a frame that answers something the station sent, that is survivable: the
answer is already staged in the device, and it re-arms and goes out when the
gate opens. **A frame the peer starts has no such path.** It can only ride a
tick, and ticks are exactly what the gates suppress.

**And the gate that closes is the receiver.** Counted over a run: sixteen
thousand stalls on "not listening" against eight thousand on "no free
descriptor". The station switches its receiver off, which ADR 0064 already
observed it doing for about a hundred milliseconds after authenticating.

That matters more than it looks, because it tells us what the right model is.
A real access point that sends message one while the station's radio is
elsewhere does not have it held for later. **It is lost.** The access point
gets no reply and sends it again. `esp_wifi_mac.c` already says this, in the
comment above the gate it is talking about:

> What made the exemption look necessary was the retry, not the gate. [...]
> which is what a real link does with a frame that arrived while the radio was
> elsewhere: it is lost, and the station asks again.

The principle is already written down. It is applied to answers and not to
anything the peer starts.

## Decision

1. **The gates gate delivery, not the conversation.** The device asks the peer
   on every tick whatever the radio can do. The four checks stay exactly where
   they are for putting a frame *on the air*.

2. **A frame that cannot be delivered is dropped, and counted.** Not staged,
   not queued, not held until the receiver comes back. The air does not buffer,
   and a model that does is a model whose timing cannot be reasoned about from
   the wire (ADR 0006).

3. **Retrying is the peer's job, because retrying is protocol.** The peer stops
   treating message one as owed-once. It owes it until the station's message
   two arrives, and re-sends it on a deadline of its own -- which is what a
   real access point does, and what ADR 0064 puts in Python.

4. **The device gains no protocol knowledge from this.** It loses a special
   case rather than gaining one: "ask only if you could deliver" becomes
   "always ask; deliver if you can".

## Consequences

**Good**

- **Every frame the peer starts is fixed, not just this one.** Message three of
  the handshake, a group-key rekey, an ARP reply the peer initiates, a DHCP
  offer -- all of them would have hit the same wall, and none of them needs its
  own arrangement now.

- **It subsumes the alternative of chasing the busy descriptor.** Whether the
  driver holds the head for a second stops being load-bearing: a free
  descriptor is needed to *deliver*, not to *talk*.

- **It points at retiring a hack.** `answer_exempt` exists because an answer
  that waits for the gate misses its window, and its own comment says the
  retry was the real fix. Once loss and retry are the general mechanism, that
  exemption is a candidate for deletion rather than a permanent exception.

**Costs, accepted**

- **The peer is asked more often.** During a stall the device re-arms at its
  retry interval, which is shorter than the beacon period, so the round trips
  ADR 0064 budgeted for go up while the receiver is off. That budget was
  argued from the bus bridge blocking a vCPU on every unmodelled access; this
  is smaller than that and on the same reasoning.

- **The peer must tolerate handing over a frame that never flies.** It cannot
  tell delivery from loss -- the device does not tell it, and a radio could
  not. That is what the retry deadline is for, and it means a duplicate
  message one is possible if a frame is delivered and its reply is lost. A
  station is required to tolerate that; the standard's replay counter is what
  makes it safe.

- **A beacon can now be asked for and dropped.** The peer paces beacons by the
  guest clock and marks one sent when it hands it over, so a dropped beacon is
  a beacon missed rather than a beacon deferred. That is correct -- a beacon
  transmitted while nobody is listening is also missed -- but it means the
  beacon rate the peer reports is an offered rate, not a delivered one. The
  device already counts deliveries separately.

## Alternatives rejected

**Stage the undeliverable frame until the gate opens.** The first thing tried
on paper, and it models a radio that buffers. Nothing in the air does, the
staging slot would need a policy for what displaces what, and the frame that
eventually goes out would be one the peer decided to send at a clock it can no
longer reason about.

**Let the association response's reply carry message one.** Two frames for one
request, which the frame channel's "one frame gets one reply" shape would have
to grow a field for. It also puts the two frames on the air back to back, which
is not what an access point does, and it would fix only the frames that happen
to follow something the station sent -- not a rekey, and not DHCP.

**Chase the unrecycled head descriptor.** Worth knowing and not worth blocking
on: the driver holding the head for a second is only a problem because the
conversation depends on it, and decision 1 removes that dependency. If it turns
out to be a modelling defect it can be fixed on its own evidence, without a
handshake waiting on it.
