# ADR 0170 — A register access does not end a translation block without `-icount`

**Status:** Accepted — 2026-11-20. Adds a carried QEMU patch (ADR 0017),
`qemu-patch/io-anywhere-without-icount.patch`.

## Context

ADR 0166 found, tracing the USB controller, that every memory-mapped I/O
access in this QEMU ends its translation block. `accel/tcg/translator.c`
clears `can_do_io` before a block's first instruction and sets it before
its last, unconditionally; `cputlb.c`'s `io_prepare` then calls
`cpu_io_recompile` for any access that is not the last instruction, which
exits the CPU loop and executes the access again in a block of its own. That
rule once applied under `-icount` only, where an access must fall on an
instruction boundary the clock has counted. Without `-icount` nothing
counts instructions, and the rule only costs: a longjmp, a block lookup and
a one-instruction block for every register access that is not last in its
block, every time the block runs.

A firmware for this chip touches registers constantly: the SPI flash and its
cache, the crypto accelerators, the interrupt matrix, the USB FIFOs. And a
slower emulated CPU is the cause of the limits that remain on a guest clock
that follows the host's: Meshtastic's interface timing out on its own
configuration, deadlines missed on a busy host (ADR 0167).

## Decision

1. **Without `-icount`, I/O is allowed anywhere in a block**:
   `translator_loop` does not emit the `can_do_io` stores unless the block
   is compiled for icount, so `cpu->neg.can_do_io` stays true (the CPU loop
   sets it so) and no access recompiles its block. Under `-icount`
   nothing changes; the deterministic runs of ADR 0115 are not touched.

2. **Measured before adopting**, Meshtastic on the T-Deck, `tools/speed.sh`,
   two runs interleaved, twice on different days' loads:
   - home screen 49.6 and 49.3 s as built, 41.6 and 40.4 s with this;
   - 46.2 and 43.1 s as built, 38.9 and 38.3 s with this;
   about 15% sooner; the boot logo about 20% sooner. Two translation
   threads with this were no faster (40.3, 40.5 s), so one stays the
   default (ADR 0041).

3. **What it could break, and why it does not here.** An access in the
   middle of a block lets the rest of the block run before anything the
   access caused is seen: an interrupt it raises is taken a few
   instructions later, which silicon's interrupt latency allows anyway; a
   mapping it changes applies from the next memory access, through the TLB
   flush the cache model already requests, and the code that rewrites this
   chip's cache MMU runs from internal RAM with the cache off, so no block
   runs on through its own remapped code. Our devices that leave a register
   access early (ADR 0166's hold) restore the CPU's state from the access's
   host address, which works anywhere in a block. The whole slow tier ran
   on the patched binary before it was adopted.

4. **Offered upstream only with the owner's approval** (ADR 0017): it
   reverses a choice QEMU made for every target, and the case for it is
   this chip's.

## Consequences

**Good**

- Register-heavy code runs faster: a Meshtastic boot reaches its home
  screen about a sixth sooner.
- A read-modify-write of a device register in one block is again one piece
  of the core's turn, as it was before the rule, which narrows the window
  ADR 0166's hold exists for.

**Costs, accepted**

- A fourth carried patch, to QEMU's core rather than to Espressif's
  machine, which a tag bump must re-check.
- What upstream gained from the rule outside icount is not used here and
  is not measured here either.
