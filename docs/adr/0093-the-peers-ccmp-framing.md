# ADR 0093 — The peer's CCMP framing

**Status:** Accepted — 2026-09-17. Step one of ADR 0092's plan: the 802.11 framing
around AES-CCM, in `emu/ccmp.py`, proven in `tests/test_ccmp.py`. A building block
-- it is not on the air until the MAC's half exists, because a frame it encrypts
is one the firmware could only read if the MAC decrypted it.

## Context

The cipher is already ours: `emu/aes.py` has CCM over an AES-128 pinned to
FIPS-197, round-trip tested (ADR 0065). What real CCMP needs on top of it is the
part 802.11 specifies and the part that is easy to get wrong -- the nonce and the
additional-authenticated-data. A wrong bit in either makes the integrity code
fail to match, the same failure mode as a hand-counted offset in the handshake,
and just as invisible.

## Decision

**A small module that builds the nonce and the AAD from a frame, per 802.11-2016
§12.5.3, and encrypts and decrypts with the temporal key.** Specifically:

- The **packet number** is split across the eight-octet CCMP header as the
  standard splits it (low two octets, a reserved and a key-id octet with the Ext
  IV bit, then the top four), with `build_header`/`header_pn` the inverse of each
  other.
- The **nonce** is the flags octet (the QoS TID as priority, or zero; a management
  bit), the transmitter address, and the packet number big-endian.
- The **AAD** is the MPDU header with the volatile bits masked -- Retry, Power
  Management, More Data cleared, Protected forced on, the sequence number masked
  to its fragment -- the three addresses, and, for a QoS frame, the TID. The
  fourth-address (WDS) case is refused rather than mishandled, because this
  network does not bridge.

Both ends of the air use this one module -- the peer, and the MAC model that
follows -- so they agree by construction; and because it follows the standard,
the bytes on the air are what a real capture decodes.

## Consequences

**Good**

- The fiddly, get-it-exactly-right part is isolated and fast-tested (nine tests):
  the header split round-trips, the nonce and AAD have the right shape for QoS and
  non-QoS, encrypt/decrypt round-trips, and decrypt rejects a wrong key, a changed
  address, a changed packet number and a torn body.
- **The masking is proved, not asserted:** a frame sealed with Retry clear and
  Protected off still verifies against a header with both set, which is exactly
  what a retransmission needs and what a too-broad or too-narrow mask would break.

**Costs, accepted**

- **No published CCMP vector.** `test_aes` records why the CCM core carries none
  (a hand-typed vector is a liability), and the same holds here; the framing is
  checked by round-trip and structure, and its real proof is the end-to-end run
  once the MAC's half exists and the firmware accepts what this sealed.
- **Not yet on the air.** This is a library; nothing calls it until the MAC model
  can decrypt, so this commit changes no behaviour on its own.

## Alternatives rejected

**Inline the nonce and AAD where the peer frames a data frame.** They are the most
error-prone bytes in the whole cipher; isolating them in a tested module is the
difference between a bug found in milliseconds and one found in a five-minute run.
