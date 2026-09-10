# ADR 0056 — What tells the driver a frame arrived

**Status:** Accepted — 2026-09-10. Supersedes the second half of ADR 0055.

## Context

ADR 0055 ended with a frame delivered, announced, taken back, and then thrown
away, and it blamed two bytes of the driver's own state. Two of the
measurements behind that conclusion were unsound, and correcting them moved the
frame a long way further.

**Translation is not execution, and this cost a whole ADR.** ADR 0055's account
of the receive path -- the four clusters, and "a delivered frame reaches
`lmacProcessRxSucData`, `wdevProcessRxSucDataAll` and `wDev_ProcessRxSucData`"
-- came from diffing the blocks a run *translates* with a frame against one
without. A breakpoint at the real entry of `wDev_ProcessRxSucData` does not
fire at all under the configuration that ADR describes. The blocks were
translated in some earlier run, under some earlier setting, and the diff
carried that forward as though it were this run's behaviour.

**There is a fourth way to look at a guest, and it was sitting unused.** The
monitor reads guest memory and cannot write it, has no breakpoints and no
watchpoints. QEMU's *gdbstub* has all four, and speaking to it needs no gdb
binary and nothing added to QEMU: the remote serial protocol is packet framing
over a socket, and `logs/dbg/rsp.py` is a hundred lines of stdlib. What it
bought, in one session:

- **write** guest memory, to try a hypothesis without a rebuild;
- **breakpoints**, which say which routine ran rather than which was compiled;
- **single-stepping**, which says which branch a real frame took;
- and the caller, from `a0` before the callee's `entry` executes.

**And the ROM is a file in this repository.** `pc-bios/esp32s3_rev0_rom.bin`,
384 KB, loaded at 0x40000000 -- so every question about ROM code is answerable
offline, with no run at all. Two tools now do that:

- `tools/rom_symbols.py` resolves all 682 named ROM symbols to the code they
  run. Each named address is a twelve-byte trampoline with its literal in
  front of it, so the linker script's address is never where the code is; a
  breakpoint or a trace filter on the named address catches the trampoline and
  nothing else.
- `tools/disas_rom.py` decodes the loads, stores and branches, and prints the
  bytes of anything it does not know rather than inventing a mnemonic. It was
  validated against the one sequence ADR 0055 had already verified byte by
  byte, and it agrees instruction for instruction.
- `tools/rom_overrides.py` reads the run of `l32r`/`l32r`/`s32i.n` in flash
  that fills the ROM's own globals in, and names all thirty-four of them:
  `wDevCtrl_ptr`, `g_scan`, `g_ic_ptr`, `g_chm`, `if_ctrl_ptr` and the rest,
  each with the address of the structure the driver put there. That is how
  `wDevCtrl` was confirmed to be 0x3fc9a62c without a run.

These live in `tools/` rather than `logs/dbg/` because they are not about
WiFi: any question about ROM code, in any subsystem, is now cheaper to answer
than it was this morning.

**The finding.** The driver's WiFi interrupt handler is in IRAM at 0x4037a9a0,
and it is fifteen instructions long: read `+0xc3c`, and it with an enable mask,
write what it read to `+0xc40`, and then dispatch on individual bits to ROM
routines whose addresses sit in its own literal pool.

| status bits | routine |
| --- | --- |
| 14, 24 | `lmacProcessRxSucData` |
| 15, 21 | `pp_post` |
| 7 | `lmacPostTxComplete` |
| 19 | `lmacProcessAllTxTimeout` |
| 8 | `lmacProcessCollisions` |

This model was raising all thirty-two. It was telling the driver that a frame
had arrived *and* that a transmission had completed *and* that everything had
timed out *and* that the medium had collided, in one interrupt. That is the
"different path" ADR 0055 saw it take and could not name, and it is why the
frame went nowhere: the driver ran every one of those handlers on state that
had never happened.

**The header is forty-eight bytes**, and that is read off the ROM rather than
chosen. On the branch a frame actually takes, `wDev_ProcessRxSucData` computes
`buffer + 48 + n` and reads the packet there; before that it reads the words at
+0, +4, +8 and +44 and the byte at +47. What each carries is partly legible
from the code that uses it:

- word 0 bits 28 and 29: unless one is set the frame does not reach the type
  dispatch at all.
- word 1 bits 8..19: a twelve-bit field the accept path copies into the low
  twelve bits of the word at +44, which is what a received frame's length is
  for.
- word 2, low nibble: selects a type -- 0, 4 and 8 each go somewhere different.
  Its byte 8 is the one ADR 0055 found tested against 64, 65 and 120..127.
- and the ROM merges the current channel into word 2's bits 16..19 itself,
  taking it from `wDevCtrl+44`. So the model does not have to know the channel
  a scan is dwelling on; the controller writes it.

## Decision

1. **Raise one bit, and say which one and why.** The default is bit 14, named
   from the driver's own dispatch table rather than narrowed by halves.
   `BOARDEMUL_WIFI_STATUS` still overrides it, because bit 24 goes to the same
   routine and nothing yet says which one a real MAC uses.

2. **The header is forty-eight bytes, and its unknown words are a hatch rather
   than a guess.** `BOARDEMUL_WIFI_HEADER` takes `offset:value` pairs, so a
   question about a header field costs a run instead of a rebuild. What the
   code makes plain -- the length in word 1 -- the model writes itself.

