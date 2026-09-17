# ADR 0081 — The message-three MIC buffers, dumped: a clean frame, a wrong MIC

**Status:** Accepted — 2026-09-17. Confirms ADR 0080's MIC-failure finding and
**disproves** the receive-corruption cause it suspected.

## Context

ADR 0080 read, statically, that message three fails a sixteen-byte MIC
comparison at 0x4206e194, and suspected the receive delivery of the 187-byte
frame differed byte-wise from what the peer sent -- the receive analogue of
ADR 0069. A breakpoint at the comparison, reading both operands, settles it.

**The comparison is a MIC check, and it fails.** Broken at 0x4206e194 across
four hits in one run, `memcmp(a10, a11, a12=16)`:

    [a10] 6b1c107c9edf8274fd4c7caa76309525   the received MIC
    [a11] d4797dd09f38d30c50968f450c95f4f9   the firmware's computed MIC

`a10` is exactly the MIC the peer put in message three, and it is the standard
one: HMAC-SHA1 of the frame with the field zeroed, truncated to sixteen, under
the KCK -- reproduced offline, and the same KCK reproduces message two's MIC,
so the key is right. `a11` is what the firmware computed. They differ, so the
handshake stops.

**The receive delivery is clean.** Breaking one instruction earlier, at
0x4206e15c -- the memcpy that reads the frame's MIC field before anything
computes over it -- the frame's MIC field already holds, byte for byte, the MIC
the peer sent (`aeedc8bb...` in that run, matching the peer's own log). The
whole frame matches what was sent: of 155 bytes, only the sixteen MIC bytes
ever differ, and that difference appears only after the firmware writes its own
computed MIC in place. **Nothing is corrupted in delivery.** ADR 0080's
suspicion is wrong.

So the firmware receives a correct, standard message three and computes a
different MIC over it. The computed value could not be reproduced offline by
any means tried: HMAC-SHA1, HMAC-MD5, HMAC-SHA256 or AES-CMAC, under any
sixteen-byte window of the PTK or PMK, over any range, offset or LLC-prefixed
form of the frame, and over a few key-data variants. The firmware's own MIC
routine (0x42070044) computes it through the ROM crypto -- the `ets_sha` and
`ets_hmac` family -- rather than plainly, which is where the difference must
live.

The one asymmetry that frames the search: message two, which the firmware
*signs*, verifies against the peer; message three, which the firmware
*verifies*, does not. The same key works in one direction and not the other,
and the visible difference between the two frames is that message three sets
the Encrypted bit and carries wrapped key data where message two carries a
plain RSN element.

## Decision

**Take the MIC failure as confirmed and the delivery as exonerated, and make
0x42070044's exact HMAC input the next thing to recover.** It computes over
some bytes with some key through the ROM crypto, and one of those differs from
the standard HMAC-SHA1 the peer computes. The leading guess, from the message
two / message three asymmetry, is that the firmware forms the MIC input from
the key data after unwrapping it, or otherwise transforms the frame that the
Encrypted bit marks -- but that is a guess, and 0x42070044 read instruction by
instruction is what answers it.

**Keep the dump instrument.** `logs/dump_mic.py` breaks at the comparison and
at the memcpy before it and reads the operands; it is how "clean frame, wrong
MIC" was established and how the next reading -- what 0x42070044 feeds the hash
-- will be checked.

## Consequences

**Good**

- The finding is no longer static inference: the two MICs are read directly,
  the received one matches the peer byte for byte, and the frame is proven
  intact before any computation. ADR 0080's "confirm before building on it" is
  discharged.

- A whole class of cause is eliminated. It is not the receive path, not the
  key schedule, not the frame on the wire. The peer's message three is correct
  by every measure. The difference is entirely inside the firmware's MIC
  routine and what it is given.

**Costs, accepted**

- **The firmware's computed MIC is still unexplained.** It is confirmed wrong
  relative to the standard, and confirmed not to be any obvious standard
  computation, but what it *is* -- which bytes, which key, unwrapped or not --
  is not yet recovered.

- **If the firmware forms the MIC over transformed bytes, the fix is not
  obviously on the peer's side.** A peer that must match a non-standard MIC to
  be believed is a harder thing to build than one that sends the standard, and
  what that would take is unknown until the routine is read.

## Alternatives rejected

**Chase the receive path (ADR 0080).** Disproved here directly: the frame,
including its MIC, arrives exactly as sent.

**Assume the SHA accelerator is mismodelled.** The Espressif QEMU fork models
the SoC's crypto, and message two's MIC -- computed by the same firmware through
the same ROM crypto -- verifies. A broken hash would fail both directions; only
one fails. The difference is the input, not the primitive.
