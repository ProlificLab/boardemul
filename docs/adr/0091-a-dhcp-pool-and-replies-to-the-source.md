# ADR 0091 — A DHCP pool, and replies to the source

**Status:** Accepted — 2026-09-17. The third workstream of ADR 0088: the one-
station table becomes a real lease pool, and the peer answers each request at the
address that sent it rather than a fixed one. `emu/dhcp.py`, proven in
`tests/test_dhcp.py`.

## Context

DHCP handed out one hard-coded address (ADR 0084): a `CLIENT_IP` constant, given
to whoever asked, and written into the source of every reply the peer sent. For a
single station it is right; for a second it is wrong twice over -- both stations
would be told they are `192.168.4.2`, and both would be sent traffic addressed to
it. The captive assumption is baked in two places: the allocator that only knows
one address, and the reply path that only knows one destination.

## Decision

**Lease from a pool, keyed by hardware address.** `emu/dhcp.py` has a `Pool` over
a range of host numbers on the gateway's /24. A DISCOVER or REQUEST is allocated
the address for its client hardware address: a new station gets the lowest free
one, a returning station gets the one it already had, and an exhausted pool is
refused rather than handing out a collision. The BOOTP request parser and reply
builder move here too, so the whole exchange is one small tested module. The
first lease is still `192.168.4.2`, so the single-station case is byte-for-byte
what it was.

**Answer at the source address.** The peer no longer replies to a fixed client;
`_ip` takes the source address off the request and every answer -- DNS, mDNS,
NTP, and each TCP segment -- goes back to it. A station's source address is the
one DHCP leased it, so this needs nothing more to be correct for a second
station: its traffic is answered at its own address because that is where it came
from. The TCP connection table is keyed by the client's address as well as its
ports, so two stations opening the same ports do not collide.

## Consequences

**Good**

- A second station is now a real possibility rather than an aliased first one: it
  gets its own address and is answered there. The "one station" assumption of
  ADR 0084 is gone from both the allocator and the reply path.
- The exchange is a tested unit (six tests): two addresses for two stations, a
  kept lease across a renewal, an exhausted pool, and the request/reply framing.
- No regression: the first lease and every reply in the single-station case are
  unchanged, which the end-to-end connect test still shows.

**Costs, accepted**

- **No lease lifecycle.** No decline, no release, no expiry within a run, no
  conflict probing. A run is short and a lease is effectively permanent for its
  length; an expiry timer would be state the tests would have to wind for no
  behaviour the firmware exercises. Stated in the module.
- **One /24, addresses only.** The pool does not hand out anything an isolated LAN
  does not need (no per-client routes or options beyond the mask, router, DNS and
  lease). Enough for the network the firmware joins; a more demanding client would
  need more options carried.

## Alternatives rejected

**Keep the single address and just stop aliasing.** Two stations would still be
told the same address; the allocator is the half that has to change, not only the
reply path.

**A full DHCP server with a lease database and timers.** More lifecycle than a
short deterministic run can exercise or a test should have to advance. The pool is
the part that was a shortcut; the timers would be speculation.
