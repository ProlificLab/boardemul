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


# As the vendor firmware sent it to lilygo.local:8896 (ADR 0096).
REPORT = (b'{\n\t"SKU":\t"NA",\n\t"product":\t"T-Deck",\n\t"mac":\t'
          b'"02:54:44:4D:00:01",\n\t"date":\t"2026-10-01,00:21"\n}')


def _open(point: AccessPoint, port: int = 8896, cport: int = 5000) -> int:
    """Complete a three-way handshake to the peer; return its initial sequence."""
    isn = _parse(point.take(_sta_frame(_segment(cport, port, 100, 0, SYN)),
                            1, 1000).frame)["seq"]
    point.take(_sta_frame(_segment(cport, port, 101, isn + 1, ACK)), 1, 1001)
    return isn


def test_a_syn_to_the_factory_port_is_answered_with_a_syn_ack() -> None:
    point = _connected()
    air = point.take(_sta_frame(_segment(5000, 8896, 100, 0, SYN)), 1, 1000)
    assert air is not None, "a SYN was not answered"
    r = _parse(air.frame)
    assert r["flags"] == SYN | ACK
    assert (r["sport"], r["dport"]) == (8896, 5000), "ports mirrored"
    assert r["ack"] == 101, "the SYN's octet is acknowledged"


def test_a_syn_to_a_port_nothing_serves_is_refused() -> None:
    """A host with no listener resets the connection rather than completing
    it (ADR 0096): RST, acknowledging the SYN so the client accepts it."""
    point = _connected()
    air = point.take(_sta_frame(_segment(5000, 9999, 100, 0, SYN)), 1, 1000)
    r = _parse(air.frame)
    assert r["flags"] == tcp.RST | ACK and r["ack"] == 101
    assert not point.conns, "no connection is kept for a refused port"


def test_the_report_is_registered_and_nothing_is_sent_back() -> None:
    """The factory-test service takes the report, checks the MAC against the
    address the frames came from, and writes nothing: the device reads none."""
    point = _connected()
    isn = _open(point)
    air = point.take(
        _sta_frame(_segment(5000, 8896, 101, isn + 1, ACK | PSH, REPORT)), 1, 1002)
    r = _parse(air.frame)
    assert r["flags"] == ACK and not r["payload"], "only an acknowledgement"
    assert r["ack"] == 101 + len(REPORT)
    assert [(x.product, x.mac) for x in point.registrations] == [
        ("T-Deck", "02:54:44:4D:00:01")]
    assert point.tick(1, 1003) is None, "and nothing follows it"


def test_a_report_split_across_segments_is_registered_once_whole() -> None:
    point = _connected()
    isn = _open(point)
    half = len(REPORT) // 2
    point.take(_sta_frame(_segment(5000, 8896, 101, isn + 1, ACK | PSH,
                                   REPORT[:half])), 1, 1002)
    assert not point.registrations, "half a report is not a report"
    point.take(_sta_frame(_segment(5000, 8896, 101 + half, isn + 1, ACK | PSH,
                                   REPORT[half:])), 1, 1003)
    assert len(point.registrations) == 1


def test_the_service_closes_when_the_client_does() -> None:
    """The client's FIN is acknowledged and answered with the peer's own FIN,
    so the connection ends in order rather than waiting in CLOSE_WAIT."""
    point = _connected()
    isn = _open(point)
    air = point.take(_sta_frame(_segment(5000, 8896, 101, isn + 1, ACK | FIN)),
                     1, 1002)
    r = _parse(air.frame)
    assert r["flags"] & ACK and r["ack"] == 102, "the FIN's octet is acknowledged"
    fin = _parse(point.tick(1, 1003).frame)
    assert fin["flags"] & FIN, "the service closes its half too"
    point.take(_sta_frame(_segment(5000, 8896, 102, fin["seq"] + 1, ACK)), 1, 1004)
    assert not point.conns, "and once acknowledged the connection is gone"


def test_a_segment_for_a_forgotten_connection_is_reset() -> None:
    point = _connected()
    air = point.take(_sta_frame(_segment(5000, 8896, 500, 9000, ACK | PSH, b"x")),
                     1, 1000)
    r = _parse(air.frame)
    assert r["flags"] == tcp.RST and r["seq"] == 9000


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
    point.take(_sta_frame(_segment(7000, 8896, 1, 0, SYN)), 1, 5000)
    assert point.tick(1, 5000 + tcp.RTO_MS - 1) is None, "not before the timeout"
    air = point.tick(1, 5000 + tcp.RTO_MS)
    assert air is not None and _parse(air.frame)["flags"] == SYN | ACK, \
        "the SYN-ACK is resent once the timer fires"


# ------------------------------------------------------------- the uplink
# ADR 0097. The internet is played by servers on localhost, which the guest
# addresses as 127.0.0.1 -- not the LAN, so the gateway routes it out.

import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from emu.uplink import Uplink  # noqa: E402

LOOPBACK = bytes([127, 0, 0, 1])


@pytest.fixture
def uplinked():
    up = Uplink()
    up.start()
    point = AccessPoint(Network(ssid="x", channel=1, beacon_ms=10 ** 9,
                                wpa2=True, passphrase="p"), uplink=up)
    _seat(point, STATION)
    yield point
    up.stop()


