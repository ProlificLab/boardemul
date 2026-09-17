# ADR 0071 — Message four leaves by a path the device cannot see

**Status:** Accepted — 2026-09-17. Records where the four-way handshake stands
after ADRs 0069 and 0070, and names the wall that is left.

## Context

ADR 0070 got message two verifying and message three sent. The station receives
message three, and the question was why it then leaves with reason eight
instead of completing. Two device bugs having already masqueraded as
crypto failures, this was chased to ground before any conclusion.

**Message three is accepted.** Traced through the supplicant's EAPOL-Key
handler at 0x4206eb18: its two exits are 0x4206ef00 (`movi a2, 0`, success) and
0x4206ee5a (`movi a2, -1`, failure), both falling through a stack-canary check
to the return. After every message-three delivery the handler reaches the
**success** exit -- four times out of four in one run, zero failures. So the
whole of message three is right: the MIC the station computes, the replay
counter, the wrapped group key, the RSN element. The station validates it and
proceeds.

**Message four never appears on the wire the device watches.** The station
transmits by arming a slot register, and the device reads the frame from there
and hands it to the peer. Instrumented to log every write to all eight slot
registers and every armed transition:

- every armed write, all of them, goes to slot zero (`+0xd08`); no other slot
  register is ever armed, so the "unserviced slot" counter stays zero;
- the device detects exactly as many data-frame transmits as there are second
  messages -- eight message-twos, eight detected data transmits, and **no
  ninth**. Message four is not among them.

So after the pairwise key is installed on message three, message four is
transmitted, but not by writing the slot register the device models. The
transmit path changes once the key is in: the hardware CCMP engine carries the
frame, and it does not arm `+0xd08`. The access point therefore never receives
message four, the handshake cannot finalise on its side, and the station --
which also does not reach a usable link, because everything it now sends is
encrypted and unseen -- gives up and disassociates with reason eight. The
firmware's own connection manager reports "Connecting Failed", never
"connected".

This is the same shape as ADR 0069's finding one layer on: a frame that is
real and correct, transmitted by a mechanism the device does not yet model.

## Decision

**Record the boundary; do not model the CCMP path in this change.** The
four-way handshake is understood and correct up to message three and the key
installation it triggers. What remains -- the encrypted transmit path, and with
it the encrypted receive path, CCMP decryption in the peer, and the ARP and
DHCP a station needs for an address -- is the "through to an IP address" work
ADR 0064 set out, and it is a phase of its own rather than a fix to bolt on
here. `emu/aes.py` already has the CCM primitive it will need (ADR 0065).

## Consequences

**Good**

- The four-way handshake is proven correct against real firmware as far as the
  cryptography goes: PMK, PTK, both nonces and their ordering, the KCK and the
  MIC (ADR 0070), the group-key wrap, and now that message three is *accepted*,
  not merely well-formed. Nothing in the WPA2 key schedule is left in doubt.

- The wall is named precisely and cheaply testable: the device sees N data
  transmits for N handshakes and no message four, which any future run can
  check in one line.

**Costs, accepted**

- **The handshake does not complete**, and cannot until the CCMP transmit and
  receive paths are modelled. That is a substantial piece of work -- a second
  hardware path in the WiFi MAC device, plus CCMP and a minimal IP peer in
  Python -- and it is deferred, not done.

- **"Message four leaves by the crypto path" is an inference, a strong one.**
  What is measured is that message three is accepted and that no message four
  arms the modelled slot. That the alternative -- the hardware CCMP TX path --
  is what carries it is the reading that fits every observation, but it has not
  been traced into the crypto registers themselves. The first task of the next
  phase is to confirm it there.

## Alternatives rejected

**Keep chasing message four as a handshake bug.** It is not one: message three
is accepted and the key is installed. Treating the missing message four as a
crypto or framing error would be chasing a frame that was correctly sent, which
is the mistake ADR 0069 already paid for once.

**Model just enough to fake message four's arrival.** The AP could pretend it
received message four and call the handshake done. It would be a lie the moment
the station sent its first real data frame, which is encrypted and equally
unseen -- the link would still not carry a packet. The honest next step is the
cipher, not a shortcut around it.
