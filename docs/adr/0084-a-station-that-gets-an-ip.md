# ADR 0084 — A station that gets an IP: identity CCMP, ARP and DHCP in the peer

**Status:** Accepted — 2026-09-17. The path from a completed handshake (ADR
0083) to an addressed station (ADR 0064's goal) — **reached**: measured, the
station completes DHCP and the firmware prints "Obtained IP address:
192.168.4.2" (event 7, GOT_IP), then stays connected.

## Context

With the handshake completing, the station connects and starts its network
stack. Captured off the wire, the first thing it sends is a **DHCP DISCOVER**:
a data frame with To-DS and the Protected bit set, an eight-byte CCMP header,
and then -- **in plaintext** -- an LLC/SNAP header, an IPv4/UDP packet from
0.0.0.0:68 to 255.255.255.255:67, and a BOOTP request (xid, the client's own
MAC as chaddr, hostname "esp32s3-<mac>", and a parameter-request list for
subnet mask, router, broadcast, DNS and NTP).

The payload is plaintext even though the Protected bit is set and a CCMP header
is present. On this emulator the CCMP cipher is effectively identity: the frame
carries its CCMP header and PN but the data is not encrypted. That is a gift,
and it is also exactly the shape ADR 0064 anticipated -- both ends of the air
are ours, so the cipher can be identity and only the framing has to be right.
The station also sends Null-data / power-management frames (subtype 4, no
payload); those are keepalives and need no answer.

## Decision

**Model CCMP as identity in the peer, and add the ARP and DHCP a station needs
to get an address.** Concretely:

- **Receive.** A data frame with the Protected bit carries an eight-byte CCMP
  header before the LLC. Strip the 802.11 header (24 bytes, or 26 for QoS) and,
  when Protected, the eight CCMP bytes, then read the plaintext LLC/SNAP and its
  ethertype. Trim to the IP total-length so the device's trailing bytes (the
  0xdeadbeef marker and whatever follows) are ignored, as ADR 0070 does for
  EAPOL.

- **ARP (ethertype 0x0806).** Answer a request for the gateway with the access
  point's own address.

- **DHCP (UDP 68 to 67).** A DISCOVER gets an OFFER, a REQUEST gets an ACK,
  each carrying a fixed address for the one station (a /24 with the access point
  as gateway and DNS), the client's xid and chaddr echoed, and the options it
  asked for.

- **Transmit.** Build the reply as a From-DS data frame with the Protected bit
  and a CCMP header (a monotonic PN, key id 0, ExtIV set), the LLC/SNAP, and the
  plaintext IP payload -- the mirror of what the station sends. What trails the
  payload (a CCMP MIC, and whether the station checks it) is read from the wire:
  the first build uses the simplest trailer that the station accepts, found by
  seeing whether it proceeds to the next exchange.

The addresses are fixed rather than pooled: there is one station, so a DHCP
server here is a lookup table of one, not a lease manager.

## Consequences

**Good**

- It reuses everything already proved -- the frame channel, the delivery gates,
  the receive path -- and adds only parsing and a table. No new crypto: the
  cipher is identity, matching the wire.

- It is the last step of ADR 0064's arc. A station with an IP is a station that
  can be talked to over IP, which is where an emulator that runs vendor
  firmware becomes useful for the things above the driver.

**Costs, accepted**

- **The CCMP trailer is the simplest thing that works: eight zero bytes.**
  Measured -- the station accepts an OFFER and an ACK carrying a CCMP header, a
  plaintext payload, and eight zero bytes where the MIC goes, and proceeds to
  REQUEST and then to GOT_IP. It does not verify the MIC on a received frame in
  this emulator (its own cipher is identity), so no CBC-MAC is needed.

- **Identity CCMP is honest only because the emulator's cipher is identity.**
  If a future QEMU change makes the AES accelerator encrypt for real, the
  station's own frames would become ciphertext and this would have to decrypt
  them with the TK (which is derived and proven; emu/aes.py has CCM). Recorded
  so the assumption is visible.

- **A one-station DHCP table is not a DHCP server.** It answers this firmware,
  not a network. Widening it is future work and unneeded now.

## Alternatives rejected

**Real CCMP with the TK.** The station does not encrypt, so a peer that
encrypted would hand it ciphertext its identity decap would read as garbage.
Real crypto is the wrong model for an identity cipher, however faithfully done.

**Ignore DHCP and hand the IP in out of band.** The firmware asks for an
address by DHCP; answering it is what makes the connection real and what a
second board or a real access point would do. Poking an IP past the stack would
prove less and match the hardware less.