def _udp_to(dst_ip: bytes, dport: int, payload: bytes, sport: int = 5000) -> bytes:
    return _sta_frame(udp4_packet(CLIENT_IP, dst_ip, sport, dport, payload))


def _tcp_to(dst_ip: bytes, dport: int, seq: int, ack: int, flags: int,
            payload: bytes = b"", sport: int = 6000) -> bytes:
    return _sta_frame(tcp_segment(CLIENT_IP, dst_ip, sport, dport, seq, ack,
                                  flags, window=64000, payload=payload))


def _src_ip(frame: bytes) -> bytes:
    return frame[40 + 12:40 + 16]


def _until(point: AccessPoint, want, timeout: float = 5.0):
    """Tick until a frame `want` accepts goes out; the uplink answers late."""
    clock, deadline = 10_000, time.monotonic() + timeout
    while time.monotonic() < deadline:
        clock += 10
        air = point.tick(1, clock)
        if air is not None and want(air.frame):
            return air.frame
        if air is None:
            time.sleep(0.01)
    raise AssertionError(f"nothing wanted went out; counts {point.counts}")


def _listen_tcp(handler) -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            handler(conn)
    threading.Thread(target=serve, daemon=True).start()
    return listener, listener.getsockname()[1]


def test_without_an_uplink_a_foreign_address_is_not_answered_locally() -> None:
    """Offline, a DNS query sent to someone else's resolver is unroutable -- not
    answered by the gateway's own resolver as if it were that host."""
    point = _connected()
    assert point.take(_udp_to(bytes([8, 8, 8, 8]), 53, _dns_query("lilygo.local")),
                      1, 100) is None
    assert point.counts.get("ip with no route (no uplink)") == 1


def test_a_datagram_goes_out_and_the_answer_comes_back_from_the_remote(uplinked) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    threading.Thread(target=lambda: (lambda d, a: server.sendto(d[::-1], a))(
        *server.recvfrom(2048)), daemon=True).start()
    try:
        assert uplinked.take(_udp_to(LOOPBACK, port, b"abc"), 1, 100) is None
        frame = _until(uplinked, lambda f: _src_ip(f) == LOOPBACK)
        ip = frame[40:]
        udp = ip[20:]
        assert int.from_bytes(udp[0:2], "big") == port, "from the remote port"
        assert int.from_bytes(udp[2:4], "big") == 5000, "to the guest's port"
        assert _udp_payload(frame) == b"cba"
    finally:
        server.close()


def test_a_tcp_connection_opens_only_when_the_host_connect_does(uplinked) -> None:
    def echo(conn):
        while data := conn.recv(4096):
            conn.sendall(data.upper())
    listener, port = _listen_tcp(echo)
    try:
        assert uplinked.take(_tcp_to(LOOPBACK, port, 100, 0, SYN), 1, 100) is None, \
            "no SYN-ACK before the host has connected"
        sa = _parse(_until(uplinked, lambda f: _src_ip(f) == LOOPBACK))
        assert sa["flags"] == SYN | ACK and sa["ack"] == 101
        assert sa["sport"] == port, "from the remote host's port"
        isn = sa["seq"]
        uplinked.take(_tcp_to(LOOPBACK, port, 101, isn + 1, ACK), 1, 200)
        uplinked.take(_tcp_to(LOOPBACK, port, 101, isn + 1, ACK | PSH, b"hi there"),
                      1, 210)
        data = _parse(_until(uplinked, lambda f: bool(_parse(f)["payload"])))
        assert data["payload"] == b"HI THERE", "spliced both ways"
        # The guest closes; the echo sees EOF and closes; the gateway's FIN follows.
        uplinked.take(_tcp_to(LOOPBACK, port, 109, isn + 9, ACK | FIN), 1, 300)
        fin = _parse(_until(uplinked, lambda f: bool(_parse(f)["flags"] & FIN)))
        assert fin["flags"] & FIN
    finally:
        listener.close()


def test_a_host_that_cannot_be_reached_resets_the_guest(uplinked) -> None:
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    uplinked.take(_tcp_to(LOOPBACK, port, 100, 0, SYN), 1, 100)
    r = _parse(_until(uplinked, lambda f: _src_ip(f) == LOOPBACK))
    assert r["flags"] == tcp.RST | ACK and r["ack"] == 101
    assert not uplinked.conns


def test_a_name_off_the_lan_is_looked_up_through_the_host(uplinked) -> None:
    """With an uplink the NTP names are no longer the gateway's, and a name the
    zone does not hold is forwarded; the answer comes a tick later."""
    assert "pool.ntp.org" not in uplinked.zone and "lilygo.local" in uplinked.zone
    assert uplinked.take(_udp_frame(53, _dns_query("localhost")), 1, 100) is None
    reply = _udp_payload(_until(uplinked, lambda f: _src_ip(f) == SERVER_IP))
    assert int.from_bytes(reply[2:4], "big") & 0x0F == 0
    assert bytes([127, 0, 0, 1]) in reply


def test_a_local_name_is_never_sent_to_the_internet(uplinked) -> None:
    """mDNS is link-local: an unknown .local name is refused on the spot, with
    an uplink or without."""
    air = uplinked.take(_udp_frame(5353, _dns_query("nothing-here.local")), 1, 100)
    assert int.from_bytes(_udp_payload(air.frame)[2:4], "big") & 0x0F == 3
    assert uplinked.counts.get("dns forwarded") is None
