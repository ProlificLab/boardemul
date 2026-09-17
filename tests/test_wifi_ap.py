#!/usr/bin/env python3
"""The access-point peer, driven directly with frames -- no emulator.

The peer used to be exercised only end to end, under QEMU, which is minutes per
run (`test_wifi_connect`). Its frame handling is ordinary code, though, and can
be fed a frame and asked what it answers in milliseconds. This does that for the
TCP path wired in at ADR 0089: a station's segments go in as data frames, and
the peer's replies come back out to be parsed, so the transport's behaviour on
the air -- the SYN-ACK, the acknowledgements, the retransmission on a tick -- is
checked in the fast tier.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import tcp, wpa2  # noqa: E402
from emu.tcp import ACK, FIN, PSH, SYN  # noqa: E402
from emu.devices.wifi_ap import (  # noqa: E402
    AP_ADDRESS, CLIENT_IP, SERVER_IP, AccessPoint, Network, Station,
    tcp_segment,
)

STATION = bytes.fromhex("0254444d0001")


def _connected() -> AccessPoint:
    """A peer with a station already associated and its handshake armed, so a
    data frame from it is processed rather than dropped as pre-handshake. The
    beacon interval is set out of reach so a tick returns only what TCP owes."""
    point = AccessPoint(Network(ssid="x", channel=1, beacon_ms=10 ** 9,
                                wpa2=True, passphrase="p"))
    station = Station(STATION, authenticated=True, associated=True)
    station.shake = wpa2.Handshake(master=bytes(32), ap=AP_ADDRESS,
                                   station=STATION)
    point.stations[STATION] = station
    return point


def _sta_frame(ip: bytes) -> bytes:
    """A Protected data frame from the station to the access point, carrying an
    IPv4 packet: ToDS and Protected set, an eight-byte CCMP header (identity
    cipher, ADR 0084), then the LLC/SNAP header and the IPv4 ethertype."""
    head = (bytes([0x08, 0x41, 0x00, 0x00])          # data, ToDS+Protected
            + AP_ADDRESS + STATION + AP_ADDRESS       # addr1 BSSID, addr2 SA, addr3 DA
            + b"\x00\x00")                            # sequence control
    return head + bytes(8) + bytes.fromhex("aaaa03000000") + b"\x08\x00" + ip


def _segment(sport: int, dport: int, seq: int, ack: int, flags: int,
             payload: bytes = b"", window: int = 64000) -> bytes:
    """A station->peer IPv4/TCP segment, ready to go inside a data frame."""
    return tcp_segment(CLIENT_IP, SERVER_IP, sport, dport, seq, ack, flags,
                       window=window, payload=payload)


def _parse(frame: bytes) -> dict:
    """Pull the TCP fields back out of a peer->station data frame. Layout: 24 of
    header, 8 of CCMP, 6 of LLC, 2 of ethertype, then the IPv4 packet (trimmed to
    its own total length, past the device's trailing marker)."""
    ip = frame[40:]
    ip = ip[:int.from_bytes(ip[2:4], "big")]
    t = ip[(ip[0] & 0x0F) * 4:]
    off = (t[12] >> 4) * 4
    return {"sport": int.from_bytes(t[0:2], "big"),
            "dport": int.from_bytes(t[2:4], "big"),
            "seq": int.from_bytes(t[4:8], "big"),
            "ack": int.from_bytes(t[8:12], "big"),
            "flags": t[13], "payload": t[off:]}


def test_a_syn_is_answered_with_a_syn_ack_on_any_port() -> None:
    """Not just 8896: the port gate is gone (ADR 0089). A SYN to an arbitrary
    port opens a connection and draws a SYN-ACK acknowledging the SYN's octet."""
    point = _connected()
    air = point.take(_sta_frame(_segment(5000, 9999, 100, 0, SYN)), 1, 1000)
    assert air is not None, "a SYN was not answered"
    r = _parse(air.frame)
    assert r["flags"] == SYN | ACK
    assert (r["sport"], r["dport"]) == (9999, 5000), "ports mirrored"
    assert r["ack"] == 101, "the SYN's octet is acknowledged"


def test_a_full_exchange_is_carried_and_the_opening_observed() -> None:
    point = _connected()
    syn_ack = _parse(point.take(_sta_frame(_segment(5000, 8896, 100, 0, SYN)),
                                1, 1000).frame)
    isn = syn_ack["seq"]
    # The client's ACK completes the open; nothing need come back.
    assert point.take(_sta_frame(_segment(5000, 8896, 101, isn + 1, ACK)),
                      1, 1001) is None
    # Then it sends its opening, which is acknowledged and observed.
    air = point.take(
        _sta_frame(_segment(5000, 8896, 101, isn + 1, ACK | PSH, b"hello")),
        1, 1002)
    r = _parse(air.frame)
    assert r["flags"] & ACK and r["ack"] == 106, "the data is acknowledged"
    assert point.counts.get("tcp opening on 8896") == 1, "the opening was seen"


def test_a_fin_is_acknowledged() -> None:
    point = _connected()
    isn = _parse(point.take(_sta_frame(_segment(5000, 80, 100, 0, SYN)),
                            1, 1000).frame)["seq"]
    point.take(_sta_frame(_segment(5000, 80, 101, isn + 1, ACK)), 1, 1001)
    air = point.take(_sta_frame(_segment(5000, 80, 101, isn + 1, ACK | FIN)),
                     1, 1002)
    r = _parse(air.frame)
    assert r["flags"] & ACK and r["ack"] == 102, "the FIN's octet is acknowledged"


def test_an_unanswered_syn_ack_is_retransmitted_on_a_tick() -> None:
    """The SYN-ACK went out but no ACK came; the retransmission timer, run on the
    guest clock a tick brings, resends it -- and not before it is due."""
    point = _connected()
    point.take(_sta_frame(_segment(7000, 80, 1, 0, SYN)), 1, 5000)
    assert point.tick(1, 5000 + tcp.RTO_MS - 1) is None, "not before the timeout"
    air = point.tick(1, 5000 + tcp.RTO_MS)
    assert air is not None and _parse(air.frame)["flags"] == SYN | ACK, \
        "the SYN-ACK is resent once the timer fires"