3. **A trace says who asked.** Every access the device logs now carries the
   guest program counter, and the two registers whose handlers returned early
   -- the acknowledgement and the receive command -- are logged too. They were
   invisible before, which is why an acknowledgement that had plainly happened
   could not be seen happening.

## Consequences

**Good**

- **The whole ROM receive path runs, in order, and it was watched rather than
  inferred.** Breakpoints, retiring after five hits each so a hot routine does
  not starve what follows it:

      lmacProcessRxSucData -> wdevProcessRxSucDataAll -> wDev_ProcessRxSucData
        -> the byte-47 branch -> the type dispatch -> wDev_IndicateFrame
        -> ppEnqueueRxq, with wDev_AppendRxBlocks re-arming the ring

  The frame is handed up and queued. That is six routines further than ADR
  0055's account, and none of it is a diff of translated blocks.

- **Twenty frames put on, twenty taken; forty put on, forty taken.** The driver
  appends fresh descriptors and writes new ring bases to `+0x088` as it goes,
  so the ring turns over indefinitely -- where before it stopped dead after
  four frames and the driver stopped scanning.

- **Scanning is unaffected by a frame arriving.** Twenty-four scans in a
  two-hundred-second run with frames landing throughout, which is the same rate
  as a run with none.

- **`ic_interface_enabled` is decoded**, and it settles what ADR 0055 called
  the interface gate: it is `(wDevCtrl[49] >> i) & 1`, and `wDevCtrl[49]` reads
  1, so interface 0 *is* enabled. The byte at +48 that ADR 0055 found is
  something else, still unnamed -- and the frame no longer needs it, because
  the branch it takes does not consult it.

- **wDevCtrl's head is measured, not guessed**: +0 the ring head, +4 the last
  descriptor, +8 the one being worked on, +44 the current channel, +49 the
  interface bitmask, +52 flags.

**Costs, accepted**

- **A scan still finds no network, and where it stops is now a named
  instruction.** Past `ppEnqueueRxq` the frame is dequeued and parsed: with a
  breakpoint on each and the hot ones retired, `ppRxPkt`,
  `ppDequeueRxq_Locked` and `ppRxProtoProc` all run, and `pp_hdrsize` returns
  24 -- the right 802.11 header length for a three-address management frame,
  so the beacon is being read correctly as a beacon. `ppRxPkt` then calls into
  flash, and the flash routine at 0x421216ec is where the frame ends: it bumps
  `g_hmac_cnt`, tests a flag word, fails a comparison, and hands the buffer to
  `ic_ebuf_recycle_rx` and `ppRecycleRxPkt`.

  It is not a timing window. Two thousand frames twenty milliseconds apart,
  1339 of them taken across twenty-seven scans, and not one network reported.
  Whatever the test is, this frame fails it every time -- which makes it a
  question about the beacon's bytes or a header field, and answerable by
  reading one flash routine.

- **Flash routines can now be named.** `pp_wdev_funcs` is a table of 140
  function pointers, read off a running guest, and the ROM's call sites give
  the offsets: +420 `pm_on_data_rx`, +460 `wDev_DiscardFrame`, +464
  `wdev_csi_len_align`, +488 `ic_interface_enabled`, +504 a flash address.
  Where an entry points into flash, that is a name for a flash routine -- and
  entry +0 is the interrupt handler this ADR is about, which is how the
  handler was confirmed to be the right one.

- **`ppProcessRxPktHdr` never fires, and that proves nothing.** The driver
  installs its own function pointers over the ROM's during initialisation --
  there is a long run of `l32r`/`s32i` pairs in flash doing exactly that -- so
  a ROM routine not running may mean the flash one is running instead. ADR
  0055's "it is thrown away before its header is looked at" rests on the same
  breakpoint-shaped hole and should not be relied on.

- Which of bits 14 and 24 a real MAC raises is not established.

- Header words 0 and 2 are set to what gets the frame through the code, which
  is not the same as what the hardware writes there.

## Method notes

Two things cost runs and are worth writing down.

**`--bus` is not optional.** Without it `run.py` builds no board at all: no
Python devices, no WiFi MAC, no frames. The guest still boots, and boots
*plausibly* -- it just finds nothing on any of its buses, spends a second of
timeout per probe, and reports guest timestamps seven times larger than a
normal run for the same work. Two runs were spent before that was noticed, and
both looked like results.

**The disassembler has one known gap, and it is written into its docstring.**
The narrow three-register forms put their destination where the wide forms put
a source; that was found by a step trace showing `1b22` computing `a2 = a2 + 1`
and is fixed. The narrow branch/`movi.n` split is *not* settled: two
instructions in one flash routine decode as `movi.n` and demonstrably branch.
So a narrow branch out of this tool is a claim to check against a trace, and
every conclusion in this ADR rests on a wide-form branch or on an observed
step, never on a narrow one.

**A watchpoint on a hot page costs the guest, not the script.** A write
watchpoint on `wDevCtrl_ptr` fires from a memory-clearing loop in ROM
thousands of times before WiFi is even initialised, and the run never reaches
WiFi. Polling for a value with brief stops is slower per look and enormously
cheaper overall.
