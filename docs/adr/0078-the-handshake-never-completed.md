# ADR 0078 — The handshake never completed; the peer miscounted a retransmit

**Status:** Accepted — 2026-09-17. Retracts ADR 0074, and with it the "handshake
completes" milestone that 0075, 0076 and 0077 were built on top of.

## Context

ADR 0074 reported the four-way handshake completing, on the strength of the
peer counting "eapol 4" and "handshake done". Both counts were wrong, and the
mistake was in the peer, not the firmware.

`Handshake.take` declares the handshake done on the *second* signed frame whose
integrity code verifies -- on the assumption that the first is message two and
the second is message four. It is not. Reading the frames the peer actually
received, byte for byte:

    message two (first signed frame):  key_info 0x010a, replay 1, no Secure
    "message four" (second frame):     key_info 0x010a, replay 1, no Secure
    the two frames are byte-for-byte identical

The second frame is a **retransmitted message two**. A real message four sets
the Secure bit (key_info 0x030a); this frame does not, and every signed frame
the station sent in the whole run was 0x010a with Secure clear -- all message
twos, no message four anywhere. The station sends message two, receives message
three, and about eighty milliseconds later -- its retransmit timer -- sends
message two again. It is not accepting message three.

So the handshake has never completed. The station has been rejecting message
three all along, exactly the wall ADR 0071 set out to explain, and ADR 0071's
"message three is accepted" was itself a misreading -- the fourth time this
session that more-code-ran or a wrong offset was read as a different outcome.
Everything downstream that assumed completion -- the 970 ms "post-handshake"
wall (0074), the key-slot hunt (0076, 0077) -- was chasing a state the firmware
never reached. The key-slot conclusion of 0077 still stands on its own evidence
(the key never touches MMIO; CCMP is software), but it was never the reason the
station left.

## Decision

**Make the peer honest first: declare the handshake done only on a real message
four.** `take` treats a signed frame received after the keys are derived as
message four only if it carries the Secure bit; a signed frame without it is a
retransmitted message two, and the peer resends the same message three -- the
identical frame, so its replay counter does not climb -- and keeps waiting. The
counts follow: "eapol 4" and "handshake done" are emitted only for a Secure
frame, and a retransmit is counted as the message two it is.

**Then reopen the real question: why the station rejects a message three whose
integrity code and replay counter are both correct.** That is a fresh
investigation, and the honest peer is the instrument for it -- once it stops
reporting a completion that did not happen, the retransmit is visible as a
retransmit.

## Consequences

**Good**

- The peer's log can be trusted again. A handshake that completes will say so
  because a Secure frame arrived, not because a frame arrived twice.

- The problem is back where the evidence puts it -- message three rejected --
  and no longer hidden behind a false success. The MIC and replay counter are
  confirmed correct on the wire, so the cause is in the key data or a key_info
  bit, which is a bounded search.

**Costs, accepted**

- **Four ADRs (0074-0077) rest on a completion that never happened.** They are
  kept, per the numbering rule, and this page is the correction they point to.
  0077's key-slot finding survives on its own footing; 0074's milestone does
  not.

- **The session's headline result is withdrawn.** "The handshake completes" was
  reported with measurements, and the measurements were of the peer's own bug.
  The lesson is the session's recurring one: a count is not an outcome until
  the thing counted is confirmed to be the thing meant.

## Alternatives rejected

**Leave the completion detection as it is and treat the retransmit specially in
the caller.** The bug is in what "done" means, and it belongs in the one place
that decides it. Papering over it in the frame handler would leave the next
reader the same trap.

**Resend message three with a fresh replay counter on each retransmit.** The
station is rejecting the content, not the counter; a new counter changes
nothing and only makes the exchange harder to read. The same frame goes back.
