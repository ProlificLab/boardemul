# ADR 0017 — Carry one patch to QEMU's own logic, and send it upstream

**Status:** Accepted — 2026-09-01

## Context

ADR 0001 set the boundary: the Espressif fork is the CPU and SoC engine, and our
C lives in files we add, never as edits scattered through their tree. That has
held through everything so far. Before this, their tree carried six build lines
naming our files and nothing else.

This is the first exception, and it is one line of behaviour.

The fork's GDMA has an asymmetry between its two directions.
`esp_gdma_write_channel`, the receive path, records which descriptor finished:

    state->suc_eof_desc_addr = in_addr;

`esp_gdma_read_channel`, the transmit path, never does. A driver that reads
`GDMA_OUT_EOF_DES_ADDR` to find the buffer that completed is therefore told
zero.

The T-Deck's vendor firmware does exactly that. Instrumenting its handler shows
it loading a descriptor pointer, getting zero, and dereferencing it to read bits
0..11 -- a descriptor's `size` field:

    0x40375aed:  l32i.n  a3, a3, 0      ; a3 = *a3   -> zero
    0x40375af4:  l32i.n  a2, a3, 0      ; faults
    0x40375af6:  extui   a2, a2, 0, 12  ; the `size` field

and logging the register it comes from confirms the source:

    GDMAEOFREAD dir=1 chan=0 value=0x00000000

The register is not writable through `esp_gdma_write_chan_register`, so no
device of ours can supply it from outside. There is no version of this that
stays on our side of the boundary.

## Decision

1. **Carry the fix, as a patch file.** `qemu-patch/gdma-out-eof-desc.patch`
   records the descriptor address before `out_addr` advances, and reports it
   alongside the EOF status -- what the receive path already does.

2. **Applied by `tools/build_qemu.sh`, never by hand.** The build applies it,
   recognises when it is already applied, and **fails loudly** if it no longer
   applies. A patch that has silently stopped matching its target is worse than
   no patch, and a tag bump is exactly when that happens.

3. **Send it upstream.** It is a real bug in their model, not a local
   preference, and it is better fixed there than carried here forever.

4. **The boundary is otherwise unchanged.** One patch, one file, declared in the
   repository and argued for here. If a second one is ever proposed, this ADR is
   the thing to re-read first: the rule was never "no patches", it was "no
   undeclared drift".

## Consequences

**Good**

- The vendor firmware stops crashing when a playback completes, and gets past
  its audio test: the stall moves from I2S0's `TX_CONF` to SPI0.
- It makes the I2S transmit model possible at all, which was otherwise blocked
  on something we could not reach.
- The change is visible in the repository, reviewed like our own code, and
  reproduced by a clean build rather than living in someone's working tree.

**Costs, accepted**

- Their tree is no longer only build lines. The diff is now three files: two
  `meson.build` and `hw/dma/esp_gdma.c`.
- Every tag bump has to re-apply it, and the build stops if it will not. That is
  the intended cost -- it is how the patch stays honest.
- If it is accepted upstream, this ADR should be superseded and the patch
  deleted rather than left to rot.

## Alternatives rejected

**Edit the file directly.** Same effect, no record, and invisible to anyone
rebuilding from a clean clone. This is the drift ADR 0001 exists to prevent.

**Leave I2S0 unmodelled.** The state before this: the firmware stalls in its
audio test and never reaches its interface. Honest, and a dead end.

**Work around it from our own device.** Not possible. The register is not
writable through the public accessor, which is the whole reason this crosses the
boundary at all.
