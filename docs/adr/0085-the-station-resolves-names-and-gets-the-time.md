# ADR 0085 — The station resolves names and gets the time: DNS and NTP in the peer

**Status:** Accepted — 2026-09-17. Continues ADR 0084's arc: from an addressed
station to one that can reach a service.

## Context

Once it has an address (ADR 0084), the station's application asks for the time.
Captured off the wire, it sends **DNS A queries** -- all to 192.168.4.1:53, the
gateway and DNS the OFFER named -- for `pool.ntp.org` and `time.nist.gov`.
Unanswered, it retries them forever and never gets to NTP. It also does other
UDP the peer has no answer for (mDNS on 5353 for `lilygo.local`, among the "ip,
unanswered" count), but the time is the one it blocks on.

## Decision

**Answer the station's DNS and NTP so it can get the time.** Two small servers
in the peer, reached from the same `_ip` dispatch that already handles DHCP:

- **DNS (UDP to 53).** An A query is answered by pointing the name at the
  gateway -- the peer itself. That is deliberately a resolver of one answer:
  the station only resolves its NTP servers, and pointing them here is exactly
  what routes the NTP that follows to a peer that can answer it. The reply
  echoes the query id and question, sets the response flags, and carries one A
  record with a short TTL.

- **NTP (UDP to 123).** Answered with the host's clock: a server-mode reply
  (mode 4, stratum 1) whose reference, receive and transmit timestamps are the
  host time in the NTP epoch, and whose originate timestamp echoes the client's
  transmit timestamp -- which is what an SNTP client checks to match a reply to
  its request.

Both go back as the From-DS Protected data frames ADR 0084 established -- CCMP
header, plaintext payload, identity cipher -- now addressed to the station's
leased address rather than broadcast.

## Consequences

**Good**

- Measured: the station resolves `pool.ntp.org` to the gateway, sends NTP there,
  and gets a reply; it stops retrying the lookup and moves on. The path from a
  raw radio to a station that can name a host and read the clock is complete.

- It reuses everything: the same frame builder, the same identity CCMP, the same
  UDP/checksum helpers as DHCP. DNS and NTP are a parser and a timestamp each.

**Costs, accepted**

- **DNS resolves every name to the gateway.** That is right for the NTP servers
  the station asks about and wrong in general -- a name for a real service would
  be pointed here, and the peer speaks no TCP, so the connection that followed
  would fail. It is a captive answer, not a resolver, and it is enough for what
  the firmware needs.

- **The time is the host's, not a real NTP source.** Good enough for a station
  that wants a plausible clock; it is not a time authority.

- **mDNS is still unanswered.** The application also looks up `lilygo.local` by
  multicast DNS (UDP 5353), which this does not touch, so that lookup keeps
  failing. It is a distinct protocol, and answering it would point the app at a
  service the peer does not run -- worth doing only if something above needs it.

## Alternatives rejected

**Resolve only the known NTP hostnames.** Matching `pool.ntp.org` and
`time.nist.gov` by name and refusing the rest would be tidier, but the station
asks for nothing else over unicast DNS, so the one-answer resolver costs less
and does no harm the firmware reaches.

**Run a real SNTP source.** The station wants a monotonic, plausible time, not a
correct one; the host clock is both, and a real source is machinery for a need
that is not here.
