# ADR 0087 — Accept a TCP connection and read its opening, nothing more

**Status:** Accepted — 2026-09-17. The bounded slice of TCP that ADR 0086's SYN
called for: enough to see what the app says first on port 8896, not a stack.

## Context

Once `lilygo.local` resolves to the gateway (ADR 0086), the station opens TCP to
192.168.4.1:8896 and retries, because nothing answers the SYN. Every UDP the
firmware sends is now handled; this connection is the only thing left, and what
rides on port 8896 is unknown. A protocol is identified by its opening bytes,
and those do not appear until the connection is established -- which needs a
three-way handshake. So the smallest useful thing is not to serve the service
but to accept the connection and read what the client sends first.

**It worked, and the opening is legible.** With the handshake completed, the
firmware sends, in the clear, a JSON identity record:

    {"SKU": "NA", "product": "T-Deck", "mac": "02:54:44:4D:00:01",
     "date": "2026-09-18,03:22"}

So `lilygo.local:8896` is a factory-test / registration server: the device
connects and reports what it is. (The date is a real one, which is the NTP of
ADR 0085 having landed.) That is the protocol identified; whether to answer it
as the test server would is the next, separate decision.

A full TCP is a large, open-ended piece -- retransmission, congestion,
reassembly, teardown, options. None of that is needed to read an opening
segment from one local, cooperative peer over a lossless emulated air. The scope
here is deliberately the accept-and-observe slice and no more.

## Decision

**Complete the handshake, acknowledge the client's first data, and log it.** In
the peer's `_ip` dispatch, a TCP segment to port 8896 goes to a small handler
that keeps one dict of state per client source port:

- **SYN** (no ACK): open a connection. Pick an initial sequence number, remember
  the client's, and send **SYN-ACK** with an MSS option and a plain window. The
  SYN consumes one sequence number on each side.

- **Data** (a segment with a payload): the first time, count it and log the
  payload as hex and as text -- that is the observation. Acknowledge it by
  advancing the received-sequence count and sending a bare **ACK**, so the
  client does not retransmit.

- **RST**: forget the connection. A bare **ACK** with no data needs no reply.

Segments go back as the same From-DS Protected data frames every other reply
uses (ADR 0084's identity CCMP), with a TCP/IPv4 checksum filled in the way UDP
already is. The IP-header build is factored out so UDP and TCP share it. The IP
packet is first trimmed to its total-length field, so the device's trailing
0xdeadbeef marker is not read as payload -- a length-less header like TCP's
otherwise takes it for the opening, which it did on the first attempt.

**It answers nothing above TCP.** It does not send application data, reassemble
across segments, retransmit, or close cleanly. It exists to make the opening
bytes appear in the log; what to do with them -- whether port 8896 is worth
serving, and with what -- is the next decision, and this is the evidence it
needs.

## Consequences

**Good**

- The protocol on port 8896 becomes readable: the client's first segment is
  logged, hex and text. That is the one fact a decision about serving the
  service depends on, and it could not be had without the handshake.

- The slice is small and self-contained. No retransmit or teardown machinery,
  because the air does not drop and the goal is one segment. The checksum and
  frame plumbing are already there from UDP.

**Costs, accepted**

- **It is not TCP.** No retransmission, no reordering, no window management, no
  FIN handshake. A client that needed a reply to continue, or that sent its
  opening across several segments with gaps, would not be fully served -- but
  the first segment, which names the protocol, still arrives.

- **The connection is left half-finished.** After the opening is read the peer
  keeps acknowledging but says nothing, so the client eventually times out or
  resets. That is fine for observing and would not be for serving.

- **State is unbounded in principle.** One dict entry per source port, and the
  client uses a new port per attempt. For a run it is a handful; a long-lived
  use would want the entries reaped. Noted, not solved.

## Alternatives rejected

**A full TCP stack now.** The point of ADR 0086 was to gather evidence before
committing to that. The evidence is one segment; reading it needs a handshake,
not a stack.

**Refuse the SYN with a RST, to stop the retries cleanly.** A RST ends the
retries but reads nothing. The retries are cheap, and the opening bytes are the
whole point; accepting is what surfaces them.
