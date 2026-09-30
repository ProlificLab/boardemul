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

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import ccmp, tcp, wpa2  # noqa: E402
from emu.tcp import ACK, FIN, PSH, SYN  # noqa: E402
from emu.devices.wifi_ap import (  # noqa: E402
    AP_ADDRESS, CLIENT_IP, FIRST_RETRY_DELIVERED_MS, FIRST_RETRY_MS, SERVER_IP,
    AccessPoint, Network, Station, tcp_segment, udp4_packet,
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


def _sta_frame(ip: bytes, src: bytes = STATION) -> bytes:
    """A Protected data frame from a station to the access point, carrying an
    IPv4 packet: ToDS and Protected set, an eight-byte CCMP header (identity
    cipher, ADR 0084), then the LLC/SNAP header and the IPv4 ethertype."""
    head = (bytes([0x08, 0x41, 0x00, 0x00])          # data, ToDS+Protected
            + AP_ADDRESS + src + AP_ADDRESS           # addr1 BSSID, addr2 SA, addr3 DA
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


def _dns_query(name: str, txn: bytes = b"\xab\xcd") -> bytes:
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return txn + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0) + qname + b"\x00\x01\x00\x01"


def _udp_frame(dst_port: int, payload: bytes, src_port: int = 5000,
               src: bytes = STATION, src_ip: bytes = CLIENT_IP) -> bytes:
    return _sta_frame(udp4_packet(src_ip, SERVER_IP, src_port, dst_port, payload),
                      src=src)


def _dhcp_discover(mac: bytes, message_type: int = 1,
                   xid: bytes = b"\x11\x22\x33\x44") -> bytes:
    bootp = bytes([1, 1, 6, 0]) + xid + struct.pack(">HH", 0, 0) + bytes(16)
    bootp += mac + bytes(16 - len(mac)) + bytes(64) + bytes(128)
    return bootp + bytes.fromhex("63825363") + bytes([53, 1, message_type, 255])


def _seat(point: AccessPoint, mac: bytes) -> None:
    """Put an associated station with an armed handshake on the peer."""
    station = Station(mac, authenticated=True, associated=True)
    station.shake = wpa2.Handshake(master=bytes(32), ap=AP_ADDRESS, station=mac)
    point.stations[mac] = station


def _sta_encrypted(tk: bytes, ip: bytes, pn: int = 1, src: bytes = STATION) -> bytes:
    """A Protected data frame from the station whose body is really CCMP-encrypted
    with `tk`, the way the MAC model seals it before it reaches the air."""
    head = bytes([0x08, 0x41, 0, 0]) + AP_ADDRESS + src + AP_ADDRESS + b"\x00\x00"
    ccmp_header = ccmp.build_header(pn)
    body = bytes.fromhex("aaaa03000000") + b"\x08\x00" + ip
    return head + ccmp_header + ccmp.encrypt(tk, head, ccmp_header, body)


def _ip_udp_payload(ip: bytes) -> bytes:
    ip = ip[:int.from_bytes(ip[2:4], "big")]
    udp = ip[(ip[0] & 0x0F) * 4:]
    return udp[8:int.from_bytes(udp[4:6], "big")]


def test_a_connected_station_is_answered_over_real_ccmp() -> None:
    """With a key in place (ADR 0094), the station's frame is encrypted, the peer
    decrypts and verifies it, and its reply comes back encrypted -- the whole
    exchange over real CCMP, not the identity stand-in."""
    point = _connected()
    tk = bytes(range(16))
    point.stations[STATION].tk = tk                  # the shared pairwise key
    ip = udp4_packet(CLIENT_IP, SERVER_IP, 68, 67, _dhcp_discover(STATION))
    air = point.take(_sta_encrypted(tk, ip), 1, 100)
    assert air is not None, "the encrypted DISCOVER was not answered"

    reply = air.frame
    body = ccmp.decrypt(tk, reply[:24], reply[24:32], reply[32:])
    assert body is not None, "the reply did not decrypt or its MIC failed"
    assert body[:6] == bytes.fromhex("aaaa03000000"), "an LLC/SNAP header"
    bootp = _ip_udp_payload(body[8:])
    assert bootp[16:20] == bytes([192, 168, 4, 2]), "the offered address"


def test_a_forged_frame_is_dropped() -> None:
    """A Protected frame that does not decrypt under the key is not read: a bit
    flipped in the ciphertext must make the peer drop it, not parse garbage."""
    point = _connected()
    tk = bytes(range(16))
    point.stations[STATION].tk = tk
    ip = udp4_packet(CLIENT_IP, SERVER_IP, 68, 67, _dhcp_discover(STATION))
    frame = bytearray(_sta_encrypted(tk, ip))
    frame[40] ^= 1                                   # flip a ciphertext octet
    assert point.take(bytes(frame), 1, 100) is None, "a bad MIC was not dropped"
    assert point.counts.get("data frame failed its MIC") == 1


def test_two_stations_each_get_their_own_address() -> None:
    """The one-station shortcut is gone (ADR 0091): a DISCOVER from a second
    station's hardware address is leased a different address from the first."""
    point = _connected()                             # STATION already seated
    second = bytes.fromhex("0254444d0002")
    _seat(point, second)

    def offer(mac: bytes) -> bytes:
        frame = _udp_frame(67, _dhcp_discover(mac), src_port=68, src=mac,
                           src_ip=bytes(4))
        return _udp_payload(point.take(frame, 1, 100).frame)[16:20]  # yiaddr

    first_ip, second_ip = offer(STATION), offer(second)
    assert first_ip == bytes([192, 168, 4, 2]), "the first lease is .2"
    assert second_ip == bytes([192, 168, 4, 3]), "the second is a different one"


def _udp_payload(frame: bytes) -> bytes:
    ip = frame[40:]
    ip = ip[:int.from_bytes(ip[2:4], "big")]
    udp = ip[(ip[0] & 0x0F) * 4:]
    return udp[8:int.from_bytes(udp[4:6], "big")]


def test_a_held_name_resolves_and_an_unknown_one_is_refused() -> None:
    """The faithful resolver reaches the wire (ADR 0090): a hosted name comes
    back NOERROR with the gateway, and a name outside the zone comes back
    NXDOMAIN -- not, as the captive resolver did, the gateway again."""
    point = _connected()
    held = _udp_payload(point.take(_udp_frame(53, _dns_query("lilygo.local")),
                                   1, 2000).frame)
    assert int.from_bytes(held[2:4], "big") & 0x0F == 0, "a held name is NOERROR"

    air = point.take(_udp_frame(53, _dns_query("example.com")), 1, 2001)
    refused = _udp_payload(air.frame)
    assert int.from_bytes(refused[2:4], "big") & 0x0F == 3, "an unknown one is NXDOMAIN"
    assert point.counts.get("dns nxdomain") == 1


def _owing_message_one() -> AccessPoint:
    """A peer with a station just associated, owing it message one."""
    point = _connected()
    station = point.stations[STATION]
    station.owes_first = True
    station.associated_at = 0
    station.first_at = 0
    return point


def _is_message_one(air) -> bool:
    return (air is not None and air.frame[0] == 0x08
            and bytes.fromhex("aaaa03000000888e") in air.frame)


def test_an_undelivered_message_one_is_retried_on_the_short_timer() -> None:
    """This radio loses offers the air would not (ADR 0067), so a message one
    not known to have arrived is offered again after FIRST_RETRY_MS."""
    point = _owing_message_one()
    assert _is_message_one(point.tick(1, 1000))
    assert point.tick(1, 1000 + FIRST_RETRY_MS - 1) is None
    assert _is_message_one(point.tick(1, 1000 + FIRST_RETRY_MS))


def test_a_delivered_message_one_waits_the_long_timer() -> None:
    """Once the device says message one reached the station, it is not offered
    again on the short timer: hostapd's rule for an acknowledged message one,
    and the fix for the station that skipped message four (ADR 0095)."""
    point = _owing_message_one()
    assert _is_message_one(point.tick(1, 1000))
    point.delivered()
    assert point.tick(1, 1000 + FIRST_RETRY_MS) is None, "retried too soon"
    assert point.tick(1, 1000 + FIRST_RETRY_DELIVERED_MS - 1) is None
    assert _is_message_one(point.tick(1, 1000 + FIRST_RETRY_DELIVERED_MS))
    assert point.counts.get("eapol 1 delivered") == 1


def test_a_delivery_after_something_else_is_not_pinned_on_message_one() -> None:
    """The acknowledgement is for the last frame offered. If that was not a
    message one -- here, nothing was offered on the tick -- it changes nothing."""
    point = _owing_message_one()
    assert _is_message_one(point.tick(1, 1000))
    assert point.tick(1, 1050) is None           # nothing offered on this tick
    point.delivered()
    assert _is_message_one(point.tick(1, 1000 + FIRST_RETRY_MS))


def test_an_unanswered_syn_ack_is_retransmitted_on_a_tick() -> None:
    """The SYN-ACK went out but no ACK came; the retransmission timer, run on the
    guest clock a tick brings, resends it -- and not before it is due."""
    point = _connected()
    point.take(_sta_frame(_segment(7000, 80, 1, 0, SYN)), 1, 5000)
    assert point.tick(1, 5000 + tcp.RTO_MS - 1) is None, "not before the timeout"
    air = point.tick(1, 5000 + tcp.RTO_MS)
    assert air is not None and _parse(air.frame)["flags"] == SYN | ACK, \
        "the SYN-ACK is resent once the timer fires"
