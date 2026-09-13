# ADR 0059 — The transmit engine

**Status:** Accepted — 2026-09-13. Corrects ADR 0058's central claim.

## Context

ADR 0058 said `lmacTxFrame` "breaks not once in two hundred seconds of
attempts", and concluded that nothing drives the transmit engine so the queue
never leaves the state that makes `lmacIsIdle` refuse. The conclusion was
right. The measurement behind it was not, and the difference matters.

**`lmacTxFrame` runs six times, nine and a half seconds into the boot.** Every
run that failed to catch it attached a minute in. Attaching before the guest's
first instruction catches it at once — which is the same lesson as ADR 0056's
`in_asm` correction, in a new costume: a probe that starts late cannot tell
"never happens" from "already happened".

**What it programs is one array, and the array was already half known.**
`lmacTxFrame`'s last two instructions are a read-modify-write ORing
0xc0000000 into a register whose address it builds as `(0x0c0067a1 - slot) << 3`
-- which is `0x60033d08 - slot*8` exactly, in the page this device already
owns. That is the same descending per-slot array ADR 0055 found on the receive
side at `+0xd08`, where the handler *clears* the top two bits. Setting them is
"go"; clearing them is what a MAC does when the frame has left.

A trace of a whole boot, opened from the first instruction, shows the sequence
once and only once:

    +0xd04 <- 0x0000000a   lmacSetTxFrame     the slot's parameters
    +0xd08 <- 0x006b9528   mac_tx_set_plcp0   the PLCP header
    +0xd04 <- 0x1200000a   lmacTxFrame        rate and backoff merged in
    +0xd08 <- 0xc06b9528   lmacTxFrame        the top two bits: go

and then nothing, ever again. Six frames were armed and none was ever
completed, so `lmacIsIdle` refused for the rest of the run and no later
`ppTxProtoProc` even reached `lmacTxFrame`.

**Then the driver was asked, one answer at a time**, which is the method the
receive path was found by (ADR 0056). Each step is a run:

- Clear the armed bits and raise the status bit the driver's own handler routes
  to `lmacPostTxComplete` -- bit 7, from the dispatch table in ADR 0056. The
  interrupt arrives and is acknowledged (`rd +0xc3c = 0x00000080`,
  `wr +0xc40 = 0x00000080`), and `lmacPostTxComplete` runs for the first time
  ever. It is a thunk for `pp_post(23, 0)`.
- The handler then asks exactly one thing: it calls the little field accessor
  at 0x420d4fe8 with selector 2, which reads the low four bits of `+0xcb0`.
  With those left at zero it read them, found nothing, and stopped.
- Told which slot finished, it read `+0xcb0 = 1` and then read and wrote
  **`+0xcac`** -- a register nothing had ever touched in any run -- putting the
  same slot number back. That is a pending set and its acknowledgement, the
  same shape as the interrupt status at `+0xc3c` and its acknowledgement at
  `+0xc40` four registers along.

## Decision

1. **Model the completion, not the radio.** A write that *sets* both top bits
   of a slot's control register arms it; after a short delay the model clears
   them, puts the slot in the completed mask at `+0xcb0`, and raises status
   bit 7. Requiring the transition rather than the value keeps a driver that
   reads a register and puts it back from looking like a fresh frame.

2. **The pending mask is write-one-to-clear at `+0xcac`.** A pending set that
   never empties is a handler that never stops, which is the lesson the
   interrupt status already taught this file.

3. **The air time is a hatch.** `BOARDEMUL_WIFI_TX_US`, three hundred
   microseconds by default -- about a short management frame at the lowest
   rate. The real number is not established and nothing yet depends on it.

4. **Do not synthesise replies yet.** The model completes transmissions; it
   does not read the frames or answer them. That is the next piece and it is
   sized below rather than started.

## Consequences

**Good**

- **Transmission completes, and the driver's completion path runs.**
  `lmacPostTxComplete` fires where it never had; the handler reads the
  completed-slot mask and acknowledges it. Four frames go out in a
  two-hundred-second run where one did before, so the engine is turning over
  rather than seizing on its first frame.

- **Two registers are named that were not**: the completed-slot mask at
  `+0xcb0` -- which ADR 0055 had seen the accessor select and could not
  explain -- and its acknowledgement at `+0xcac`, which no run had ever
  touched. Both were found by answering what the driver last read and watching
  what it read next.

- The per-slot array is now understood from both ends: `0xd08 - slot*8`
  control with the top two bits as go/done, `0xd04 - slot*8` parameters with
  the rate in bits 24..27 and the backoff in bits 12..21.

**Costs, accepted**

- **The station still does not connect.** It transmits, the transmission
  completes, and nothing answers it, so authentication times out and
  `WiFiMulti` reports `Connecting Failed.` as before. Completing a transmission
  is not the same as replying to one.

- **Four frames, not forty.** Most connect attempts still do not reach
  `lmacTxFrame`. Whether the queue-state byte is now cleared on every
  completion or only sometimes is not established -- the probe that would say
  so is a breakpoint on `lmacEndFrameExchangeSequence`, which had not fired at
  the point this was written.

- **Eight slots is a generalisation from one.** Only slot zero has ever been
  seen in use. Eight is as far down as the array can be followed without
  colliding with a register this file already names (`+0xcb0` is the first),
  and a write to an unobserved slot is treated the same way.

## What connecting would take

Worth writing down, because it is much larger than this ADR:

- **Read the transmitted frame.** `lmacTxFrame` stores its first argument at
  the queue entry's word 0, so the frame is reachable from the LMAC queue
  table; the buffer layout is the same `eb` the receive side hands up.
- **Answer authentication and association**, which means synthesising two
  management frames and delivering them through the receive path that already
  works.
- **Then the WPA2 four-way handshake**, because a station given a passphrase
  will not associate with an open network -- advertising one makes it report
  `AP not found` instead. That needs PBKDF2-SHA1 for the pairwise master key
  and HMAC-SHA1 for the message integrity check, both in `hashlib`, and
  **AES-CCMP** for the data frames, which is not in the standard library and
  would have to be written. That is the one piece here that is a subsystem in
  its own right.
