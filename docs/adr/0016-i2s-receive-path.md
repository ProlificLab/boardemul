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

**The path works end to end, which took instrumenting to establish**

This ADR first claimed the interrupt was raised but never delivered. That was
wrong, and the error was in the reasoning rather than the code: the status bits
looked permanently set, but every reading was taken immediately after this model
had set them, so they would have looked that way either way.

Instrumenting the interrupt matrix settled it. At the moment of assertion,
CPU1 has `INTENABLE = 0x1300121c` -- bit 12 set, which is what the guest mapped
source 66 to -- `INTSET` shows bit 12 pending, and `PS` has INTLEVEL 0 with EXCM
clear, so nothing is masked. On the next transition `INTSET` bit 12 has cleared
and `PS.INTLEVEL` is 1: the core is inside a level-1 handler. The guest is
servicing it.

So the whole chain runs: capture completes, the engine raises IN_SUC_EOF, the
matrix maps source 66 to CPU1 interrupt 12, and the handler executes. Buffers
continue to be delivered around the ring rather than stalling after one, which
is the same conclusion from the other end.

**Where the vendor firmware actually stops, which is the other half of audio**

Its LVGL loop still runs about seventeen iterations and stops, and that turns
out to be the *transmit* side. Running the same firmware for thirty seconds and
for ninety produces byte-identical counters -- the same 96 SPI segments, 60 I2C
transactions, 17 keyboard polls -- so it is frozen rather than idle. The last
bus access is `0x6000f024`, which is `I2S_TX_CONF_REG` of I2S0, written
`0x80100` then `0x80104`: TX_UPDATE, then TX_START. `OUT_PERI_SEL_CH0` is 3,
which is I2S0. So after the capture the firmware plays a sound and waits for the
playback DMA, and nothing completes it.

Completing it was tried and is not kept. Two things were learned:

- **A transmit list is not a receive ring.** `suc_eof` ends a transmit list,
  where on receive it only raises a status bit. Stepping a transmit list
  descriptor by descriptor, trusting the owner bit, therefore walks straight
  past the queued buffers -- 179 of them, off the end of DRAM, clearing owner
  bits through everything it crossed. The engine's read side is built to be
  handed a whole terminated list in one call, which is how AES and SHA use it.

- **Even done correctly it crashes the firmware.** One drain of a properly
  terminated 2048-byte list at `0x3fca88c0` kills it with a null load at
  `PS = 0x00060031` -- INTLEVEL 1, so inside a level-1 handler. Its own
  interrupt handler runs and dereferences something not yet built. Slowing
  delivery from 5 ms to 50 ms, which is closer to the real time 2048 bytes of
  audio takes, does not change it, so it is not a simple race.

**What the handler was missing, established by instrumenting it**

It is the address of the descriptor that finished, and this is a bug in the
fork rather than in anything here.

Disassembling the faulting code shows the handler loading a descriptor pointer
out of a structure, getting zero, and dereferencing it to read bits 0..11 --
which is a descriptor's `size` field:

    0x40375aed:  l32i.n  a3, a3, 0      ; a3 = *a3   -> zero
    0x40375af4:  l32i.n  a2, a3, 0      ; faults
    0x40375af6:  extui   a2, a2, 0, 12  ; the `size` field

Logging the register it comes from confirms it: the guest reads the transmit
channel's EOF-descriptor register exactly once and is told nothing.

    GDMAEOFREAD dir=1 chan=0 value=0x00000000

The cause is an asymmetry between the two directions in `hw/dma/esp_gdma.c`.
The receive path records `state->suc_eof_desc_addr = in_addr` when it reaches
the end marker. The transmit path never sets it at all, and the register is not
writable through `esp_gdma_write_chan_register` either, so no device outside
that file can supply it.

Supplying it is one line, and it was tried: the crash goes away completely, the
display still draws, and the firmware advances past audio -- the stall moves
from I2S0's TX_CONF to SPI0. So the diagnosis is right and the fix is small.

It is not applied, because it is a change to a file we do not own and ADR 0001's
boundary has held so far. That is a decision to take deliberately rather than
by drifting into it: carry a patch, upstream it to Espressif, or leave I2S0
unmodelled and the firmware stalled where it is.

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
