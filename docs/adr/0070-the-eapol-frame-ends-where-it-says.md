# ADR 0070 — The EAPOL frame ends where it says it does

**Status:** Accepted — 2026-09-17. Removes the last obstacle to verifying the
station's handshake reply; follows ADR 0069, which got that reply read at all.

## Context

ADR 0069 got message two of the four-way handshake read correctly, and the
peer recognised it: two hundred and twenty-three of them in a run. None
verified. Every one produced no reply, and the station timed out with reason
fifteen -- the four-way handshake timed out -- exactly as before, so from the
outside nothing had changed.

The integrity code is the first thing that proves the passphrase, and it did
not match. With the frame finally in hand, the check could be done offline
against one captured message two: its own carried code was
`94b4cd8edb097fd9fd6148b55d494abb`, and

- the master key from the passphrase and network name,
- the transient key with the addresses and nonces in min/max order,
- the key-confirmation key as the first sixteen bytes of it,
- the code as HMAC-SHA1 over the frame with its own field zeroed,

reproduced it **exactly** -- but only when the frame was taken to be the length
its own header declares. The whole key schedule was already right. The bug was
four trailing bytes.

**The radio hands over the whole transmit buffer.** The device reads a frame of
head plus body bytes from the DMA descriptor, and for this frame that was four
bytes longer than the EAPOL frame itself: a `0xdeadbeef` marker sitting past
the end. The station computes its code over exactly the EAPOL-Key frame -- four
bytes of header plus the length its header gives -- so including those four
trailing bytes made the code the peer computed differ from the one the station
sent, on every message, with every other input already correct.

    MIC over the frame as received (125 bytes):  1eb292371a8e34525dc0c92703c08ec6   no
    MIC over the declared length   (121 bytes):  94b4cd8edb097fd9fd6148b55d494abb   yes

## Decision

**Truncate the EAPOL frame to its declared length before anything reads it.**
The EAPOL header's length field is authoritative; the four bytes past it are
not part of the frame and must not be in the integrity code's input. The peer
trims `4 + length` off the payload before handing it to the handshake.

## Consequences

**Good**

- Message two verifies, and the peer answers it. Measured on the run that
  fixed it: **message three now goes out**, and the reason-fifteen timeout that
  had defined this wall all along is **gone from the run entirely** -- the
  station no longer times the handshake out, because the handshake is
  advancing.

- The whole WPA2 key schedule is confirmed correct against real firmware, not
  just against the self-test in `test_wpa2.py`: the PMK, the PTK, the address
  and nonce ordering, the KCK, and the MIC all reproduce a code a real
  supplicant computed. That is the thing ADR 0065 said the vectors could not
  prove.

**Costs, accepted**

- **Trusting the length field.** A frame whose declared length exceeded what
  was delivered would be truncated to the delivered bytes by the slice, which
  is the safe direction; a frame that under-declared its length would lose the
  tail, but an EAPOL frame that lies about its own length is not one the
  station would have signed either.

- **It exposes the next step rather than finishing the handshake.** Message
  three goes out and the station does not send message four: it receives the
  third message and then leaves with reason eight rather than completing. That
  is a separate question about message three -- its integrity code, its wrapped
  group key, or its delivery -- and it is the only thing now between here and a
  completed four-way handshake.

## Alternatives rejected

**Stop the device appending the trailing bytes.** They come from the transmit
buffer the guest built, not from the device -- the `0xdeadbeef` is the driver's
own marker past the frame. The device reads what the descriptor's length says,
which is right for putting bytes on the air; it is the EAPOL reader that must
respect the EAPOL length. Trimming at the reader is where the knowledge of what
an EAPOL frame is belongs (ADR 0064).
