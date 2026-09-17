# ADR 0080 — Message three fails MIC verification in the firmware

**Status:** Accepted — 2026-09-17. Answers the gate ADR 0079 pointed at, and
relocates the rejection it named.

## Context

ADR 0079 traced message three to the handler at 0x4206eb18, which bails on a
state check, and pointed at the gate around 0x4206e270 -- `set_state(8)` behind
`bnone a2, 0x30` and `beqz a2` -- as what decides whether the supplicant arms
itself for message three. Tracing that gate to the bottom moves the answer.

**The 0x4206e270 gate is the MIC bit, and message three does not reach it.**
The value it tests, `[a1+32]`, is written at 0x4206e13d as `key_info & 0x100` --
`KI_MIC`. (`a2` there is the frame's key_info: the bytes around it mask the
pairwise bit 0x8, the key-index field 0x30, and against literals 0x2080, 0x800,
0x100 -- all key_info bits.) So that gate advances the state only for a
MIC-bearing frame. Message three has a MIC, so it would pass; but its path
through the main EAPOL processor at 0x4206dff0 does not go through 0x4206e270 at
all. With key_info 0x13ca it runs 0x4206e142 -> 0x4206e15f -> 0x4206e176 ->
0x4206e187 -> **0x4206e197**, and there it bails.

**Where it bails is a MIC comparison, and the MIC does not match.** At
0x4206e194 the processor calls ROM 0x40056eb0, which disassembles as `memcmp`
(word and byte compare, returns the first non-zero difference, 0 on equal). Its
length argument is 16 -- the value 0x4206fc64 returns for key descriptor
version 2, the MIC length. One operand is the frame's MIC field at frame+81
(MIC_OFFSET is 81), the other a sixteen-byte buffer the preceding call
(0x42070044) fills. The sequence is textbook: save the received MIC, recompute
it, compare. At 0x4206e197 `bnez` on the result bails -- and message three
takes that branch every time. **The firmware's computed MIC for message three
does not equal the one the peer put in.**

This is surprising, because the inputs should agree. Message two's MIC verifies
against the peer's KCK, so the firmware's KCK equals the peer's -- the pairwise
key is right. The peer's message three carries a standard MIC: HMAC-SHA1 of the
frame with the field zeroed, truncated to sixteen, version 2. The frame is
delivered clean: 187 bytes on the air, 24 of 802.11 header plus 8 of LLC plus
the 155-byte EAPOL frame, no padding. Every input that decides the MIC is
shared. Yet the firmware computes a different one.

## Decision

**Record message three's rejection as a MIC-verification failure in the
firmware, and make the byte sequence the firmware hashes the next thing to
recover.** The MIC is right by every check the peer can make on its own side,
so the difference is in what the firmware feeds to HMAC: a different length, a
different zeroing, or a received frame that differs from the sent one. The
leading suspicion is the last -- message one at 131 bytes is delivered and its
nonce survives (message two's key derives from it and verifies), but message
three at 187 bytes is larger, and a delivery that is byte-correct at 131 and
not at 187 would fail exactly here while leaving everything before it intact.
It is the receive-side analogue of ADR 0069's transmit-frame bug.

**Confirm before building on it.** The `memcmp`, the length 16, the MIC offset
and the bail are all read statically and from the block trace; the one thing
not yet seen directly is the two sixteen-byte operands. A breakpoint at
0x4206e194 -- which runs only for a MIC-bearing frame, so it is not hot --
reading the buffer at a1+124 and the sixteen bytes at frame+81 would show the
received and computed MICs side by side, prove they are MICs, and show how they
differ. That is the next step, and it is cheap.

## Consequences

**Good**

- The gate ADR 0079 named is now understood exactly -- it is the MIC bit -- and
  it is not what turns message three away. The real rejection is a MIC
  comparison sixteen bytes wide, located to one instruction (0x4206e197) and
  one ROM call (0x40056eb0, memcmp).

- It is a content rejection, not a state one, which is a more tractable place
  to be: the peer controls the bytes of message three, and the fix is on the
  wire, not in the firmware's state machine.

**Costs, accepted**

- **ADR 0079's "dropped on state" is relocated, not simply kept.** The state
  bail at 0x4206eb18 is real, but it is a second handler; the operative
  rejection -- the one that stops message four -- is the MIC failure in the main
  processor. 0079's gate reading was a true fact about the wrong branch.

- **Why the firmware's MIC differs is not yet known.** The finding is that it
  differs; the cause -- delivery, length, or zeroing -- is a hypothesis with a
  named next test, not a conclusion.

- **The operands have not been seen directly.** The MIC reading rests on a
  strong static chain (memcmp, length 16, MIC offset, the save/compute/compare
  shape) but not yet on the bytes themselves.

## Alternatives rejected

**Return to the state machine (ADR 0079).** The state handler is not where
message four is lost: the main processor rejects message three on its MIC
first, before any message four could be built.

**Re-derive the key schedule.** The KCK is proven correct by message two. The
MIC that fails is computed with the right key over the wrong bytes, so the key
is not the thing to change.
