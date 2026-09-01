# ADR 0016 — Model the I2S receive path, from the reference manual

**Status:** Accepted — 2026-09-01. Supersedes ADR 0015.

## Context

ADR 0015 recorded an attempt at this built on inference from bus traffic, and
recorded that it failed. The ESP32-S3 technical reference manual and Espressif's
own register headers were then obtained, and they overturned most of what that
attempt had assumed. It is worth being specific about how wrong inference was,
because the traffic looked perfectly consistent with the wrong reading:

- **0x6002D000 is I2S1, not I2S0.** I2S0 is at 0x6000F000. The board file had
  carried the wrong label since the window was first claimed, and every
  conclusion drawn from "the I2S0 registers" inherited it.
- **The firmware is recording, not playing.** The registers it drives are
  `I2S_RX_*` and the DMA direction is receive. The earlier attempt drained a
  *transmit* channel -- one that had never been started -- which is why it
  cleared descriptor owner bits in unrelated guest memory and killed the
  firmware before it printed a line.
- **`I2S_RX_CONF` is at +0x20 and `I2S_RX_START` is bit 2.** The guessed bit
  was right by luck; the register it was guessed on was not.
- **`GDMA_INLINK_START` is bit 22.** The transmit side's is 21. Reading the
  receive link with the transmit layout finds a channel that is not running.
- **I2S0 is GDMA peripheral 3 and I2S1 is 4.** The firmware binds its receive
  channel to 4, which agrees with the block being I2S1.

The fork already models GDMA, so the descriptor walking, the guest memory
access and the interrupt raising were never the work. Only the peripheral was
missing.

## Decision

Model the receive path, and only that.

1. **Keep the registers, decode one bit.** The block is stored so
   read-modify-write behaves (ADR 0008); the only register read back is
   `I2S_RX_CONF`, for `RX_START`.

2. **Deliver on a timer, not on a register write.** A receiver produces a
   buffer per period. Five milliseconds is fast enough that a driver waiting on
   a buffer is not delayed and costs nothing while no capture runs.

3. **Carry our own cursor around the descriptor ring.** The engine always
   starts from the link address and never advances it; the hardware works its
   way around the ring. So the cursor is ours, and the link address is written
   before each delivery -- address only, since the start bits would re-trigger
   a transfer.

4. **The owner bit is the handshake.** Fill a descriptor only when the DMA owns
   it. The engine clears it, the driver sets it again once it has taken the
   data, and a buffer the CPU still owns is left alone -- which on real
   hardware is an overrun, and stalls the receiver there too.

5. **Silence is the sample value.** There is no audio into this emulator, and
   what the firmware waits for is the transfer, not sound.

## Consequences

**Good**

- The capture completes. Buffers are delivered, descriptors are filled and
  marked, the cursor moves around the ring, and the GDMA reports what it should:
  `raw=0x3` (IN_DONE and IN_SUC_EOF), `ena=0x2`, `st=0x2`. The interrupt is
  enabled and asserted.
- Nothing crashes and no guest memory is corrupted, which is where ADR 0015
  ended.
- Every number in the file is sourced rather than inferred, and says so.

**What it does not achieve, which is the honest headline**

The firmware still does not get past its audio test. The status bits stay set
across every delivery, which means the guest never clears them, which means its
handler never runs. So the transfer is right and the interrupt is raised, and
something between the GDMA and the CPU does not deliver it.

That is downstream of this model. The machine wires receive channel *n* to
`ETS_DMA_IN_CH0_INTR_SOURCE + n`, and that matches Espressif's own numbering, so
the interrupt matrix is the next place to look rather than the wiring.

**Costs, accepted**

- A peripheral that does not yet pay for itself: it is correct as far as it
  goes and buys nothing visible until the interrupt is delivered. It is kept
  because it is sourced, harmless, and the foundation of the remaining step.
- Transmit is not modelled at all. Nothing on this board has asked for it.

## Alternatives rejected

**Leave it as ADR 0015 had it.** That ADR's conclusion -- that the obvious route
does not work -- turned out to be a conclusion about a route built on four wrong
facts. Documentation was the cheaper fix and should have come first.

**Fill the whole descriptor chain at once.** A receive chain is a ring, so
walking it looking for an end does not terminate, and delivering every buffer in
one call is not what a receiver does.

**Keep guessing at the interrupt semantics.** The remaining question -- why an
asserted interrupt is not serviced -- is answerable by reading the interrupt
matrix, and guessing at it is how ADR 0015 happened.
