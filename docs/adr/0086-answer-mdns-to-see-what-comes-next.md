# ADR 0086 — Answer mDNS, to see what the app reaches for next

**Status:** Accepted — 2026-09-17. A small step past ADR 0085, and a probe: it
resolves the one name the app blocks on so the connection it then makes becomes
visible. **The probe worked:** with `lilygo.local` resolved to the gateway, the
station opens TCP to **192.168.4.1:8896**, retrying with climbing source ports
because nothing SYN-ACKs. That port is the TCP question the next decision turns
on.

## Context

With DNS and NTP answered (ADR 0085), the connected firmware's last unmet
request is a name it looks up over and over: `lilygo.local`. Captured off the
wire, that is an mDNS query -- to `224.0.0.251:5353` -- but not the multicast
shape a responder usually sees. It is a unicast-DNS-style query: a non-zero
transaction id, the recursion-desired flag set, one A question for
`lilygo.local`, sent from an ephemeral port. Arduino's `hostByName` for a
`.local` name makes exactly this, and waits for a unicast answer the way it
would for ordinary DNS. Unanswered, it retries forever and prints
`DNS Failed for lilygo.local`.

Answering it is worth doing for its own sake -- it is the last name the firmware
cannot resolve -- and it is also how the next wall becomes legible: once the
name resolves, the app opens a TCP connection to it, and the SYN says which port
and thus what it wants. That is the evidence a decision about a TCP layer needs,
and it cannot be gathered while the name stays unresolved.

## Decision

**Answer the mDNS query the same way as unicast DNS, pointing the name at the
gateway.** The query's body is byte-for-byte a DNS A query, so the reply is
built by the same code the DNS path uses -- echo the id and question, one A
record -- and sent back unicast to the querier, from the gateway's address on
port 5353. The name resolves to the gateway (the peer itself), which is where a
TCP connection to it will then arrive, to be observed.

**Log the TCP SYN that follows.** The `_ip` dispatch now notes a TCP SYN's
source and destination -- address and port -- so the connection the app makes
to the resolved name is recorded. Nothing answers it yet; this ADR is the step
that makes it appear, not the step that serves it. What to do about TCP is a
separate decision, to be made once the SYN says what the app is after.

## Consequences

**Good**

- The firmware stops failing the lookup: `lilygo.local` resolves, and the app
  proceeds to whatever it wanted the name for. Every name the firmware asks
  about is now answered.

- The TCP target becomes visible, which is the whole point of doing this before
  weighing a TCP layer. A decision that large should be made against a real
  SYN, not a guess.

- Almost no new code: mDNS reuses the DNS reply builder; only the transport
  address differs.

**Costs, accepted**

- **The name resolves to the gateway, which is not what `lilygo.local` is.** As
  with the DNS resolver (ADR 0085), this is a captive answer that routes the
  follow-on connection to the peer so it can be seen, not a truthful record. If
  the app needs the real service behind that name, that is the TCP question.

- **The connection it makes is still refused.** Resolving the name moves the
  wall from DNS to TCP; it does not remove it. That is intended -- the move is
  what exposes the target.

## Alternatives rejected

**Ignore mDNS as out of scope.** It is the last unresolved name and the only
thing standing between an addressed station and whatever it does next; leaving
it unanswered keeps that next thing invisible.

**Answer it and build the TCP layer in the same step.** Building TCP before the
SYN says what protocol rides on it is a large speculative effort; ADR 0085's
arc is to model from the wire, and the wire has not yet shown the connection.
