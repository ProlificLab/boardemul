# ADR 0065 — AES by hand, and what can be proved about it

**Status:** Accepted — 2026-09-15. Follows ADR 0064, which put the peer in
Python and left it needing a cipher.

## Context

WPA2 needs AES in three places, and Python's standard library has none of it.
The one accepted dependency in this project is the QEMU fork; everything
outside it is the standard library. So AES gets written.

What is actually needed is narrow, and worth pinning down before writing a
general library:

- **AES-128 encryption of a single block.** Nothing else. The key encryption
  key a four-way handshake wraps with is sixteen bytes, and so is the temporal
  key CCMP encrypts with. No other key length occurs.
- **No decryption.** AES key wrap needs the block cipher forwards to wrap;
  only the station unwraps. CCM is counter mode with a CBC message integrity
  code, and both directions of CCM use the cipher forwards only. A decrypt
  path would be a hundred lines that nothing calls, and a hundred lines
  nothing calls are a hundred lines nobody checks.
- **Key wrap (RFC 3394)** for the group key in the third handshake message.
- **CCM (RFC 3610)** with an eight-byte integrity code and a two-byte length
  field, which is exactly what 802.11 asks CCMP for.

## Decision

1. **The substitution box is computed, not typed in.** It is the
   multiplicative inverse in GF(2^8) followed by a fixed affine map, and both
   are four lines. A literal table of two hundred and fifty-six bytes is two
   hundred and fifty-six chances to typo something that then decrypts almost
   everything correctly. The table this builds is checked against the two
   entries everybody knows -- `S[0x00] = 0x63` and `S[0x53] = 0xed`.

2. **AES-128 encryption only**, with the key length checked rather than
   generalised. A sixteen-byte key is the only one this project has.

3. **CCM is written from RFC 3610 directly**, generically in the integrity code
   length and the length field, because CCMP's choice of both is then a caller
   argument rather than a constant baked through the code.

4. **What is verified, and how, is stated per layer** -- because the three
   layers can be verified to very different degrees and pretending otherwise
   is how a cipher bug survives:

   | layer | verified by |
   | --- | --- |
   | AES-128 block | the two FIPS-197 vectors, in the fast suite |
   | key wrap | the RFC 3394 128-bit-over-128-bit vector |
   | CCM | round trip, and structural properties |
   | CCMP framing | the station accepting the frames |

## Consequences

**Good**

- Milliseconds to check, in the tier ADR 0012 built for it. A cipher is the
  best possible fit for a fast test: fixed input, fixed output, no guest.

- The narrowness is the point. Encrypt-only AES-128 is about a hundred and
  fifty lines, all of it reachable, none of it speculative generality.

**Costs, accepted**

- **CCM has no published vector here, and that is a real gap.** Two of the
  three layers are pinned to bytes published in a standard. CCM is not: it is
  checked by encrypting and decrypting back, and by showing that the integrity
  code moves when the additional data, the nonce or the key moves. That
  catches an asymmetric mistake and a field that is not being fed in. It does
  not catch a mistake symmetric between the two directions -- a counter block
  assembled the same wrong way on both sides round-trips perfectly and fails
  against every real station.

  Rather than type a vector from memory and dress a guess as a proof, the gap
  is named. The station is the oracle that closes it: CCMP that is wrong in
  that way shows up as a station that completes the handshake and then answers
  nothing, and that is a distinguishable outcome rather than a silent one.

- **A wrong nonce or additional-data layout is the likeliest bug and the
  hardest to see.** 802.11 masks particular bits of the frame control and
  sequence control fields before they go into the additional data, and gets
  the nonce from the packet number, the priority and one of the addresses. All
  of it is bookkeeping, none of it is exercised by a round trip, and the
  failure is silent. It gets written with the standard's field names in the
  code beside it so that the next reader can check it against the standard
  rather than against an intention.
