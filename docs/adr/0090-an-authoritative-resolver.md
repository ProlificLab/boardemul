# ADR 0090 — An authoritative resolver: a zone, and NXDOMAIN outside it

**Status:** Accepted — 2026-09-17. The second workstream of ADR 0088, and the
one that makes the resolver faithful rather than captive. `emu/dns.py`, proven in
`tests/test_dns.py`, replaces the answer-everything-with-the-gateway helper.

## Context

The peer resolved names by pointing every A query at the gateway (ADR 0085 for
DNS, ADR 0086 for mDNS). That is captive: it does not host records, it redirects.
A firmware that trusts whatever it gets is unblocked by it, but it is not a
resolver -- ask it for a name that cannot exist and it says the gateway too, and
a firmware that checked would be misled.

The names the firmware actually looks up are known, off the wire: its two NTP
servers, `pool.ntp.org` and `time.nist.gov`, over DNS, and its own
`lilygo.local`, over an mDNS query shaped like unicast DNS (logs/ntp, logs/mdns,
logs/tcp). Three names, and nothing else asked.

## Decision

**Answer from a zone.** `emu/dns.py` is a small authoritative resolver: it parses
a single-question A query and answers from a table of the names the LAN holds. A
held name gets its A record; a name that is not held gets `NXDOMAIN` (response
code 3), which is what a real resolver says and what makes this authoritative
rather than captive. A DNS query and the firmware's `.local` mDNS query have the
same body, so both go through it; only the port the reply leaves from differs.

**The zone is exactly the names seen on the wire**, each pointing at the gateway,
where the service behind it lives: `lilygo.local` (the factory-test server),
`pool.ntp.org` and `time.nist.gov` (NTP). It is held on the peer, so it can grow
as new names are observed, and it is the peer -- not the transport-level resolver
-- that decides what the LAN hosts.

**Keep it to what is needed, and refuse the rest cleanly.** Single-question A
queries in the IN class are answered; a query of another type or class, or one
that does not parse, is not answered at all rather than guessed at. The resolver
is its own module and its own fast test, so the two behaviours that matter -- a
held name resolves, an unheld name is refused -- are checked without a guest.

## Consequences

**Good**

- The resolver tells the truth about what exists: the three names the LAN hosts
  resolve, and everything else is `NXDOMAIN`. That is the "not captive" of
  ADR 0088 made real for names.
- It is evidence-based: the zone is the names the firmware was measured asking,
  not a guess, so nothing the firmware needs regresses -- it still resolves its
  NTP servers and its `.local` name and still gets the time.
- A self-contained, fast unit (seven tests): held/refused, case-insensitivity,
  the echoed question, the mDNS shape, and malformed input.

**Costs, accepted**

- **The hosted names still point at the gateway.** `pool.ntp.org` resolving to
  the peer is a service decision -- the LAN runs NTP and maps the firmware's
  configured server onto it -- not the real record, exactly as ADR 0085 noted.
  It is faithful as a *self-contained* LAN with a local NTP and a DNS override,
  which many lab networks are; it is not the real internet, which is the deferred
  uplink (ADR 0088).
- **A name the firmware asks for that is not yet in the zone would now fail**
  where the captive resolver answered it. That is the point -- an authoritative
  resolver refuses names it does not host -- but it means a new firmware or a new
  service needs its name added, from the wire, deliberately.

## Alternatives rejected

**Keep answering every name with the gateway.** It cannot be wrong for a firmware
that only asks about names it hosts, but it is not a resolver, and it is the
captive behaviour ADR 0088 set out to remove.

**Host a wildcard: any `*.local`, or any name, to the gateway.** Still captive,
just spelled as a rule. The zone is small and known; a wildcard would answer for
names that do not exist, which is the thing being fixed.
