# ADR 0089 — A real TCP transport, driven a segment at a time

**Status:** Accepted — 2026-09-17. The first workstream of ADR 0088: the accept-
and-observe stand-in of ADR 0087 is replaced by a real connection. This ADR is
the transport itself, `emu/tcp.py`, proven against a hand-written client in
`tests/test_tcp.py`; wiring it into the peer is the next step.

## Context

ADR 0087 accepted a TCP connection and read the first thing said on it, and
stopped there deliberately, to see what the firmware was after (it was a factory-
test server on port 8896). That was never a TCP stack: no send side, no
retransmission, no reassembly, no teardown, and it answered exactly one port.

To carry a real exchange -- to answer the factory-test server, and whatever else
the firmware or a later firmware opens -- the peer needs a connection that
behaves like one. The constraints are the peer's own: everything is Python
standard library (there is nothing to import for TCP, and that is the point), and
the peer can put only one frame on the air per guest event (ADR 0064), so the
transport cannot send on its own timer -- it can only be *asked* what to send,
and the guest's millisecond clock is the only clock it has.

## Decision

**Model the transport as a state machine that takes segment fields in and hands
segments out, and nothing else.** `emu/tcp.py` is a `Connection` with TCP's own
states (RFC 9293) and none of IP, framing, or the air in it. It is fed inbound
segments with `on_segment`, given the clock with `on_tick`, read with `read`,
written to with `write`, and closed with `close`; every one of those returns the
list of segments to send, and the caller -- the peer -- serialises them and
spends them one per guest event. A `Segment` is plain fields (seq, ack, flags,
window, payload, and the MSS option on a SYN), so the whole thing runs and is
checked with no emulator present, the way `wpa2` is.

What it implements, because the point is to be right and not merely to pass the
one exchange in hand:

- **The passive open.** A connection begins in LISTEN, because the firmware is
  always the one that connects; a SYN is answered with a SYN-ACK that offers an
  MSS and advertises the receive window, and the client's ACK establishes it.
- **A receive side that reassembles.** In-order data is delivered and
  acknowledged; a segment ahead of a gap is held and draws a duplicate ack
  pointing at the gap; the gap filler releases the run in order; a duplicate or
  an overlapping retransmit delivers only the octets past what was received.
- **A send side that retransmits.** Written data is segmented to the MSS and the
  window; an unacknowledged segment is resent when the retransmission timer
  expires on the guest clock and not before, the timer doubling on each miss up
  to a cap and resetting when fresh data is acknowledged.
- **Flow control by a real window.** No more than the peer's advertised window is
  outstanding; the window this end advertises is the room left in its receive
  buffer, so a service that stopped reading would stop the peer sending.
- **Orderly teardown from either end.** The passive path
  (CLOSE_WAIT → LAST_ACK) for the firmware closing first, and the active path
  (FIN_WAIT_1 → FIN_WAIT_2 → TIME_WAIT, or CLOSING on a simultaneous close) for a
  service closing first.
- **Sequence numbers that wrap.** All comparison is serial-number arithmetic
  (RFC 1982), tested across the 2**32 boundary in both directions.

**Accept on any port.** The transport does not know about ports at all; which
ports have something behind them is the service layer's business (ADR 0088's next
workstream), not the transport's. The port-8896 gate goes with the old stand-in.

## Consequences

**Good**

- The peer can now hold a real conversation over TCP, in both directions, and end
  it cleanly -- the precondition for serving the factory-test protocol and
  anything else, and for the network no longer being observe-only.
- It is a self-contained, fast unit (twenty tests, milliseconds) that proves the
  hard parts -- reassembly, retransmission, the window, the wrap, teardown from
  both sides -- without a guest, so a regression is caught in the fast tier.
- No new dependency; it is stdlib and arithmetic.

**Costs, accepted**

- It is more transport than the current firmware exercises: one connection, a
  short exchange. That is ADR 0088's accepted trade -- a model that is right over
  one that is just enough.
- **Simplifications, stated in the code:** no urgent data, no options past the
  MSS, no window scaling or SACK, a single-segment go-back-N retransmission
  rather than a fast-retransmit/recovery, and a TIME-WAIT that is a token wait
  rather than two minutes. None of these change correctness on a LAN that does
  not reorder or lose except when a frame is genuinely dropped; each is a place a
  more demanding peer would need more.

## Alternatives rejected

**Keep accept-and-observe and special-case each service.** It cannot send, so it
could never answer the factory-test server or carry a reply; every new service
would re-add the same missing half.

**Let the transport send on its own thread/timer.** It cannot: the peer owns the
one frame per guest event, and a transport that sent independently would have no
frame to send it in. Returning segments to the caller is what fits the channel.
