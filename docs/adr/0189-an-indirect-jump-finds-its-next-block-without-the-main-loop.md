# ADR 0189 — An indirect jump finds its next block without the main loop

**Status:** Rejected — 2026-11-26. Measured; no gain that shows on this host.

## Context

The profile of ADR 0176 puts a quarter of the guest thread in generated code
and 14 % in finding the next translation block. QEMU's Xtensa translator
ends every block whose successor it cannot chain with `goto_tb` -- every
`ret`, `retw`, `callx`, `jx`, and a direct jump to another page -- with
`tcg_gen_exit_tb(NULL, 0)`: back to `cpu_exec`'s loop, which looks the next
block up and enters generated code again. Other targets in the same QEMU
(Arm, RISC-V, x86) end such blocks with `tcg_gen_lookup_and_goto_ptr()`,
which looks the block up from generated code and jumps straight to it.
Neither Espressif's tree nor QEMU's own has made the change for Xtensa
(checked 2026-11-26). An interrupt would still be taken: Xtensa raises one
with `cpu_interrupt()`, which every block's start checks.

## Decision (proposed, then measured)

In `gen_jump_slot()`, a jump that cannot use `goto_tb` would end with
`tcg_gen_lookup_and_goto_ptr()`, except an operation flagged
`XTENSA_OP_EXIT_TB_M1`, which keeps returning to the main loop. The
prototype is `qemu-patch/experiments/xtensa-goto-ptr.patch`, built in a
second tree.

## Measurement

Wall-clock boots (`tools/speed.sh 90 3`, no `-icount`) spread from 17 to
25 s for one binary, so the comparison moved to `tools/speed_icount.sh`:
under `-icount` the guest does the same work in every run (the home screen
at 5.5 guest seconds, within 2 %), and the QEMU process's CPU seconds to get
there measure the emulator. Nine interleaved runs each:

| | CPU seconds to the home screen | mean |
|---|---|---|
| as built | 103, 61, 74, 58, 52, 48, 57, 104, 61 | 68.6 |
| goto_ptr | 93, 43, 50, 65, 75, 64, 68, 86, 55 | 66.5 |

Three per cent, against a spread of a factor of two within each binary:
the host, shared with other containers, sets the time, and the change does
not show above it.

## Consequences

- Not carried, not offered upstream: a patch to QEMU's translator needs a
  gain that can be shown, and this one cannot be here.
- The lookup itself stays where the profile found it; what `goto_ptr`
  saves is the trip through `cpu_exec`'s loop, a part of those 14 %.
- A measurement on a quiet host, or one that counts the host's executed
  instructions rather than its seconds, could decide it; `perf` would, and
  it is not installed (ADR 0176).
