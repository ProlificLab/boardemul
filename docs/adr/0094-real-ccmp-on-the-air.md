# ADR 0094 — Real CCMP on the air: the MAC encrypts, keyed by the peer

**Status:** Accepted — 2026-09-18. The last step of ADR 0092's plan, and the end
of ADR 0088's workstreams: the data cipher is real. Verified end to end -- the
station completes the handshake, its DHCP rides over AES-CCM, and it gets an
address (`tests/test_wifi_connect.py`, under `thread=multi`).

## Context

ADR 0092 located the cipher: pairwise data CCMP is offloaded to the WiFi MAC,
which `esp_wifi_mac.c` did not model, so the body crossed the air in the clear
(ADR 0084's identity cipher). ADR 0093 built the peer's half of the crypto. What
was left was the MAC's half, and getting the key to it -- the install never
reaches MMIO (ADR 0077), so there is nothing on the bus to intercept.

## Decision

**The MAC model does AES-CCM, keyed by the peer.** In `esp_wifi_mac.c`, the one
place a frame crosses the air -- `wifi_ask` -- now seals a transmitted Protected
data frame and opens a received one, using libgcrypt's CCM (the same libgcrypt
`esp_aes.c` links) with the nonce and additional-authenticated-data built exactly
as `emu/ccmp.py` builds them. The driver hands the MAC plaintext and gets
plaintext back, as against real hardware; what changes is the body on the wire.

**The key comes over the frame channel.** The reply carries a new flag; when the
handshake completes, the peer -- which derived the same temporal key -- sends it
in the sixteen octets after the reply header, and the MAC installs it. Because the
reply to the station's message four is the first reply after completion, and the
channel is synchronous, the MAC has the key before the station transmits its first
data frame. A reset drops the key; a re-association hands over a fresh one.

**On receive, the MIC stays on the frame.** This is the one thing the wire had to
teach. The MAC verifies the MIC and decrypts the body, but leaves the length as
it is, the eight-octet MIC still on the end -- because the driver strips those
eight octets itself, exactly as it stripped the eight-zero placeholder the
identity cipher used to leave there (ADR 0084). Stripping the MIC in the model
too took eight octets of real body with it, and the station, handed a short
frame, dropped every DHCP offer and never got an address. Delivering the frame
the shape the driver already expected fixed it.

## Consequences

**Good**

- The air carries genuine CCMP-encrypted 802.11 frames with verified MICs, the
  crypto sitting where the hardware's does -- in the MAC -- so a capture decodes
  as a real device's would. The identity cipher of ADR 0084 is gone.
- It is proven, not asserted: the DHCP exchange completing at all is proof the
  cipher round-trips, since a broken one drops the encrypted offer and the station
  never gets past it; and the test also asserts the key was installed and no frame
  failed its MIC, so a silent fall back to identity would fail.
- No new dependency: gcrypt in the MAC, `emu/aes.py` in the peer; both already
  present.

**Costs, accepted**

- **The key reaches the MAC out of band**, over the frame channel rather than a
  key install, as ADR 0092 chose -- a modelling seam, recorded and visible,
  taken because the real install is not on the bus (ADR 0077) and the peer's key
  is the same key.
- **`esp_wifi_mac.c` now decrypts and encrypts in the MMIO round trip.** It is a
  handful of frames a second at CCMP, negligible against the run, and it happens
  on the same thread the transmit and receive already run on.
- The seal/open handles data frames, QoS and not, without the fourth address;
  this network has no WDS frames, and one is refused rather than mishandled.

## Alternatives rejected

**Strip the MIC in the MAC on receive.** The natural thing, and wrong: the driver
strips it, so the model must not, or the body loses its last eight octets. The
run said so; the identity cipher had already shown the shape the driver expects.

**Verify the MIC in the peer only and leave the station's frames plaintext.** The
station's receive path expects the MAC to have handled the cipher; handing its
stack ciphertext, or a frame of the wrong length, is what breaks it. The crypto
has to be on both ends of the air, which both ends being ours is what allows.
