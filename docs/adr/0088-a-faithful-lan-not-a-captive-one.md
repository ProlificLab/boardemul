# ADR 0088 — The peer becomes a faithful LAN, not a captive one

**Status:** Accepted — 2026-09-17. Direction-setting, like ADR 0006: it does
not add code by itself but says what the WiFi peer is becoming and in what
order, so the ADRs that follow can point back here instead of re-arguing it.

## Context

The station now connects, gets an address, resolves names, reads the clock, and
opens a TCP connection (ADRs 0083–0087). Every one of those was built as the
smallest thing that moved the wall, and each carries a deliberate shortcut,
recorded in its own ADR's costs:

- **DNS and mDNS answer every name with the gateway** (ADR 0085/0086). The
  resolver is captive: it does not host records, it redirects everything to the
  one address the peer answers on. Ask it for a name that does not exist and it
  says the gateway too.
- **DHCP is a table of one** (ADR 0084). A single hard-coded `CLIENT_IP`, handed
  to whoever asks, with no lease table and no second station.
- **CCMP is the identity cipher** (ADR 0084). Frames ride plaintext behind a
  CCMP header whose packet number only advances; the MIC on a received frame is
  not checked and the one on a sent frame is eight zero bytes. It works because
  both ends of the air are ours, but it is not encryption.
- **TCP is accept-and-observe on one port** (ADR 0087). It completes a handshake
  and acknowledges the opening bytes so the client stops retransmitting; there
  is no send side, no reassembly, no retransmission, no teardown, and it answers
  only port 8896.
- **The transmit path exercises one slot.** The driver has several; the model
  and its tests have only ever watched slot 0.

Each was the right call for reaching the next piece of evidence. Together they
describe a network that is convincing enough to connect to and not a real one.
The decision now is to close that gap: make the peer a **faithful LAN** — a
self-contained network that behaves correctly for its own sake, not just
plausibly enough to unblock the firmware.

## Decision

**Replace the captive shortcuts with real, owned implementations, staying inside
the project's boundary** — Python 3.12 standard library only, every line ours,
modelled against the relevant specification the way the crypto was modelled
against 802.11 (ADR 0006 permits the wire *or* the source; a published protocol
is a source). The work is these workstreams, each its own ADR and commits, with
the fast tier green at every step:

1. **Real TCP transport** (`emu/tcp.py`): a connection state machine with a send
   side, retransmission on the guest clock, out-of-order reassembly, flow
   control by a real window, and orderly teardown. Accept on any port; which
   ports have a service is the service layer's business, not the transport's.
2. **A service layer, and an authoritative resolver.** DNS that hosts a zone and
   answers `NXDOMAIN` for names outside it, rather than pointing everything at
   the gateway; the services those names name (the factory-test server on 8896,
   NTP) served over the real transport.
3. **Multi-station DHCP.** A real lease pool keyed by hardware address, so the
   "one station" assumption is gone and a second station gets its own address.
4. **Real CCMP.** AES-CCM with the temporal key (the primitive is already in
   `emu/aes.py`, ADR 0065): the MIC verified on receive and computed on send,
   the header's packet number a real nonce. Done last, because it is the change
   most able to break a working handshake and the one that gains most from
   having faithful services to prove connectivity through.
5. **All transmit slots**, in the model and its assertions.

**Defer the uplink.** Reaching the *real* internet — a NAT bridge that routes
guest flows out through host sockets so the firmware talks to real servers — is
a later, opt-in piece behind a flag (working name `--wifi-uplink`). It is not
built now, because it breaks the determinism the test suite depends on (real
DNS, real NTP, real hosts are not reproducible) and egresses guest traffic to
the real network, which is a decision of its own. The faithful LAN is the
foundation it would sit on, and is worth having on its own.

## Consequences

**Good**

- The network becomes correct, not just convincing: a name that does not exist
  is refused, a second station is possible, a TCP connection can carry a real
  exchange in both directions and be torn down, and the air is actually
  encrypted.
- Each piece is independently useful and independently testable — the transport
  against a simulated peer, the resolver against a query set — so the suite
  gains real coverage rather than one more end-to-end assertion.
- It stays inside the boundary: no new dependency, nothing that reaches the host
  network, runs still reproducible.

**Costs, accepted**

- It is a lot of code for a firmware that today asks little of it — one TCP
  connection, a handful of names. The justification is the project's own aim
  (understand and own every line; a faithful model is worth more than a
  sufficient one), not the current firmware's demands, and that is a deliberate
  change from the strict model-from-the-wire pacing of the last several ADRs.
- The real-internet reach the phrasing "not captive" could also mean is *not*
  delivered here; it is deferred, and until the uplink exists the faithful LAN
  is still an island — a correct one.

## Alternatives rejected

**Keep the captive shortcuts.** They work for the one firmware measured. Rejected
because the project's standard is a model that is right, not one that is barely
enough, and because each shortcut is a latent trap for the next firmware or the
next test.

**Bridge to the real internet now.** The most literal reading of "not captive".
Rejected as the *first* step: it is non-deterministic, depends on the host's
network, and sends guest traffic out — so it is deferred behind a flag and built
on top of the faithful LAN, not instead of it.
