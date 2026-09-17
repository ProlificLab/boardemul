# ADR 0073 — Message four is never submitted, and the transmit path is sound

**Status:** Accepted, central measurement amended by 0074 — 2026-09-17.
Message four *is* submitted and the handshake *does* complete when the guest
runs slower; "never submitted" holds for the fast run measured here, not for
the firmware. The transmit-path clearance and the arms-equal-frames accounting
below stand, and 0074 builds on both. Supersedes 0072, and retracts the
explanation ADR 0071 offered. The measurements in both still stand; only their
accounts of *why* were wrong.

## Context

ADR 0071 measured that message four never arms the modelled transmit slot, and
explained it by a hardware CCMP path the device cannot see. ADR 0072 traced the
send path, found `ppTxProtoProc` abandoning at the branch after `lmacIsIdle`,
and explained it by a busy queue. **Both explanations are wrong, and the
second was wrong for an instructive reason.**

`tools/analyse_trace.py` reports the distinct program counters of a window in
the order first seen. That is the right instrument for "what code is involved"
and the wrong one for "which way did this send branch": a basic block already
seen earlier in the window is suppressed at its later position. Reading one
send's control flow off it produced a confident, false answer. A second error
compounded it -- `ppTxProtoProc` is at 0x4003d6ac, not 0x4003d740, so the
offsets in the window were attributed to the wrong instructions.

`tools/msg4_branch.py` counts every occurrence of the addresses the decision
turns on, without deduplication. Over a 200-second run:

| address | meaning | whole run |
|---|---|---|
| 0x4003d809 | `beqz` on what `lmacIsIdle` returned | **454** |
| 0x4003d867 | busy: abandon, `return 0` | **1** |
| 0x4003d81c | the arm returned | 452 |

**453 of 454 sends transmit.** The abandon happens once in two hundred
seconds; it is an incidental event, not a wall. The transmit path is healthy.

The peer's own tally closes the account. What it received from the station:
433 probe + 6 auth + 5 assoc + 5 data + 4 disassoc = **453 frames** -- exactly
the number of sends that armed. Every frame the driver handed to the transmit
engine reached the air, and nothing was lost inside the device.

So message four is not lost in the radio, not lost in the cipher, and not
turned away by a busy queue. **It is never submitted to the transmit path at
all.** The peer counts 5 message ones, 5 message twos, 5 message threes and no
message four across five association attempts; the station then leaves with
reason 8.

## Decision

**Look in the supplicant, between accepting message three and emitting message
four, and stop looking at the device's transmit path.** The evidence that the
device is not at fault is now positive rather than absent: the station's own
message two is built, armed, transmitted, received by the peer and verified
byte-for-byte (ADR 0070), over the same path, in the same run. A path that
carries message two carries message four.

**Retain `tools/msg4_branch.py`, and prefer counting to first-seen ordering for
any question about which way a branch went.** The deduplicating analyser keeps
its use for shape; it must not be used for control flow again.

## Consequences

**Good**

- The device is cleared by measurement, not by assumption: 453 arms, 453
  frames received, arithmetic that closes exactly. The transmit engine, the
  DMA-descriptor frame read (ADR 0069) and the completion path are all sound
  at the point where the handshake stalls.

- The remaining question is entirely inside firmware the project does not
  write, which is where the tools already built -- the filtered trace, the
  recovered `__func__` names, the offline disassembler -- are strongest.

- Two wrong explanations are off the table, and the cost of each was a wrong
  instrument rather than a wrong measurement. The measurements from 0071 and
  0072 are reusable; only the readings are retracted.

**Costs, accepted**

- **Three ADRs now describe one wall, two of them retracted.** The numbering
  rule keeps them, so the history is longer than the finding. That is the
  price of writing the decision before the code and correcting in public
  rather than quietly.

- **`lmacTxFrame` is counted 454 times against 453 arms.** One call comes from
  a site other than this branch, and that one call is not yet accounted for.
  It does not affect the conclusion -- the peer's 453 received frames match the
  453 arms -- but it is not fully explained either.

- **Why the supplicant stops is still open.** What is now known is where to
  look and, just as usefully, where not to.

## Alternatives rejected

**Keep pursuing the queue-state byte.** It decides one send in 454. Chasing it
would be optimising an event that does not happen.

**Model the CCMP transmit path (ADR 0071's plan).** Message four is plaintext
and is never submitted; a cipher cannot explain a frame that no one offered.

**Trust the first-seen analyser with a tighter window.** The defect is in the
instrument, not the window: suppressing a repeated block is exactly wrong for
a branch that is taken many times. Counting replaces it.
