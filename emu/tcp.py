#!/usr/bin/env python3
"""A TCP endpoint: the transport, with nothing above it and nothing below.

The peer used to accept a connection and read the first thing said on it, and no
more (ADR 0087). This is the real thing instead (ADR 0089): a connection that
opens, carries a stream in both directions with a send side that retransmits and
a receive side that reassembles, obeys a flow-control window, and is taken down
in order.

It is deliberately just the transport. It knows nothing about IP, about frames,
about the air, or about what the bytes mean -- it takes the fields of a segment
in and hands segments to send back out, as plain values, so it can be driven and
checked without any of that (the way `wpa2` is). The peer serialises a `Segment`
into an IPv4/TCP datagram and a data frame; a service reads the stream with
`read`, answers with `write`, and ends it with `close`.

Two things shape the design. First, this is always the *passive* opener: the
firmware connects to us, so a connection begins in LISTEN and the first thing it
ever sees is a SYN. Second, the peer can put only one frame on the air per guest
event (ADR 0064), so nothing here sends on its own -- every method returns the
segments to send and the caller decides when. The guest's millisecond clock,
passed in on every call, is the only clock there is; the retransmission timer
runs on it.

The state names and the rules are TCP's own (RFC 9293). What is not modelled is
said so in the code: no urgent data, no options past the MSS on the SYN, no
window scaling or SACK, and a TIME-WAIT that is a token wait, not two minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Flag bits, in the position they sit in the TCP header's byte.
FIN = 0x01
SYN = 0x02
RST = 0x04
PSH = 0x08
ACK = 0x10

# The states of the connection, as RFC 9293 names them. LISTEN and the passive
# path (SYN_RECEIVED, ESTABLISHED, CLOSE_WAIT, LAST_ACK) are the ones the
# firmware drives; the active-close path (FIN_WAIT_1/2, CLOSING, TIME_WAIT) is
# here so that a service closing first is also correct.
CLOSED = "CLOSED"
LISTEN = "LISTEN"
SYN_RECEIVED = "SYN_RECEIVED"
ESTABLISHED = "ESTABLISHED"
CLOSE_WAIT = "CLOSE_WAIT"
LAST_ACK = "LAST_ACK"
FIN_WAIT_1 = "FIN_WAIT_1"
FIN_WAIT_2 = "FIN_WAIT_2"
CLOSING = "CLOSING"
TIME_WAIT = "TIME_WAIT"

# Defaults. The MSS the SYN-ACK offers, the receive buffer the window is measured
# against, and the retransmission timer: half a second to start, doubling on each
# miss up to four, which is where a real stack lands too. MSL is a token here --
# a deterministic simulation gains nothing from a real two-minute TIME-WAIT, but
# passing through the state at all keeps the teardown honest.
DEFAULT_MSS = 1460
DEFAULT_RCV_CAPACITY = 8192
RTO_MS = 500
RTO_MAX_MS = 4000
MSL_MS = 1000


def _sub(a: int, b: int) -> int:
    """(a - b) reduced into a 32-bit sequence number."""
    return (a - b) & 0xFFFFFFFF


def _leq(a: int, b: int) -> bool:
    """a <= b in sequence space (RFC 1982): the shorter way round the circle.

    Sequence numbers wrap at 2**32, so "less than" is not the integers' -- it is
    which of the two is a small step ahead of the other. b - a landing in the
    low half means a is at or behind b.
    """
    return _sub(b, a) < 0x80000000


def _lt(a: int, b: int) -> bool:
    return a != b and _leq(a, b)


@dataclass
class Segment:
    """One segment to put on the wire, as fields rather than bytes.

    `mss`, when set, is the maximum-segment-size option carried on a SYN; it is
    the only option this endpoint ever sends. The caller fills in the addresses
    and ports and turns this into a datagram.
    """
    seq: int
    ack: int
    flags: int
    window: int
    payload: bytes = b""
    mss: int | None = None

    def seq_len(self) -> int:
        """How much of the sequence space this segment occupies: its data, plus
        one each for a SYN or a FIN, which are counted as if they were a byte so
        that they can be acknowledged."""
        return len(self.payload) + (1 if self.flags & SYN else 0) \
            + (1 if self.flags & FIN else 0)


@dataclass
class _Sent:
    """A segment that has gone out and not yet been acknowledged, with when it
    was last sent so the timer can tell it has waited too long."""
    seg: Segment
    sent_at: int


class Connection:
    """One TCP connection, driven entirely from the outside.

    The caller feeds it inbound segments with `on_segment`, gives it the clock on
    `on_tick` so its timer can fire, reads the received stream with `read`, sends
    on it with `write`, and ends it with `close`. Every one of those returns the
    segments to put on the air, in order; the caller sends them (one per guest
    event) and never has to reach inside.
    """

    def __init__(self, iss: int, mss: int = DEFAULT_MSS,
                 rcv_capacity: int = DEFAULT_RCV_CAPACITY):
        self.state = LISTEN
        self.mss = mss
        self.rcv_capacity = rcv_capacity

        # Send side. iss is our initial sequence number; the SYN sits on it. Data
        # the service has queued but not yet segmented waits in send_buf; segments
        # that have been sent and not acknowledged wait, in order, in unacked.
        self.iss = iss
        self.snd_una = iss
        self.snd_nxt = iss
        self.snd_wnd = 0
        self.send_buf = bytearray()
        self.unacked: list[_Sent] = []
        self._closing = False           # the service has asked to close
        self._fin_sent = False

        # Receive side. rcv_nxt is the next sequence number expected; recv_buf is
        # the in-order stream waiting for the service to read; ooo holds segments
        # that arrived ahead of a gap, by their sequence number.
        self.irs = 0                    # the peer's initial sequence number
        self.rcv_nxt = 0
        self.recv_buf = bytearray()
        self.ooo: dict[int, bytes] = {}
        self._peer_fin_seq: int | None = None   # where the peer's FIN sits, once known

        # Timers. rto grows on each retransmission and resets when fresh data is
        # acknowledged; time_wait_until ends the TIME-WAIT state.
        self.rto = RTO_MS
        self.time_wait_until: int | None = None

    # ------------------------------------------------------------------ state

    @property
    def established(self) -> bool:
        return self.state == ESTABLISHED

    @property
    def closed(self) -> bool:
        return self.state == CLOSED

    def _rcv_wnd(self) -> int:
        """What to advertise: the room left in the receive buffer. A service that
        reads promptly keeps this near the capacity; if it stopped reading, this
        would fall to zero and the peer would stop sending, which is the point."""
        return max(0, self.rcv_capacity - len(self.recv_buf))

    def _ack(self, extra_flags: int = 0) -> Segment:
        """A pure acknowledgement of everything received so far."""
        return Segment(seq=self.snd_nxt, ack=self.rcv_nxt,
                       flags=ACK | extra_flags, window=self._rcv_wnd())

    # ---------------------------------------------------------------- service

    def read(self) -> bytes:
        """Take the in-order bytes received so far, leaving none behind."""
        data = bytes(self.recv_buf)
        self.recv_buf.clear()
        return data

    def write(self, data: bytes) -> None:
        """Queue data to send. It goes out on the next `on_segment`/`on_tick`,
        segmented to the window and the MSS; a caller that wants it now calls
        `pump`."""
        self.send_buf += data

    def close(self) -> None:
        """Ask to close once everything written has been sent and acknowledged.
        The FIN it turns into occupies a sequence number of its own."""
        self._closing = True

    # ------------------------------------------------------------- the engine

    def on_segment(self, seq: int, ack: int, flags: int, window: int,
                   payload: bytes, now: int) -> list[Segment]:
        """Process one inbound segment and return what to send in reply."""
        if flags & RST:
            # The peer has torn it down; drop everything and go quiet.
            self.state = CLOSED
            return []

        out: list[Segment] = []

        if self.state == LISTEN:
            if not (flags & SYN):
                return []                       # nothing else opens a connection
            self.irs = seq
            self.rcv_nxt = _sub(seq + 1, 0)     # the SYN takes one
            self.snd_wnd = window
            syn_ack = Segment(seq=self.iss, ack=self.rcv_nxt, flags=SYN | ACK,
                              window=self._rcv_wnd(), mss=self.mss)
            self.unacked.append(_Sent(syn_ack, now))
            self.snd_nxt = _sub(self.iss + 1, 0)
            self.state = SYN_RECEIVED
            return [syn_ack]

        # An old duplicate SYN, or a SYN-ACK we would never get as the passive
        # end: acknowledge what we have and move on.
        if flags & SYN and self.state != SYN_RECEIVED:
            out.append(self._ack())

        # Acknowledgement first: it frees the send side and can complete the
        # open or the close.
        if flags & ACK:
            self._process_ack(ack, window, now)

        # Then the payload and any FIN, which may produce an acknowledgement.
        if payload or (flags & FIN):
            out += self._process_incoming(seq, flags, payload, now)

        # A window may have opened, or data may now be sendable; and a close that
        # was waiting on the send buffer emptying can now send its FIN.
        out += self.pump(now)
        return _coalesce(out)

    def _process_ack(self, ack: int, window: int, now: int) -> None:
        """Advance the send side by an acknowledgement."""
        if _lt(self.snd_nxt, ack):
            # Acks something we have not sent; ignore the number, take the window.
            self.snd_wnd = window
            return
        advanced = _lt(self.snd_una, ack)
        if advanced:
            self.snd_una = ack
        self.snd_wnd = window

        # Drop every segment the ack now covers in full.
        kept: list[_Sent] = []
        for s in self.unacked:
            end = _sub(s.seg.seq + s.seg.seq_len(), 0)
            if _leq(end, ack):
                continue                        # fully acknowledged
            kept.append(s)
        self.unacked = kept

        if advanced:
            self.rto = RTO_MS                    # progress resets the backoff
            if self.unacked:
                self.unacked[0].sent_at = now    # restart the timer on what is left

        # State transitions the acknowledgement drives.
        if self.state == SYN_RECEIVED and not any(
                s.seg.flags & SYN for s in self.unacked):
            self.state = ESTABLISHED
        if self._fin_sent and not any(s.seg.flags & FIN for s in self.unacked):
            # Our FIN has been acknowledged.
            if self.state == FIN_WAIT_1:
                self.state = FIN_WAIT_2
            elif self.state == CLOSING:
                self._enter_time_wait(now)
            elif self.state == LAST_ACK:
                self.state = CLOSED

    def _process_incoming(self, seq: int, flags: int, payload: bytes,
                          now: int) -> list[Segment]:
        """Take a segment's payload and FIN into the receive stream, reassembling
        across a gap, and return the acknowledgement it earns."""
        if payload:
            if seq == self.rcv_nxt:
                self._accept(payload, seq)
                self._drain_ooo(now)
            elif _lt(self.rcv_nxt, seq):
                # Ahead of a gap: keep it until the gap is filled. Bounded by the
                # advertised window, so a peer that respects the window cannot
                # make this grow without limit.
                if _lt(_sub(seq + len(payload), 0),
                       _sub(self.rcv_nxt + self.rcv_capacity + 1, 0)):
                    self.ooo[seq] = payload
            else:
                # Behind rcv_nxt: some or all already received. Take any tail that
                # is new and in order.
                overlap = _sub(self.rcv_nxt, seq)
                if overlap < len(payload):
                    self._accept(payload[overlap:], self.rcv_nxt)
                    self._drain_ooo(now)

        if flags & FIN:
            # The FIN sits after this segment's payload. Honour it only once the
            # stream has reached it, so a FIN that overtook a lost segment waits.
            fin_seq = _sub(seq + len(payload), 0)
            self._peer_fin_seq = fin_seq
            if fin_seq == self.rcv_nxt:
                self._accept_fin(fin_seq, now)

        return [self._ack()]

    def _accept(self, data: bytes, at: int) -> None:
        """Append in-order data and advance rcv_nxt."""
        self.recv_buf += data
        self.rcv_nxt = _sub(at + len(data), 0)

    def _accept_fin(self, fin_seq: int, now: int) -> None:
        """Consume the peer's FIN and take the state one step towards closed."""
        self.rcv_nxt = _sub(fin_seq + 1, 0)
        self._peer_fin_seq = None
        if self.state == ESTABLISHED:
            self.state = CLOSE_WAIT
        elif self.state == FIN_WAIT_1:
            self.state = CLOSING            # our FIN not yet acknowledged
        elif self.state == FIN_WAIT_2:
            self._enter_time_wait(now)

    def _drain_ooo(self, now: int) -> None:
        """After accepting in-order data, take any buffered segment that is now
        contiguous, and the FIN if the stream has reached it."""
        while True:
            seg = self.ooo.pop(self.rcv_nxt, None)
            if seg is None:
                break
            self._accept(seg, self.rcv_nxt)
        if self._peer_fin_seq is not None and self._peer_fin_seq == self.rcv_nxt:
            self._accept_fin(self._peer_fin_seq, now)

    def _enter_time_wait(self, now: int) -> None:
        self.state = TIME_WAIT
        self.time_wait_until = now + 2 * MSL_MS

    # ------------------------------------------------------------- sending

    def pump(self, now: int) -> list[Segment]:
        """Send whatever can be sent now: new data within the window and the MSS,
        then the FIN if a close is pending and the buffer has drained."""
        out: list[Segment] = []
        if self.state not in (ESTABLISHED, CLOSE_WAIT):
            return out

        # Outstanding is what the peer has not acknowledged; the window caps the
        # sum of that and anything new.
        while self.send_buf:
            outstanding = _sub(self.snd_nxt, self.snd_una)
            room = self.snd_wnd - outstanding
            if room <= 0:
                break
            take = min(len(self.send_buf), self.mss, room)
            chunk = bytes(self.send_buf[:take])
            del self.send_buf[:take]
            seg = Segment(seq=self.snd_nxt, ack=self.rcv_nxt, flags=ACK | PSH,
                          window=self._rcv_wnd(), payload=chunk)
            self._send_new(seg, now)
            out.append(seg)

        if self._closing and not self.send_buf and not self._fin_sent:
            # Everything written has been segmented; the FIN can go.
            seg = Segment(seq=self.snd_nxt, ack=self.rcv_nxt, flags=ACK | FIN,
                          window=self._rcv_wnd())
            self._send_new(seg, now)
            self._fin_sent = True
            self.state = LAST_ACK if self.state == CLOSE_WAIT else FIN_WAIT_1
            out.append(seg)
        return out

    def _send_new(self, seg: Segment, now: int) -> None:
        """Record a freshly sent segment and advance snd_nxt."""
        self.unacked.append(_Sent(seg, now))
        self.snd_nxt = _sub(self.snd_nxt + seg.seq_len(), 0)

    def on_tick(self, now: int) -> list[Segment]:
        """The clock advanced. Retransmit the oldest unacknowledged segment if it
        has waited longer than the timer, send anything newly sendable, and end
        the TIME-WAIT once it is up."""
        out: list[Segment] = []
        if self.state == TIME_WAIT and self.time_wait_until is not None \
                and now >= self.time_wait_until:
            self.state = CLOSED
            return out

        if self.unacked:
            oldest = self.unacked[0]
            if now - oldest.sent_at >= self.rto:
                # Go back to the first unacknowledged segment and resend it, its
                # ack and window refreshed. One segment per timeout is enough
                # here: the window is small and the air rarely loses ours.
                oldest.sent_at = now
                oldest.seg.ack = self.rcv_nxt
                oldest.seg.window = self._rcv_wnd()
                self.rto = min(self.rto * 2, RTO_MAX_MS)
                out.append(oldest.seg)

        out += self.pump(now)
        return out


def _coalesce(segments: list[Segment]) -> list[Segment]:
    """Drop a bare duplicate acknowledgement that a later segment already carries.

    Several steps of `on_segment` each want to acknowledge; when a real segment
    (data, SYN or FIN) is going out anyway it carries the same ack, so a pure-ACK
    with nothing else in front of it is redundant. Keeping the last of any run of
    bare acks, and every segment that carries payload or a flag, is enough.
    """
    out: list[Segment] = []
    for seg in segments:
        bare = seg.flags == ACK and not seg.payload
        if bare and out and out[-1].flags == ACK and not out[-1].payload:
            out[-1] = seg                       # collapse consecutive bare acks
            continue
        out.append(seg)
    # If a real segment carries the same ack as a trailing bare ack, the bare one
    # adds nothing.
    if len(out) >= 2 and out[-1].flags == ACK and not out[-1].payload:
        if out[-2].flags & (SYN | FIN) or out[-2].payload:
            if out[-2].ack == out[-1].ack:
                out.pop()
    return out
