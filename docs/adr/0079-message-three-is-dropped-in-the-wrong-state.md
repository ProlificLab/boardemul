# ADR 0079 — Message three is dropped because the supplicant is in the wrong state

**Status:** Accepted — 2026-09-17. Answers the question ADR 0078 reopened, and
finishes correcting ADR 0071.

## Context

With the peer no longer reporting a completion that never happened (ADR 0078),
the real behaviour is visible: the station sends message two, receives message
three, and about eighty milliseconds later sends message two again. It rejects
message three. Message three's content is not the problem -- its integrity code,
its replay counter and its RSN element are all confirmed correct on the wire.

Tracing what the firmware does with message three, filtered to the EAPOL-key
handler and interleaved with the device's delivery marker, over seven message
threes in one run:

- Every message three (187 bytes) enters the handler at **0x4206eb18**, and
  that address appears in all seven message-three windows and in none of the
  seven message-one windows -- so it is message three's handler, not a timer
  that happened to run.
- The handler's second act is to read the supplicant's state from
  `*0x4203f47c + 0x1f4` and dispatch on it: state 8 processes message three
  (0x4206eb38), 9 and 12 go elsewhere, anything else bails to 0x4206eb76.
- For all seven message threes it bails. The message-three processing block
  (0x4206eb38) is entered **zero** times in the whole run. Message three is
  never processed.

(This is also what ADR 0071 misread: the handler reaches 0x4206ef00 by way of
the bail path, so "reaches success" was true and meaningless -- it reaches the
same address whether it processed the frame or dropped it.)

So message three is turned away not on anything it contains but on the state
the supplicant is in when it arrives. The state that would accept it, 8, is set
at 0x4206e279 -- on the message-one path, the code that builds message two --
and that set is **gated**:

    0x4206e26a  bnone a2, 0x30, 0x4206e270   ; only if bits 4,5 of a2 are clear
    0x4206e270  l32i  a2, a1, 32
    0x4206e273  beqz  a2, 0x4206e5d5          ; and only if a2 is non-zero
    0x4206e276  movi  a10, 8
    0x4206e279  call8 set_state               ; state := 8

Across the run, message two was built seven times and `set_state(8)` ran
**once**. The supplicant almost always builds and sends message two *without*
advancing to the state in which it will later accept message three. So when
message three arrives, the state is not 8, the handler drops it, and the
station's retransmit timer resends message two -- forever, until it gives up
with reason 8.

This is a sequencing problem, and it fits everything else: the handshake only
ever appeared to progress under the wide trace filter, which slows the guest,
and a race that a slower guest wins is exactly this shape.

## Decision

**Record the cause as a supplicant state-machine sequencing race, and make the
gate at 0x4206e26a/0x4206e273 the next thing to understand.** The value `a2`
that decides whether message two's send advances the state to 8 -- loaded from
a stack local at the message-one handler -- is what has to be identified: it is
the difference between a message two that sets up to receive message three and
one that does not.

Nothing in the peer or the device is known to be wrong here. The peer's message
three is correct, delivered promptly, and reaches the supplicant. The next work
is inside the firmware's message-one handling, on why it so rarely takes the
branch that arms it for message three.

## Consequences

**Good**

- The wall is located exactly: address 0x4206eb18, the state word at
  `*0x4203f47c + 0x1f4`, the value 8, and the gate at 0x4206e26a that decides
  whether that value is ever set. It is no longer "the station rejects message
  three" but "the supplicant is not in state 8 when message three arrives,
  because building message two only conditionally advances it."

- It is consistent with every prior measurement: correct message-three content,
  the retransmitted message two, the reason-8 exit, and the dependence on guest
  speed.

**Costs, accepted**

- **What `a2` is, is not yet known.** The gate is precise but its input is a
  stack local whose origin has not been traced. Whether the branch is missed
  because of a race, a missing input the peer should supply, or a device
  behaviour that changes it, is open.

- **State 8 versus never-set-or-reset is not fully separated.** `set_state(8)`
  ran once but the processing path ran zero times, so even the one time the
  state reached 8, no message three arrived while it held. Whether the state is
  usually never set or is set and then reset before message three is a
  distinction the next trace should draw.

## Alternatives rejected

**Keep looking at message three's content.** Its integrity code, replay counter
and RSN element are verified on the wire. The handler never examines them: it
drops the frame on the state check, before any content check.

**Change the peer's timing (resend message three later, or repeat it).** The
supplicant never gets far enough to look at message three; sending it
differently changes nothing while the state is wrong. The fault is in when the
supplicant arms itself, which the peer cannot reach.
