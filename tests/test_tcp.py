#!/usr/bin/env python3
"""The TCP endpoint, driven a segment at a time and checked at each step.

There is nothing to quote against a published vector here -- it is a state
machine, not a cipher -- so what is checked is the behaviour the transport
exists for: that it opens as the passive end, delivers a stream in order however
the segments arrive, reassembles across a gap without delivering it twice, holds
back to a window, resends what is not acknowledged and only then, and tears the
connection down from either side. The client half is written out by hand in each
test, so a bug in the endpoint cannot hide by being on both sides.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import tcp  # noqa: E402
from emu.tcp import ACK, FIN, PSH, RST, SYN, Connection  # noqa: E402

WINDOW = 64000


def _established(conn_iss: int = 5000, client_iss: int = 1000,
                 window: int = WINDOW, mss: int = 1460,
                 rcv_capacity: int = tcp.DEFAULT_RCV_CAPACITY) -> Connection:
    """A connection past the three-way handshake, with the client's next sequence
    number at client_iss+1 and ours at conn_iss+1."""
    conn = Connection(iss=conn_iss, mss=mss, rcv_capacity=rcv_capacity)
    conn.on_segment(client_iss, 0, SYN, window, b"", now=0)
    conn.on_segment(client_iss + 1, conn_iss + 1, ACK, window, b"", now=0)
    assert conn.established
    return conn


def _acks(out: list[tcp.Segment]) -> list[tcp.Segment]:
    return [s for s in out if s.flags & ACK]


def _data(out: list[tcp.Segment]) -> list[tcp.Segment]:
    return [s for s in out if s.payload]


# ---------------------------------------------------------------------- open

def test_a_syn_is_answered_with_a_syn_ack_that_offers_the_mss() -> None:
    conn = Connection(iss=5000, mss=1460)
    out = conn.on_segment(1000, 0, SYN, WINDOW, b"", now=0)
    assert len(out) == 1
    sa = out[0]
    assert sa.flags == SYN | ACK, "the answer to a SYN is a SYN-ACK"
    assert sa.seq == 5000 and sa.ack == 1001, "it acknowledges the SYN's octet"
    assert sa.mss == 1460, "the SYN-ACK carries the MSS option"
    assert sa.window == tcp.DEFAULT_RCV_CAPACITY, "and advertises the receive room"
    assert conn.state == tcp.SYN_RECEIVED


def test_the_final_ack_establishes_the_connection() -> None:
    conn = Connection(iss=5000)
    conn.on_segment(1000, 0, SYN, WINDOW, b"", now=0)
    out = conn.on_segment(1001, 5001, ACK, WINDOW, b"", now=1)
    assert conn.established
    assert out == [], "a bare ACK completing the open needs no reply"


def test_nothing_but_a_syn_opens_a_connection() -> None:
    conn = Connection(iss=5000)
    assert conn.on_segment(1000, 0, ACK, WINDOW, b"data", now=0) == []
    assert conn.state == tcp.LISTEN


# ------------------------------------------------------------------- receive

def test_in_order_data_is_delivered_and_acknowledged() -> None:
    conn = _established()
    out = conn.on_segment(1001, 5001, ACK | PSH, WINDOW, b"hello", now=2)
    assert conn.read() == b"hello"
    assert _acks(out)[-1].ack == 1006, "the ack covers the delivered octets"
    assert conn.read() == b"", "read leaves nothing behind"


def test_out_of_order_data_waits_for_the_gap_to_fill() -> None:
    conn = _established()
    # A segment five octets ahead of what is expected: a gap at 1001..1005.
    out = conn.on_segment(1006, 5001, ACK | PSH, WINDOW, b"WORLD", now=1)
    assert conn.read() == b"", "nothing is delivered while the gap is open"
    assert _acks(out)[-1].ack == 1001, "the ack still points at the gap"
    # Now the gap filler, in order.
    out = conn.on_segment(1001, 5001, ACK | PSH, WINDOW, b"01234", now=2)
    assert conn.read() == b"01234WORLD", "both are delivered, in order"
    assert _acks(out)[-1].ack == 1011, "the ack jumps past the reassembled run"


def test_already_received_data_is_not_delivered_twice() -> None:
    conn = _established()
    conn.on_segment(1001, 5001, ACK | PSH, WINDOW, b"hello", now=1)
    assert conn.read() == b"hello"
    # The whole segment again -- a retransmit the ack for crossed in flight.
    out = conn.on_segment(1001, 5001, ACK | PSH, WINDOW, b"hello", now=2)
    assert conn.read() == b"", "a full duplicate delivers nothing"
    assert _acks(out)[-1].ack == 1006
    # A segment that overlaps the end of what was received: only the tail is new.
    out = conn.on_segment(1004, 5001, ACK | PSH, WINDOW, b"loXYZ", now=3)
    assert conn.read() == b"XYZ", "only the octets past rcv_nxt are taken"
    assert _acks(out)[-1].ack == 1009


# ---------------------------------------------------------------------- send

def test_written_data_is_sent_within_the_mss() -> None:
    conn = _established(mss=4)
    conn.write(b"abcdefgh")
    segs = _data(conn.pump(now=1))
    assert [s.payload for s in segs] == [b"abcd", b"efgh"], "split at the MSS"
    assert segs[0].seq == 5001 and segs[1].seq == 5005, "sequence runs on"
    assert all(s.flags & PSH for s in segs)


def test_an_acknowledgement_frees_the_send_buffer() -> None:
    conn = _established()
    conn.write(b"world")
    conn.pump(now=1)
    assert conn.unacked, "the segment is held until acknowledged"
    conn.on_segment(1001, 5006, ACK, WINDOW, b"", now=2)
    assert conn.snd_una == 5006 and not conn.unacked, "the ack clears it"


def test_the_send_window_holds_data_back_until_it_opens() -> None:
    conn = _established(window=4)
    conn.write(b"abcdefgh")
    segs = _data(conn.pump(now=1))
    assert [s.payload for s in segs] == [b"abcd"], "only a window's worth goes"
    # Acknowledge the first four and advertise room for four more.
    out = conn.on_segment(1001, 5005, ACK, 4, b"", now=2)
    assert [s.payload for s in _data(out)] == [b"efgh"], "the rest goes as room opens"


# -------------------------------------------------------------- retransmit

def test_an_unacknowledged_segment_is_resent_after_the_timeout() -> None:
    conn = _established()
    conn.write(b"again")
    conn.pump(now=10)
    assert conn.on_tick(now=10 + tcp.RTO_MS - 1) == [], "not before the timer"
    resent = conn.on_tick(now=10 + tcp.RTO_MS)
    assert [s.payload for s in resent] == [b"again"], "resent at the timeout"


def test_a_retransmission_stops_once_the_data_is_acknowledged() -> None:
    conn = _established()
    conn.write(b"again")
    conn.pump(now=10)
    conn.on_segment(1001, 5006, ACK, WINDOW, b"", now=20)
    assert conn.on_tick(now=10 + 10 * tcp.RTO_MS) == [], "acknowledged, so silent"


def test_the_timeout_backs_off() -> None:
    conn = _established()
    conn.write(b"x")
    conn.pump(now=0)
    assert conn.rto == tcp.RTO_MS
    conn.on_tick(now=tcp.RTO_MS)
    assert conn.rto == 2 * tcp.RTO_MS, "each miss doubles the timer"


# ------------------------------------------------------------------- close

def test_the_peer_closing_is_answered_and_then_matched() -> None:
    conn = _established()
    # The client's FIN, in order.
    out = conn.on_segment(1001, 5001, ACK | FIN, WINDOW, b"", now=1)
    assert conn.state == tcp.CLOSE_WAIT
    assert _acks(out)[-1].ack == 1002, "the FIN's octet is acknowledged"
    # The service closes its half.
    conn.close()
    fins = [s for s in conn.pump(now=2) if s.flags & FIN]
    assert fins and conn.state == tcp.LAST_ACK
    # The client acknowledges our FIN.
    conn.on_segment(1002, 5002, ACK, WINDOW, b"", now=3)
    assert conn.closed


def test_the_service_can_close_first() -> None:
    conn = _established()
    conn.close()
    fins = [s for s in conn.pump(now=1) if s.flags & FIN]
    assert fins and conn.state == tcp.FIN_WAIT_1
    conn.on_segment(1001, 5002, ACK, WINDOW, b"", now=2)
    assert conn.state == tcp.FIN_WAIT_2, "our FIN acknowledged"
    out = conn.on_segment(1001, 5002, ACK | FIN, WINDOW, b"", now=3)
    assert conn.state == tcp.TIME_WAIT
    assert _acks(out)[-1].ack == 1002
    assert conn.on_tick(now=3 + 2 * tcp.MSL_MS) == [] and conn.closed


def test_a_simultaneous_close_goes_through_closing() -> None:
    conn = _established()
    conn.close()
    conn.pump(now=1)                     # our FIN, FIN_WAIT_1
    # The client's FIN before it acknowledges ours.
    conn.on_segment(1001, 5001, ACK | FIN, WINDOW, b"", now=2)
    assert conn.state == tcp.CLOSING
    conn.on_segment(1002, 5002, ACK, WINDOW, b"", now=3)
    assert conn.state == tcp.TIME_WAIT
    assert conn.on_tick(now=3 + 2 * tcp.MSL_MS) == [] and conn.closed


def test_a_reset_tears_the_connection_down_at_once() -> None:
    conn = _established()
    assert conn.on_segment(1001, 5001, RST, WINDOW, b"", now=1) == []
    assert conn.closed


def test_data_written_before_a_close_is_sent_before_the_fin() -> None:
    conn = _established()
    conn.write(b"bye")
    conn.close()
    out = conn.pump(now=1)
    kinds = [(bytes(s.payload), bool(s.flags & FIN)) for s in out]
    assert kinds == [(b"bye", False), (b"", True)], "data first, then the FIN"


# -------------------------------------------------- refused (CLOSED state)

def test_a_syn_to_a_closed_port_is_reset_and_its_octet_acknowledged() -> None:
    """RFC 9293 §3.10.7.1, no ACK on the segment: <SEQ=0><ACK=SEG.SEQ+SEG.LEN>
    <CTL=RST,ACK>, the SYN counting as one."""
    seg = tcp.refuse(1000, 0, SYN, 0)
    assert seg is not None and seg.flags == RST | ACK
    assert seg.seq == 0 and seg.ack == 1001


def test_a_segment_with_an_ack_is_reset_at_that_number() -> None:
    """With an ACK on it: <SEQ=SEG.ACK><CTL=RST>, so the sender accepts it."""
    seg = tcp.refuse(1000, 7777, ACK | PSH, 5)
    assert seg is not None and seg.flags == RST and seg.seq == 7777


def test_data_and_a_fin_without_an_ack_are_all_acknowledged() -> None:
    seg = tcp.refuse(1000, 0, FIN, 5)
    assert seg is not None and seg.ack == 1006, "five octets and the FIN"


def test_a_reset_is_never_answered_with_a_reset() -> None:
    assert tcp.refuse(1000, 0, RST, 0) is None
    assert tcp.refuse(1000, 5, RST | ACK, 0) is None


# ---------------------------------------------------- sequence-number wrap

def test_sequence_comparison_wraps() -> None:
    assert tcp._lt(0xFFFFFFFF, 0), "one before the wrap is less than zero after"
    assert not tcp._lt(0, 0xFFFFFFFF)
    assert tcp._leq(5, 5) and not tcp._lt(5, 5)
    assert tcp._lt(0xFFFFFF00, 0x100), "a small step across the boundary"


def test_the_send_side_wraps_past_2_to_the_32() -> None:
    conn = _established(conn_iss=0xFFFFFFFE, client_iss=1000)
    # snd_nxt is 0xFFFFFFFF after the SYN-ACK's octet was acknowledged.
    conn.write(b"hi")
    seg = _data(conn.pump(now=1))[0]
    assert seg.seq == 0xFFFFFFFF
    # Two octets from there lands at sequence 1, wrapped.
    conn.on_segment(1001, 1, ACK, WINDOW, b"", now=2)
    assert conn.snd_una == 1 and not conn.unacked, "the wrapped ack clears it"


def test_the_receive_side_wraps_past_2_to_the_32() -> None:
    conn = _established(conn_iss=5000, client_iss=0xFFFFFFF0)
    # rcv_nxt is 0xFFFFFFF1; twenty octets from there wraps to 5.
    out = conn.on_segment(0xFFFFFFF1, 5001, ACK | PSH, WINDOW, bytes(20), now=1)
    assert conn.read() == bytes(20)
    assert _acks(out)[-1].ack == 5, "the acknowledgement wraps with it"
