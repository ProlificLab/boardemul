#!/usr/bin/env python3
"""An access point: what is on the air, and what to say back.

The radio is a C device in QEMU because it walks descriptor chains in guest
memory. Everything about what a frame *is* lives here instead (ADR 0064),
which is where ADR 0006 says peripheral behaviour belongs and where the
standard library's hashing is.

The device drives this over a socket, synchronously:

    request   type:u16 channel:u16 length:u16 clock:u32 free:u16 frame[]
    reply     length:u16 flags:u16                                frame[]

Type 1 says the guest transmitted the frame that follows; type 2 is a tick.
Either can be answered with a frame to put on the air, or with nothing. The
clock is the guest's, in milliseconds, which is what lets this side pace
itself without the beacon rate drifting with host load -- and what lets it
keep a beacon back when something more urgent is due. `free` is how many of
the driver's descriptors the hardware owns, so this side can decline to spend
the last one on a beacon -- they are the one frame here that can always
wait.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from emu import ccmp, dhcp, dns, factory, tcp, wpa2
from emu.uplink import TCP_BUFFER, Uplink

log = logging.getLogger("wifi")

REQ_TX = 1
REQ_TICK = 2
HEAD = struct.Struct("<HHHIH")
REPLY = struct.Struct("<HH")
# Reply flags. AWAITED says a station is waiting for this frame, which lets the
# radio put it on the air even while the station's receiver reads as switched
# off -- the only state the handshake's first message is ever offered in. A
# beacon never carries it: nobody is waiting for one and another is due in
# milliseconds. See ADR 0068; the device learns that a frame is awaited and
# nothing about what it is.
AWAITED = 1
# A second reply flag: the sixteen octets after the reply header, before the
# frame, are the pairwise temporal key for the MAC model to encrypt and decrypt
# data frames with (ADR 0094). Sent once, on the first reply after the handshake
# completes, because the key install never reaches MMIO for the device to see.
REPLY_KEY = 2
# A third: the access point did not acknowledge the frame it was handed -- an
# individually addressed frame for someone else. The device then completes the
# station's transmission with an acknowledgement timeout (ADR 0109).
REPLY_UNACKED = 4

# Power save (ADR 0109, IEEE Std 802.11-2016 11.2). The frame-control bits a
# station's frames carry, and those this side sets on frames it sends.
FC1_RETRY = 0x08
FC1_PM = 0x10                   # the station will be dozing after this exchange
FC1_MORE_DATA = 0x20            # more frames are held for the dozing station
PS_POLL = 0xA4                  # a dozing station asking for one held frame
NULL_DATA = 0x48
QOS_NULL = 0xC8
# Every beacon is a DTIM beacon. Held frames go to their station, not after a
# DTIM, so the period decides nothing else here.
DTIM_PERIOD = 1
# How many frames are held for one dozing station before the oldest goes.
HOLD_LIMIT = 64

# 802.11 subtypes, as the first octet of a frame carries them.
PROBE_REQUEST = 0x40
PROBE_RESPONSE = 0x50
BEACON = 0x80
AUTH = 0xB0
ASSOC_REQUEST = 0x00
ASSOC_RESPONSE = 0x10
DISASSOC = 0xA0
DEAUTH = 0xC0
DATA = 0x08

SSID_MAX = 32

# Why a station tore the link down, as the standard numbers them. Only the ones
# this peer can plausibly provoke are named; anything else is printed raw.
DEAUTH_REASONS = {
    1: "unspecified",
    2: "the previous authentication is no longer valid",
    3: "the station is leaving",
    4: "inactivity",
    6: "a class 2 frame arrived from a station that is not authenticated",
    7: "a class 3 frame arrived from a station that is not associated",
    8: "the station is leaving the network",
    15: "the four-way handshake timed out",
    16: "the group key handshake timed out",
    17: "an element in the handshake differed from the one advertised",
    18: "the group cipher is not valid",
    19: "the pairwise cipher is not valid",
    20: "the key management suite is not valid",
    23: "802.1X authentication failed",
    24: "the cipher suite was rejected by the security policy",
}
# How long to wait for the station's message two before offering message one
# again. The radio loses a frame sent while the station is not listening and
# nothing buffers it (ADR 0067), so the peer has to be the thing that retries;
# a real access point retries this message on about this period too.
FIRST_RETRY_MS = int(os.environ.get("BOARDEMUL_WIFI_FIRST_RETRY_MS", "100"))
# BOARDEMUL_WIFI_FIRST_RETRY_MS is an experiment knob, like the hold below: set
# short, it reproduces on demand the flake ADR 0095 chased -- a station slower
# than the retry, handed several copies of message one. Default unchanged.
# Once the device reports that message one reached the station, wait this long
# instead: hostapd's eapol_key_timeout_subseq, which wpa_auth_eapol_key_tx_status
# switches to on an acknowledged message one, because supplicants handed a
# retransmitted message one while still deriving keys can fail the handshake --
# this one then never sends message four (ADR 0095).
FIRST_RETRY_DELIVERED_MS = 1000
# The type bit the device sets on the request after a frame the peer offered
# reached a receive descriptor: the model's transmit acknowledgement (ADR 0095).
REQ_DELIVERED = 0x8000
# And the bit it sets when the frame offered was retried to the 802.11 limit and
# never acknowledged: a failed tx_status (ADR 0109).
REQ_UNACKED = 0x4000
# How long after the association response to hold message one back, as an
# experiment knob only -- default 0, no hold. The idea was that a message one
# arriving before the station's supplicant finished setting up was answered
# from state that was then wiped; holding it should have fixed the handshake.
# It did not: the completion rate was the same held and unheld (ADR 0076).
# The sensitivity that is real is to GUEST speed, which a peer-side delay
# cannot touch, so the knob is kept off. Do not raise it expecting a fix.
FIRST_HOLD_MS = int(os.environ.get("BOARDEMUL_WIFI_FIRST_MS", "0"))
BROADCAST = b"\xff" * 6
# This access point's own address. Locally administered, so it cannot collide
# with a real one, and stable so a scan result stays valid across a retry.
AP_ADDRESS = bytes.fromhex("020000000001")

# A WPA2-PSK network: CCMP for both the group and the pair, one key kind, and
# no capabilities. The station's own element has to agree with this one, and
# the third handshake message sends it back so the station can check it did.
RSN_ELEMENT = bytes.fromhex(
    "3014"                  # element 48, twenty bytes
    "0100"                  # version 1
    "000fac04"              # group cipher: CCMP
    "0100" "000fac04"       # one pairwise cipher: CCMP
    "0100" "000fac02"       # one key kind: PSK
    "0000")                 # no capabilities


# The little network the peer hands out (ADR 0084, pooled by ADR 0091). The
# access point is the gateway and DNS at .1; stations are leased from .2 up by
# hardware address, so a second station gets its own address. CLIENT_IP is what
# the first station is leased, kept named for the tests that build its packets.
SERVER_IP = bytes([192, 168, 4, 1])
CLIENT_IP = bytes([192, 168, 4, 2])
NETMASK = bytes([255, 255, 255, 0])
LEASE_SECONDS = 7200
DHCP_FIRST, DHCP_LAST = 2, 254          # the host numbers the pool may lease
# The TCP ports something serves (ADR 0096); a connection to any other port is
# refused with a reset, as by a host with no listener there. The transport
# itself (emu/tcp.py, ADR 0089) knows nothing of ports.
SERVICES = {factory.PORT: "factory test"}


LAN_BROADCAST = SERVER_IP[:3] + b"\xff"
LIMITED_BROADCAST = b"\xff" * 4


def _for_the_lan(dst: bytes) -> bool:
    """Whether a packet is for this network rather than to be routed: the
    gateway itself, a broadcast, or a multicast group (mDNS's among them)."""
    return (dst == SERVER_IP or dst in (LAN_BROADCAST, LIMITED_BROADCAST)
            or 224 <= dst[0] <= 239)


def _dotted(ip: bytes) -> str:
    return ".".join(map(str, ip))


def _packed(ip: str) -> bytes:
    return bytes(int(part) for part in ip.split("."))


def ip_checksum(data: bytes) -> int:
    """The one's-complement sum an IPv4 or UDP header carries."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def ipv4_wrap(src_ip: bytes, dst_ip: bytes, proto: int, l4: bytes) -> bytes:
    """An IPv4 header, checksum filled, in front of a layer-4 payload."""
    total = 20 + len(l4)
    ip = (struct.pack(">BBHHHBBH", 0x45, 0, total, 0, 0x4000, 64, proto, 0)
          + src_ip + dst_ip)
    return ip[:10] + struct.pack(">H", ip_checksum(ip)) + ip[12:] + l4


def udp4_packet(src_ip: bytes, dst_ip: bytes, src_port: int, dst_port: int,
                payload: bytes) -> bytes:
    """An IPv4/UDP datagram with both checksums filled in.

    The station's stack verifies both, so a reply with either wrong is dropped
    as silently as if it never came.
    """
    udp_len = 8 + len(payload)
    udp = struct.pack(">HHHH", src_port, dst_port, udp_len, 0) + payload
    pseudo = src_ip + dst_ip + bytes([0, 17]) + struct.pack(">H", udp_len)
    udp = udp[:6] + struct.pack(">H", ip_checksum(pseudo + udp)) + udp[8:]
    return ipv4_wrap(src_ip, dst_ip, 17, udp)


def tcp_segment(src_ip: bytes, dst_ip: bytes, src_port: int, dst_port: int,
                seq: int, ack: int, flags: int, window: int = 64240,
                payload: bytes = b"", options: bytes = b"") -> bytes:
    """An IPv4/TCP segment with the TCP checksum filled in (ADR 0087)."""
    if len(options) % 4:
        options += bytes(4 - len(options) % 4)
    offset = (20 + len(options)) // 4
    tcp = (struct.pack(">HHIIBBHHH", src_port, dst_port, seq & 0xFFFFFFFF,
                       ack & 0xFFFFFFFF, offset << 4, flags, window, 0, 0)
           + options + payload)
    pseudo = src_ip + dst_ip + bytes([0, 6]) + struct.pack(">H", len(tcp))
    tcp = tcp[:16] + struct.pack(">H", ip_checksum(pseudo + tcp)) + tcp[18:]
    return ipv4_wrap(src_ip, dst_ip, 6, tcp)


def header(kind: int, to: bytes, sequence: int, frm: bytes = AP_ADDRESS,
           bssid: bytes = AP_ADDRESS) -> bytes:
    """Twenty-four bytes of management header.

    The sequence control field carries the number in its top twelve bits and a
    fragment number in the bottom four, which is always zero here.
    """
    return (bytes([kind, 0x00, 0x00, 0x00]) + to + frm + bssid
            + ((sequence & 0xFFF) << 4).to_bytes(2, "little"))


def tim_element(held: set[int]) -> bytes:
    """A TIM element: DTIM count 0 and period DTIM_PERIOD (every beacon is a
    DTIM), then the partial virtual bitmap of the association IDs with frames
    held (IEEE Std 802.11-2016 9.4.2.6).

    The bitmap runs from octet N1, the largest even octet number below the
    first set bit, to N2, the last octet with one; the bitmap control octet
    carries N1/2 above its group-traffic bit, which stays clear because nothing
    group-addressed is held. With nothing held it is one octet of zero.
    """
    held = {aid for aid in held if 0 < aid < 2008}
    if not held:
        bitmap, n1 = bytes([0]), 0
    else:
        n1 = (min(held) // 8) & ~1
        n2 = max(held) // 8
        octets = bytearray(n2 - n1 + 1)
        for aid in held:
            octets[aid // 8 - n1] |= 1 << (aid % 8)
        bitmap = bytes(octets)
    return bytes([0x05, 3 + len(bitmap), 0, DTIM_PERIOD, (n1 // 2) << 1]) + bitmap


@dataclass
class Network:
    """The network this access point advertises."""
    ssid: str = "BoardEmul"
    channel: int = 1
    # How often a beacon is due, on the guest's clock. A real access point
    # beacons about ten times a second; a scan that has to hear one inside its
    # dwell on a channel does better with more.
    beacon_ms: int = 100
    wpa2: bool = False
    passphrase: str = ""

    def body(self, channel: int, tim: bytes | None = None) -> bytes:
        """The fixed fields and elements a beacon and a probe response share,
        and a beacon's TIM after the channel when it is given one -- beacons
        carry one and probe responses do not (ADR 0109).

        Ordinary 2.4 GHz g: the four b rates as basic and four more on top,
        the rest in the extended element, and an ERP element to say so. Four b
        rates and nothing else described a network that does not exist, which
        no station's rate negotiation has ever had to cope with (ADR 0062).
        """
        name = self.ssid.encode()[:SSID_MAX]
        # Privacy is set for a network with a passphrase: a station given one
        # filters the open networks out of its connect-time scan, so without
        # this bit its own network is "not found" while every scan lists it.
        capability = 0x0011 if self.wpa2 else 0x0001
        out = bytearray()
        out += bytes(8)                                 # timestamp
        out += (100).to_bytes(2, "little")              # beacon interval
        out += capability.to_bytes(2, "little")
        out += bytes([0x00, len(name)]) + name          # the name
        out += bytes([0x01, 0x08, 0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
        out += bytes([0x03, 0x01, channel or self.channel])
        if tim is not None:
            out += tim                                  # beacons only
        out += bytes([0x2A, 0x01, 0x00])                # an ERP network
        if self.wpa2:
            out += RSN_ELEMENT
        out += bytes([0x32, 0x04, 0x30, 0x48, 0x60, 0x6C])
        return bytes(out)


@dataclass
class Air:
    """A frame to put on the air, and the one thing the radio must know about
    it: whether a station is waiting for it (ADR 0068)."""
    frame: bytes
    awaited: bool = False


@dataclass
class Station:
    """One station, and how far it has got with us."""
    address: bytes
    authenticated: bool = False
    associated: bool = False
    # The address DHCP leased it (ADR 0091), or None before it has one.
    ip: bytes | None = None
    # The pairwise temporal key, once the handshake completes (ADR 0094); until
    # then None, and the data cipher is identity.
    tk: bytes | None = None
    shake: wpa2.Handshake | None = None
    # Message one is sent on a tick rather than in reply to the association
    # request, because a real access point does not answer a frame twice.
    # It stays owed until message two arrives: handing it to the radio is no
    # evidence it was heard (ADR 0067).
    owes_first: bool = False
    first_at: int = 0
    # Whether a copy of message one is known to have reached the station, which
    # the device reports (ADR 0095). Handing it to the radio is no evidence;
    # this is. It stretches the retry to FIRST_RETRY_DELIVERED_MS.
    first_delivered: bool = False
    # When the association response went, so message one can be held back
    # until the station is ready for it (ADR 0076).
    associated_at: int = 0
    # Its association identifier, its own (ADR 0109); 0 before it associates.
    aid: int = 0
    # Whether its last frame to us said it would be dozing, and what is held
    # for it meanwhile, in order (ADR 0109).
    dozing: bool = False
    held: list = field(default_factory=list)


class AccessPoint:
    """Answers what the station sends, and beacons between times."""

    def __init__(self, network: Network, uplink: "Uplink | None" = None):
        self.network = network
        # The NAT to the real internet, or None for an island (ADR 0097).
        self.uplink = uplink
        # UDP flows out through it, by the key the uplink knows them by, and
        # DNS questions it is answering for the station.
        self.nat_udp: dict[tuple, dict] = {}
        self.nat_dns: dict[tuple, tuple] = {}
        self._dns_seq = 0
        self.sequence = 0
        self.stations: dict[bytes, Station] = {}
        self.master = (wpa2.pmk(network.passphrase, network.ssid)
                       if network.wpa2 and network.passphrase else b"")
        self.counts: dict[str, int] = {}
        self.channel = network.channel
        self.last_beacon = 0
        # A monotonic packet number for the CCMP header on frames we send once
        # the station is connected (ADR 0084). The cipher is identity, so the
        # number only has to advance, not be secret.
        self.pn = 0
        # TCP connections (ADR 0089), keyed by (client address, client port,
        # server address, server port) -- the server is the gateway for a local
        # service, a remote host for one through the uplink (ADR 0097). Each
        # holds a real transport (emu/tcp.py). tcp_isn advances per connection so
        # two do not share an initial sequence number. Frames the transport wants
        # to send wait in the outbox, because the peer may put only one frame on
        # the air per guest event (ADR 0064); take() and tick() drain it.
        self.conns: dict[tuple, dict] = {}
        self.tcp_isn = 0x1000
        self.outbox: list[Air] = []
        # The names this LAN hosts (ADR 0090). Each points at the gateway, which
        # is where the services behind them live -- but a name outside this set
        # gets NXDOMAIN, not the gateway, which is what makes the resolver
        # authoritative rather than captive. The set is exactly the names the
        # firmware was seen to look up (logs/{ntp,mdns,tcp}): its two NTP servers
        # and its own .local name. With an uplink the NTP servers are real ones
        # (ADR 0097): their names are looked up through the host instead, and
        # only the LAN's own name stays here.
        self.zone: dict[str, bytes] = {"lilygo.local": SERVER_IP}
        if uplink is None:
            self.zone.update({"pool.ntp.org": SERVER_IP,
                              "time.nist.gov": SERVER_IP})
        # The DHCP lease pool (ADR 0091): addresses on the gateway's /24, leased
        # by hardware address so each station gets its own and keeps it.
        self.leases = dhcp.Pool(SERVER_IP[:3], first=DHCP_FIRST, last=DHCP_LAST)
        # The guest's clock in milliseconds, set on every event, so the TCP
        # retransmission timer has a clock to run on.
        self.clock = 0
        # The temporal key waiting to be handed to the MAC model, set when a
        # handshake completes and cleared once it goes out on a reply (ADR 0094).
        self.pending_key: bytes | None = None
        # The station whose message one was the last frame offered on a tick,
        # so a delivery the device reports can be pinned on it (ADR 0095).
        self.last_offered: Station | None = None
        # Whether the last frame the station transmitted was acknowledged by
        # this access point, which the device reports back to it (ADR 0109).
        self.last_heard = True
        # What the factory-test service has been told (ADR 0096).
        self.registrations: list[factory.Report] = []
        log.info("access point %r on channel %d, %s", network.ssid,
                 network.channel,
                 "WPA2 with a master key" if self.master
                 else ("WPA2 but no passphrase" if network.wpa2 else "open"))

    def _next(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFFF
        return self.sequence

    def _count(self, what: str) -> None:
        self.counts[what] = self.counts.get(what, 0) + 1

    # ----------------------------------------------------------- power save

    @staticmethod
    def _heard(frame: bytes) -> bool:
        """Whether this access point acknowledges the frame: one addressed to
        it. A group frame wants no acknowledgement; one addressed to anyone
        else gets none, and its sender's MAC times out (ADR 0109)."""
        if len(frame) < 10:
            return True
        to = frame[4:10]
        return bool(to[0] & 1) or to == AP_ADDRESS

    def _station_for(self, frame: bytes) -> "Station | None":
        """The station an outgoing frame is individually addressed to."""
        if len(frame) < 10 or frame[4] & 1:
            return None
        return self.stations.get(frame[4:10])

    def _hold(self, station: Station, air: Air) -> None:
        if len(station.held) >= HOLD_LIMIT:
            station.held.pop(0)
            self._count("held frame dropped, the hold was full")
        station.held.append(air)
        self._count("frame held for a dozing station")

    def _route(self) -> Air | None:
        """The next frame from the outbox that may go now. One for a dozing
        station is held for it instead, as an access point buffers for a
        station in power save (ADR 0109)."""
        while self.outbox:
            air = self.outbox.pop(0)
            station = self._station_for(air.frame)
            if station is not None and station.dozing:
                self._hold(station, air)
                continue
            return air
        return None

    def _power(self, station: Station, frame: bytes) -> None:
        """The power-management bit of a frame the station addressed to us says
        whether it will be dozing after this exchange. Waking releases what was
        held, in order and ahead of anything newer."""
        dozing = bool(frame[1] & FC1_PM)
        if dozing == station.dozing:
            return
        station.dozing = dozing
        self._count("station dozing" if dozing else "station awake")
        if not dozing and station.held:
            log.info("at %d ms %s is awake; %d held frame(s) go out", self.clock,
                     station.address.hex(":"), len(station.held))
            self.outbox[0:0] = station.held
            station.held = []

    def _ps_poll(self, station: Station) -> Air:
        """One held frame for a dozing station that asked, with More Data set
        while more remain -- or a null-data frame when none is held."""
        if station.held:
            air = station.held.pop(0)
            frame = bytearray(air.frame)
            if station.held:
                frame[1] |= FC1_MORE_DATA
            self._count("ps-poll answered")
            return Air(bytes(frame), awaited=True)
        self._count("ps-poll with nothing held")
        return Air(bytes([NULL_DATA, 0x02, 0x00, 0x00]) + station.address
                   + AP_ADDRESS + AP_ADDRESS
                   + ((self._next() & 0xFFF) << 4).to_bytes(2, "little"),
                   awaited=True)

    def unacked(self) -> None:
        """The device says the last frame offered was tried to the 802.11 retry
        limit and never acknowledged, which is hostapd's failed tx_status.

        A message one stays owed and keeps its short retry timer, which is
        what an unacknowledged one gets from hostapd too; anything else -- a
        TCP segment -- is recovered by its own protocol, as it would be.
        """
        station, self.last_offered = self.last_offered, None
        self._count("frame unacknowledged after retries")
        if station is not None:
            log.info("at %d ms message one to %s went unacknowledged", self.clock,
                     station.address.hex(":"))

    # ---------------------------------------------------------------- ticks

    def delivered(self) -> None:
        """The device says the last frame offered on a tick reached the station.

        If that frame was a message one, the station has it: from now on it is
        retried on hostapd's longer timeout, not the short one that exists only
        because this radio loses offers (ADR 0095). Anything else offered -- a
        beacon, a TCP segment -- needs nothing done about it.
        """
        station, self.last_offered = self.last_offered, None
        if station is None or not station.owes_first or station.first_delivered:
            return
        station.first_delivered = True
        self._count("eapol 1 delivered")
        log.info("at %d ms message one reached %s; the next copy waits %d ms",
                 self.clock, station.address.hex(":"), FIRST_RETRY_DELIVERED_MS)

    def tick(self, channel: int, clock: int, free: int = 9) -> Air | None:
        """What to put on the air when nothing was asked.

        A handshake that owes its first message gets it here: the association
        response has already gone, and this is the next thing the station is
        waiting for.
        """
        self.channel = channel or self.channel
        self.clock = clock
        self.last_offered = None
        # Anything owed comes first, and is never held back for a schedule.
        for station in self.stations.values():
            if not (station.owes_first and station.shake is not None):
                continue
            if station.dozing:
                continue                # held until it wakes (ADR 0109)
            if clock - station.associated_at < FIRST_HOLD_MS:
                continue                # not ready for it yet; see ADR 0076
            again = station.first_at != 0
            wait = (FIRST_RETRY_DELIVERED_MS if station.first_delivered
                    else FIRST_RETRY_MS)
            if again and clock - station.first_at < wait:
                continue                # sent recently; give it time to reply
            station.first_at = clock
            self._count("eapol 1 again" if again else "eapol 1")
            if not again:
                log.info("at %d ms handshake message one goes to %s", clock,
                         station.address.hex(":"))
            self.last_beacon = clock
            self.last_offered = station
            return Air(self.data(station.address, station.shake.first()),
                       awaited=True)
        # What came back through the uplink (ADR 0097), then TCP retransmissions
        # and any frames a connection still owes, go before a beacon: a
        # connected station is waiting on them, and another beacon is
        # milliseconds away (ADR 0089).
        self._uplink_events()
        self._tcp_tick(clock)
        air = self._route()
        if air is not None:
            return air
        # A beacon only when one is due. Offering one on every opportunity is
        # what filled the driver's descriptors and left none for the frames
        # that matter.
        if clock - self.last_beacon < self.network.beacon_ms:
            self._count("nothing due")
            return None
        if free < 2:
            # Never spend the last descriptor on a beacon. The driver recycles
            # them at its own pace, and it was spending a whole second after an
            # association with none free -- which is where the handshake's
            # first message has to go.
            self._count("beacon held back")
            return None
        self.last_beacon = clock
        self._count("beacon")
        # Not awaited: a beacon nobody hears is a beacon missed, and the next
        # one is twenty milliseconds away. Its TIM names every station with
        # frames held for it, a message one owed to a dozing one included.
        held = {s.aid for s in self.stations.values()
                if s.held or (s.dozing and s.owes_first)}
        return Air(header(BEACON, BROADCAST, self._next())
                   + self.network.body(channel, tim_element(held)))

    # --------------------------------------------------------------- frames

    def data(self, to: bytes, payload: bytes) -> bytes:
        """An EAPOL payload in a data frame, with the LLC header in front.

        From the access point to the station, so From-DS is set and the
        addressing is destination, BSSID, source. Unencrypted: the handshake
        runs before there is a key to encrypt it with, and its own integrity
        code is what protects it.
        """
        fc = bytes([DATA, 0x02])        # data, From-DS
        head = (fc + b"\x00\x00" + to + AP_ADDRESS + AP_ADDRESS
                + ((self._next() & 0xFFF) << 4).to_bytes(2, "little"))
        llc = bytes.fromhex("aaaa03000000888e")
        return head + llc + payload

    def data_protected(self, to: bytes, ethertype: bytes, payload: bytes) -> bytes:
        """A From-DS data frame with the Protected bit and a CCMP header.

        The mirror of what the station sends once it is connected. Once the
        handshake has given the station a key (ADR 0094), the body is really
        encrypted with it: the CCMP header carries the packet number, and the
        eight-octet trailer is a real MIC, not a stand-in -- the MAC model in
        QEMU holds the same key and decrypts it before the driver sees it. Until
        then (no key yet, an open network) the cipher is identity, as ADR 0084
        left it, and the trailer is eight zeros.
        """
        fc = bytes([DATA, 0x42])        # data, From-DS, Protected
        head = (fc + b"\x00\x00" + to + AP_ADDRESS + AP_ADDRESS
                + ((self._next() & 0xFFF) << 4).to_bytes(2, "little"))
        self.pn += 1
        ccmp_header = ccmp.build_header(self.pn)
        body = bytes.fromhex("aaaa03000000") + ethertype + payload
        station = self.stations.get(to)
        if station is not None and station.tk is not None:
            sealed = ccmp.encrypt(station.tk, head, ccmp_header, body)
            return head + ccmp_header + sealed
        return head + ccmp_header + body + bytes(8)

    def take(self, frame: bytes, channel: int, clock: int = 0) -> Air | None:
        """A frame the guest transmitted. What to send back, or nothing.

        Everything this returns answers something the station just sent, so
        the station is waiting for it by construction and it is marked so.

        Whether this access point acknowledged the frame is left in
        `last_heard` for the device (ADR 0109). A frame an associated station
        addressed to us carries its power-management state; a PS-Poll asks for
        one held frame; and an answer for a station that is dozing is held for
        it like anything else.
        """
        self.channel = channel or self.channel
        self.clock = clock
        self.last_heard = self._heard(frame)
        sender = self.stations.get(frame[10:16]) if len(frame) >= 16 else None
        if (sender is not None and sender.associated and not frame[0] & 0x03
                and frame[4:10] == AP_ADDRESS):
            if frame[0] == PS_POLL:
                return self._ps_poll(sender)
            self._power(sender, frame)
        answer = self._answer(frame, channel, clock)
        self._uplink_events()           # anything the uplink has brought back
        if answer:
            air = Air(answer, awaited=True)
            target = self._station_for(answer)
            if target is None or not target.dozing or (answer[0] & 0x0C) != 0x08:
                return air
            self._hold(target, air)
        # A TCP exchange puts its frames in the outbox rather than returning one
        # (ADR 0089); hand back the first now, the rest follow on ticks.
        return self._route()

    def _answer(self, frame: bytes, channel: int,
                clock: int = 0) -> bytes | None:
        """Which frame answers this one, as bytes."""
        self.channel = channel or self.channel
        self.clock = clock
        if len(frame) < 24:
            return None
        # Bits zero and one of the first octet are the protocol version and
        # are zero in every real frame. Anything else is not a frame: the
        # device sometimes reports a transmit whose buffer holds stale memory,
        # and about a quarter of arbitrary bytes look like a data frame to the
        # type check below. Counting those as replies from the station is how
        # "the station answers our handshake" got believed for a while --
        # twenty-three of them in one run, with random addresses and an
        # impossible version, and a constant at addr3 that gave them away.
        if frame[0] & 0x03:
            self._count("not a frame (bad protocol version)")
            return None
        kind = frame[0] & 0xFC
        to, station_address = frame[4:10], frame[10:16]
        if (frame[0] & 0x0C) == 0x08:
            return self._data(frame)
        if (frame[0] & 0x0C) != 0:
            return None                 # control frames are not ours to answer
        if to != AP_ADDRESS and to != BROADCAST:
            return None
        # The low bit of the first octet is the group bit, so a source address
        # with it set is not a station -- it is a broadcast or multicast
        # address, and answering it invents a peer that does not exist. One run
        # grew an ff:ff:ff:ff:ff:ff "station" with a handshake of its own.
        if station_address[0] & 1:
            return None
        station = self.stations.setdefault(station_address,
                                           Station(station_address))
        if kind == PROBE_REQUEST:
            self._count("probe")
            return (header(PROBE_RESPONSE, station_address, self._next())
                    + self.network.body(channel))
        if kind == AUTH:
            return self._auth(station, clock)
        if kind == ASSOC_REQUEST:
            return self._assoc(station, clock)
        if kind in (DISASSOC, DEAUTH):
            self._count("disassoc" if kind == DISASSOC else "deauth")
            # Logged with the clock because what matters about a disassociation
            # is how long the station waited before giving up -- the handshake
            # has to fit inside that window.
            # The reason code is the station telling us what it objected to,
            # which is the only direct statement it ever makes about our
            # frames. Two bytes of fixed body, straight after the header.
            reason = (int.from_bytes(frame[24:26], "little")
                      if len(frame) >= 26 else 0)
            log.info("at %d ms %s %s: reason %d, %s", clock,
                     station_address.hex(":"),
                     "disassociated" if kind == DISASSOC else "deauthenticated",
                     reason, DEAUTH_REASONS.get(reason, "not a reason we name"))
            self._count("reason %d" % reason)
            self.stations.pop(station_address, None)
            return None
        return None

    def _auth(self, station: Station, clock: int = 0) -> bytes:
        """Open System, transaction two, success."""
        self._count("auth")
        log.info("at %d ms %s authenticated", clock, station.address.hex(":"))
        station.authenticated = True
        station.associated = False
        station.shake = None
        station.owes_first = False
        station.dozing = False          # a joining station starts awake
        station.held = []
        return (header(AUTH, station.address, self._next())
                + bytes([0x00, 0x00, 0x02, 0x00, 0x00, 0x00]))

    def _assoc(self, station: Station, clock: int = 0) -> bytes:
        """Accepted, with the station's own association identifier (ADR 0109).

        And, on a protected network, the handshake starts: its first message
        goes out on the next tick rather than now, because one frame gets one
        reply.
        """
        self._count("assoc")
        station.associated = True
        station.dozing = False
        if not station.aid:
            taken = {s.aid for s in self.stations.values() if s.aid}
            station.aid = next(a for a in range(1, 2008) if a not in taken)
        log.info("at %d ms %s associated; handshake %s", clock,
                 station.address.hex(":"),
                 "armed" if self.master else "not armed")
        if self.master:
            station.shake = wpa2.Handshake(
                master=self.master, ap=AP_ADDRESS, station=station.address,
                rsn_element=RSN_ELEMENT)
            station.owes_first = True
            station.associated_at = clock
            # A fresh handshake, so its first message is a first send and must
            # not wait out the previous one's retry deadline -- nor inherit the
            # previous one's delivery (ADR 0095).
            station.first_at = 0
            station.first_delivered = False
        return (header(ASSOC_RESPONSE, station.address, self._next())
                + bytes([0x11 if self.network.wpa2 else 0x01, 0x00,
                         0x00, 0x00, station.aid & 0xFF, 0xC0 | (station.aid >> 8),
                         0x01, 0x04, 0x82, 0x84, 0x8B, 0x96]))

    def _data(self, frame: bytes) -> bytes | None:
        """A data frame from the station. Only EAPOL is understood so far.

        Every way out of here used to be a silent `return None`, so a frame in
        a shape this did not expect vanished without appearing in any count --
        which is indistinguishable from a station that sent nothing, and that
        is the question this code is usually being asked.
        """
        self._count("data frame in")
        if frame[0] in (NULL_DATA, QOS_NULL):
            # No body: a station saying it is dozing or awake, which take()
            # has already read off the frame control (ADR 0109).
            self._count("null data")
            return None
        station = self.stations.get(frame[10:16])
        if station is None:
            # Either the addressing is not what this expects, or the station
            # was already forgotten -- a deauthentication pops it, so a reply
            # that arrives just after one finds nothing. Both look the same
            # from a single counter, so say which and say who.
            self._count("data frame from an unknown station")
            log.info("a 0x%02x data frame: addr1 %s addr2 %s addr3 %s, and "
                     "no station of that name; known: %s",
                     frame[0], frame[4:10].hex(":"), frame[10:16].hex(":"),
                     frame[16:22].hex(":"),
                     ", ".join(a.hex(":") for a in self.stations) or "none")
            return None
        if station.shake is None:
            self._count("data frame before the handshake was armed")
            return None
        # A QoS data frame carries two more bytes of header than an ordinary
        # one, so the payload does not start at twenty-four. Reading it at the
        # fixed offset put the LLC check two bytes out and dropped the frame
        # for looking wrong.
        at = 26 if frame[0] & 0x80 else 24
        # A Protected frame carries an eight-byte CCMP header before the body.
        # Once the station has a key, the MAC model has really encrypted the
        # body (ADR 0094), so decrypt it with the key and verify its MIC; a frame
        # whose MIC does not check is dropped, not read. Before the key exists
        # the cipher is identity (ADR 0084) and the body is plaintext behind the
        # header; EAPOL is sent unprotected and has no CCMP header at all.
        if frame[1] & 0x40:                         # Protected
            if station.tk is not None:
                body = ccmp.decrypt(station.tk, frame[:at], frame[at:at + 8],
                                    frame[at + 8:])
                if body is None:
                    self._count("data frame failed its MIC")
                    log.info("a data frame from %s did not decrypt (bad MIC)",
                             station.address.hex(":"))
                    return None
            else:
                body = frame[at + 8:]               # identity, before the key
        else:
            body = frame[at:]
        if body[:6] != bytes.fromhex("aaaa03000000"):
            self._count("data frame that is not EAPOL")
            log.info("a 0x%02x data frame from %s carried %s, not LLC",
                     frame[0], frame[10:16].hex(":"), body[:8].hex())
            return None
        ethertype = body[6:8]
        if ethertype == b"\x08\x00":            # IPv4 -- DHCP lives here
            return self._ip(station, body[8:])
        if ethertype == b"\x08\x06":            # ARP
            return self._arp(station, body[8:])
        if ethertype != b"\x88\x8e":            # not EAPOL either
            self._count("data frame that is not EAPOL")
            log.info("a 0x%02x data frame from %s: ethertype %s",
                     frame[0], frame[10:16].hex(":"), ethertype.hex())
            return None
        eap = body[8:]
        # Truncate to the EAPOL frame's own declared length before anything
        # reads it. The radio hands over the whole transmit buffer, which can
        # carry a few trailing bytes past the frame -- a 0xdeadbeef marker was
        # measured -- and the station computes its integrity code over exactly
        # the EAPOL frame, four bytes plus the length field. Include the trailing
        # bytes and the code never matches, which is precisely what stalled the
        # handshake with every other input already correct (ADR 0070).
        if len(eap) >= 4:
            eap = eap[:4 + int.from_bytes(eap[2:4], "big")]
        reply = station.shake.take(eap)
        info = wpa2.key_info(eap)
        verified = reply is not None or station.shake.done
        # Every EAPOL frame in full, because there are a handful per run and
        # whether a message that does not verify has the wrong bytes or the
        # wrong key can only be told by reading the bytes.
        log.info("eapol in from %s, %d byte(s), header %s, key_info 0x%04x, "
                 "%s: %s", station.address.hex(":"), len(frame),
                 frame[:at].hex(), info, "verified" if verified
                 else "NOT verified", eap.hex())
        if reply is not None:
            # A verified frame means message one arrived and is no longer owed.
            station.owes_first = False
        if station.shake.done:
            # A real message four: the Secure bit was set and the code verified
            # (ADR 0078). This is the only path that ends the handshake.
            self._count("eapol 4")
            self._count("handshake done")
            log.info("the station %s completed the four-way handshake",
                     station.address.hex(":"))
            # The data cipher is now real (ADR 0094): keep the temporal key to
            # encrypt and decrypt the station's frames, and hand a copy to the
            # MAC model on the next reply, since the key install it would read
            # never reaches MMIO (ADR 0077).
            if station.shake.keys is not None:
                station.tk = station.shake.keys.tk
                self.pending_key = station.shake.keys.tk
            return None
        # Anything else here is a message two -- the first, or a retransmit
        # because the station has not accepted message three.
        self._count("eapol 2")
        if reply is None:
            # No reply means the integrity code did not verify: a key-schedule
            # mismatch, kept distinct from "the station never replied".
            if info & wpa2.KI_MIC:
                self._count("message two, mic did not verify")
            return None
        self._count("eapol 3")
        log.info("eapol out to %s, message three: %s",
                 station.address.hex(":"), reply.hex())
        return self.data(station.address, reply)

    def _ip(self, station: "Station", ip: bytes) -> bytes | None:
        """An IPv4 packet from the station. DHCP is the one it needs answered."""
        if len(ip) < 28 or (ip[0] >> 4) != 4:
            return None
        # Trim the device's trailing bytes (the 0xdeadbeef marker and whatever
        # follows) to the IP total-length, so a length-less header like TCP's
        # does not read them as payload. UDP self-trims by its own length.
        ip = ip[:int.from_bytes(ip[2:4], "big")]
        ihl = (ip[0] & 0x0F) * 4
        # Reply to whoever sent it, not to a fixed client (ADR 0091): the source
        # address is the station's own, leased by DHCP, so a second station's
        # traffic is answered at its own address without any of this changing.
        src_ip = ip[12:16]
        dst_ip = ip[16:20]
        # The gateway routes (ADR 0097). What is for the LAN -- the gateway,
        # a broadcast, a multicast -- is answered here. Anything else is for the
        # internet: out through the uplink if there is one, and dropped as
        # unroutable if not, rather than answered by a local service as if the
        # gateway were that host.
        if not _for_the_lan(dst_ip):
            return self._route_out(station, src_ip, dst_ip, ip[9], ip[ihl:])
        if ip[9] == 6:                          # TCP
            return self._tcp(station, src_ip, ip[ihl:])
        if ip[9] != 17:                         # not UDP
            return None
        udp = ip[ihl:]
        src_port = int.from_bytes(udp[0:2], "big")
        dst_port = int.from_bytes(udp[2:4], "big")
        udp_len = int.from_bytes(udp[4:6], "big")
        payload = udp[8:udp_len]
        if dst_port == 67:                      # DHCP
            return self._dhcp(station, payload)
        if dst_port == 53:                      # DNS
            return self._dns(station, src_ip, src_port, payload)
        if dst_port == 123:                     # NTP
            return self._ntp(station, src_ip, src_port, payload)
        if dst_port == 5353:                    # mDNS (.local lookups)
            return self._mdns(station, src_ip, src_port, payload)
        self._count("udp, unanswered to %d" % dst_port)
        return None

    def _dhcp(self, station: "Station", bootp: bytes) -> bytes | None:
        """A BOOTP request: answer a DISCOVER with an OFFER, a REQUEST with ACK.

        The address comes from the pool, keyed by the client's hardware address
        (ADR 0091), so a second station gets its own; a renewing station gets the
        one it had. The reply is broadcast, because the client has no address to
        receive it at yet.
        """
        parsed = dhcp.request(bootp)
        if parsed is None:
            return None
        xid, chaddr, message_type = parsed
        if message_type == dhcp.DISCOVER:
            kind, name = dhcp.OFFER, "dhcp offer"
        elif message_type == dhcp.REQUEST:
            kind, name = dhcp.ACK, "dhcp ack"
        else:
            return None
        yiaddr = self.leases.allocate(chaddr)
        if yiaddr is None:                      # the pool is used up
            self._count("dhcp pool exhausted")
            return None
        station.ip = yiaddr
        self._count(name)
        log.info("dhcp %s to %s: %s", "OFFER" if kind == dhcp.OFFER else "ACK",
                 station.address.hex(":"), ".".join(map(str, yiaddr)))
        frame = dhcp.reply(xid, chaddr, kind, yiaddr, SERVER_IP, NETMASK,
                           SERVER_IP, LEASE_SECONDS)
        packet = udp4_packet(SERVER_IP, bytes([255, 255, 255, 255]), 67, 68, frame)
        return self.data_protected(station.address, b"\x08\x00", packet)

    def _resolve(self, station: "Station", dst_ip: bytes, src_port: int,
                 query: bytes, kind: str, reply_port: int) -> bytes | None:
        """Answer a DNS or mDNS A query against the zone (ADR 0090).

        A name the LAN hosts gets its record; a name it does not gets NXDOMAIN,
        which is the difference between an authoritative resolver and the captive
        one this replaces. A DNS query and the firmware's unicast-shaped `.local`
        mDNS query have the same body, so both come through here; only the port
        the reply leaves from differs. The reply goes back to the querier's own
        address (ADR 0091).

        With an uplink, a DNS name the zone does not hold is looked up through
        the host instead of refused (ADR 0097), off the guest's path: the answer
        goes out on a later tick. A `.local` name, asked by mDNS, is link-local
        and never leaves.
        """
        parsed = dns.parse_query(query)
        if (self.uplink is not None and kind == "dns" and parsed is not None
                and parsed.qtype == dns.TYPE_A and parsed.qclass == dns.CLASS_IN
                and parsed.name not in self.zone):
            self._dns_seq += 1
            key = ("dns", self._dns_seq)
            self.nat_dns[key] = (station, dst_ip, src_port, parsed, reply_port)
            self._count("dns forwarded")
            self.uplink.resolve(key, parsed.name)
            return None
        reply = dns.respond(query, self.zone)
        if reply is None:
            self._count("%s, not served" % kind)
            return None
        name = parsed.name if parsed else "?"
        held = self.zone.get(name)
        self._count("%s reply" % kind if held else "%s nxdomain" % kind)
        log.info("%s A %s -> %s for %s", kind, name,
                 ".".join(map(str, held)) if held else "NXDOMAIN",
                 station.address.hex(":"))
        packet = udp4_packet(SERVER_IP, dst_ip, reply_port, src_port, reply)
        return self.data_protected(station.address, b"\x08\x00", packet)

    def _dns(self, station: "Station", dst_ip: bytes, src_port: int,
             query: bytes) -> bytes | None:
        """A DNS query. The station resolves its NTP servers this way and then
        talks NTP to the address that comes back; those names are in the zone and
        point at the gateway, which is where NTP is answered (ADR 0085/0090)."""
        return self._resolve(station, dst_ip, src_port, query, "dns", 53)

    def _mdns(self, station: "Station", dst_ip: bytes, src_port: int,
              query: bytes) -> bytes | None:
        """An mDNS A query (the firmware's `.local` lookups), answered like DNS
        but unicast back from port 5353 (ADR 0086)."""
        return self._resolve(station, dst_ip, src_port, query, "mdns", 5353)

    def _ntp(self, station: "Station", dst_ip: bytes, src_port: int,
             ntp: bytes) -> bytes | None:
        """An NTP request. Answer with the host's clock as the server's time."""
        if len(ntp) < 48:
            return None
        now = time.time() + 2208988800            # seconds from the NTP epoch (1900)
        secs, frac = int(now), int((now - int(now)) * (1 << 32))
        stamp = struct.pack(">II", secs, frac)
        self._count("ntp reply")
        reply = bytes([0x24, 1, 4, 0xEC])         # LI 0, VN 4, mode 4 server; stratum 1
        reply += struct.pack(">I", 0) + struct.pack(">I", 0) + b"LOCL"  # delay, dispersion, refid
        reply += stamp                            # reference timestamp
        reply += ntp[40:48]                       # originate = the client's transmit stamp
        reply += stamp                            # receive timestamp
        reply += stamp                            # transmit timestamp
        packet = udp4_packet(SERVER_IP, dst_ip, 123, src_port, reply)
        return self.data_protected(station.address, b"\x08\x00", packet)

    def _tcp(self, station: "Station", client_ip: bytes,
             segment: bytes) -> bytes | None:
        """A TCP segment from the station, carried by a real transport (ADR 0089).

        The transport is fed the segment and asked what to send; the answers go
        into the outbox, because there may be more than one and the peer sends
        only one frame per event. This returns nothing: `take`/`tick` drain the
        outbox. Connections are keyed by (client address, client port, server
        port), so more than one at a time, from more than one station, is fine.
        """
        if len(segment) < 20:
            return None
        client_port = int.from_bytes(segment[0:2], "big")
        server_port = int.from_bytes(segment[2:4], "big")
        seq = int.from_bytes(segment[4:8], "big")
        ack = int.from_bytes(segment[8:12], "big")
        data_off = (segment[12] >> 4) * 4
        flags = segment[13]
        window = int.from_bytes(segment[14:16], "big")
        payload = segment[data_off:]

        key = (client_ip, client_port, SERVER_IP, server_port)
        entry = self.conns.get(key)
        if entry is None:
            if not (flags & tcp.SYN) or server_port not in SERVICES:
                # No connection for it to go to: a port nothing serves, or one
                # already forgotten. Refused as a host in the CLOSED state
                # refuses it (ADR 0096).
                return self._tcp_refuse(station, client_ip, client_port,
                                        SERVER_IP, server_port, seq, ack, flags,
                                        payload)
            self.tcp_isn = (self.tcp_isn + 0x10000) & 0xFFFFFFFF
            entry = {"conn": tcp.Connection(iss=self.tcp_isn),
                     "client_ip": client_ip, "client_port": client_port,
                     "server_ip": SERVER_IP, "server_port": server_port,
                     "station": station,
                     "stream": b"", "report": None, "closing": False}
            self.conns[key] = entry
            self._count("tcp connection to %d" % server_port)
        conn = entry["conn"]
        out = conn.on_segment(seq, ack, flags, window, payload, self.clock)
        if flags & tcp.RST:
            log.info("tcp :%d from %s reset by the client", server_port,
                     station.address.hex(":"))
        self._tcp_service(entry)                 # let a service read and answer
        out += conn.pump(self.clock)             # send anything the service wrote
        for seg in out:
            self.outbox.append(self._tcp_frame(entry, seg))
        if conn.closed:
            self._tcp_forget(key, entry)
        return None

    def _tcp_refuse(self, station: "Station", client_ip: bytes,
                    client_port: int, server_ip: bytes, server_port: int,
                    seq: int, ack: int, flags: int, payload: bytes,
                    why: str = "nothing serves that port") -> None:
        """Answer a segment with no connection with a reset (RFC 9293's CLOSED
        state), or with nothing if it was itself a reset. It comes from the
        address the segment was sent to: the gateway, or a remote host the
        uplink could not reach."""
        seg = tcp.refuse(seq, ack, flags, len(payload))
        if seg is None:
            return None
        self._count("tcp refused on %d" % server_port)
        if flags & tcp.SYN:
            log.info("tcp %s:%d refused for %s: %s", _dotted(server_ip),
                     server_port, station.address.hex(":"), why)
        self.outbox.append(self._tcp_frame(
            {"client_ip": client_ip, "client_port": client_port,
             "server_ip": server_ip, "server_port": server_port,
             "station": station}, seg))
        return None

    def _tcp_service(self, entry: dict) -> None:
        """What sits above the transport: the factory-test server on 8896, the
        one service this LAN runs (ADR 0096).

        The device sends one JSON report and reads nothing back, so the service
        collects the stream until it parses, records the report -- checking the
        MAC it claims against the address the frames came from -- and writes
        nothing. When the client closes its half, the service closes its own.
        """
        conn = entry["conn"]
        entry["stream"] += conn.read()
        if entry["report"] is None and entry["stream"]:
            report = factory.parse_report(entry["stream"])
            if report is not None:
                entry["report"] = report
                self._register(entry["station"], report)
        if conn.state == tcp.CLOSE_WAIT and not entry["closing"]:
            entry["closing"] = True
            log.info("tcp :%d from %s closed by the client; closing ours",
                     entry["server_port"], entry["station"].address.hex(":"))
            conn.close()

    def _register(self, station: "Station", report: "factory.Report") -> None:
        """Keep a factory-test report and say what it was."""
        claimed = factory.mac_bytes(report.mac)
        matches = claimed == station.address
        self.registrations.append(report)
        self._count("factory registration")
        log.info("factory test: %s %s (SKU %s) registered at %s, MAC %s -- %s",
                 report.product, report.mac, report.sku, report.date,
                 station.address.hex(":"),
                 "the address it transmits from" if matches
                 else "NOT the address it transmits from")

    def _tcp_frame(self, entry: dict, seg: "tcp.Segment") -> Air:
        """Turn a transport segment into a frame on the air: an IPv4/TCP datagram
        from the server -- the gateway, or the remote host behind the uplink --
        to the station, behind a data frame. Marked awaited so the radio sends
        it even if the station's receiver reads as off."""
        opts = (bytes([2, 4]) + struct.pack(">H", seg.mss)) \
            if seg.mss is not None else b""
        datagram = tcp_segment(entry["server_ip"], entry["client_ip"],
                               entry["server_port"], entry["client_port"],
                               seg.seq, seg.ack, seg.flags, window=seg.window,
                               payload=seg.payload, options=opts)
        return Air(self.data_protected(entry["station"].address, b"\x08\x00",
                                       datagram), awaited=True)

    def _tcp_tick(self, clock: int) -> None:
        """Give every connection the clock, so its retransmission timer can fire
        and anything a service queued can go, and forget those that have closed.
        A connection through the uplink first takes what the remote end has
        sent, as much as the guest has room for (ADR 0097)."""
        for key, entry in list(self.conns.items()):
            conn = entry["conn"]
            if conn is None:
                continue                         # still connecting on the host
            if entry.get("nat"):
                self._nat_pull(key, entry)
            for seg in conn.on_tick(clock):
                self.outbox.append(self._tcp_frame(entry, seg))
            if conn.closed:
                self._tcp_forget(key, entry)

    def _tcp_forget(self, key: tuple, entry: dict) -> None:
        self.conns.pop(key, None)
        if entry.get("nat") and self.uplink is not None:
            self.uplink.tcp_close(key)

    # ------------------------------------------------------------ the uplink

    def _route_out(self, station: "Station", src_ip: bytes, dst_ip: bytes,
                   proto: int, l4: bytes) -> None:
        """A packet for somewhere other than this LAN (ADR 0097)."""
        if dst_ip[:3] == SERVER_IP[:3]:
            # Another station's address. Station-to-station traffic through the
            # access point is not modelled; nothing in the firmware sends any.
            self._count("ip to another host on the LAN, not forwarded")
            return None
        if self.uplink is None:
            self._count("ip with no route (no uplink)")
            return None
        if proto == 17:
            return self._nat_udp(station, src_ip, dst_ip, l4)
        if proto == 6:
            return self._nat_tcp(station, src_ip, dst_ip, l4)
        self._count("ip protocol %d not forwarded" % proto)
        return None

    def _nat_udp(self, station: "Station", src_ip: bytes, dst_ip: bytes,
                 udp: bytes) -> None:
        """A datagram out through the uplink, on its flow's own host socket."""
        if len(udp) < 8:
            return None
        sport = int.from_bytes(udp[0:2], "big")
        dport = int.from_bytes(udp[2:4], "big")
        payload = udp[8:int.from_bytes(udp[4:6], "big")]
        flow = ("udp", src_ip, sport, dst_ip, dport)
        if flow not in self.nat_udp:
            self.nat_udp[flow] = {"station": station, "guest_ip": src_ip,
                                  "guest_port": sport}
            log.info("uplink: udp %s:%d -> %s:%d", _dotted(src_ip), sport,
                     _dotted(dst_ip), dport)
        self._count("uplink udp out")
        self.uplink.udp_send(flow, (_dotted(dst_ip), dport), payload)
        return None

    def _nat_tcp(self, station: "Station", src_ip: bytes, dst_ip: bytes,
                 segment: bytes) -> None:
        """A segment for a remote host. The guest's connection is terminated
        here and spliced to a host socket (ADR 0097); its SYN is not answered
        until the host's own connect is."""
        if len(segment) < 20:
            return None
        cport = int.from_bytes(segment[0:2], "big")
        dport = int.from_bytes(segment[2:4], "big")
        seq = int.from_bytes(segment[4:8], "big")
        ack = int.from_bytes(segment[8:12], "big")
        flags = segment[13]
        window = int.from_bytes(segment[14:16], "big")
        payload = segment[(segment[12] >> 4) * 4:]
        key = (src_ip, cport, dst_ip, dport)
        entry = self.conns.get(key)
        if entry is None:
            if not flags & tcp.SYN or flags & (tcp.ACK | tcp.RST):
                return self._tcp_refuse(station, src_ip, cport, dst_ip, dport,
                                        seq, ack, flags, payload,
                                        why="no such connection")
            self.conns[key] = {"conn": None, "nat": True, "station": station,
                               "client_ip": src_ip, "client_port": cport,
                               "server_ip": dst_ip, "server_port": dport,
                               "syn": (seq, window), "fin_passed": False,
                               "eof_passed": False}
            self._count("uplink tcp open")
            log.info("uplink: tcp %s:%d -> %s:%d, connecting on the host",
                     _dotted(src_ip), cport, _dotted(dst_ip), dport)
            self.uplink.tcp_open(key, (_dotted(dst_ip), dport))
            return None
        conn = entry["conn"]
        if conn is None:
            if flags & tcp.RST:                  # given up before we answered
                self._tcp_forget(key, entry)
            return None                          # else a SYN retransmitted
        out = conn.on_segment(seq, ack, flags, window, payload, self.clock)
        if flags & tcp.RST:
            log.info("uplink: tcp to %s:%d reset by the client",
                     _dotted(dst_ip), dport)
            self._tcp_forget(key, entry)
            return None
        data = conn.read()
        if data:
            self.uplink.tcp_send(key, data)
        if not entry["fin_passed"] and conn.state in (
                tcp.CLOSE_WAIT, tcp.LAST_ACK, tcp.CLOSING, tcp.TIME_WAIT):
            entry["fin_passed"] = True           # the guest is done sending
            self.uplink.tcp_shutdown(key)
        out += conn.pump(self.clock)
        for seg in out:
            self.outbox.append(self._tcp_frame(entry, seg))
        if conn.closed:
            self._tcp_forget(key, entry)
        return None

    def _nat_pull(self, key: tuple, entry: dict) -> None:
        """Move what the remote end sent into the guest's connection, no more
        than keeps 64 KiB waiting on its window, and close the guest's side
        once the remote end has closed and everything it sent is across."""
        conn = entry["conn"]
        if entry["eof_passed"] or conn.state not in (tcp.ESTABLISHED,
                                                      tcp.CLOSE_WAIT):
            return
        room = TCP_BUFFER - len(conn.send_buf)
        if room <= 0:
            return
        data, finished = self.uplink.tcp_take(key, room)
        if data:
            conn.write(data)
        if finished:
            entry["eof_passed"] = True
            conn.close()

    def _uplink_events(self) -> None:
        """Turn what came back through the uplink into frames for the station."""
        if self.uplink is None:
            return
        for event in self.uplink.events():
            kind = event[0]
            if kind == "udp":
                _, flow, (rip, rport), payload = event
                info = self.nat_udp.get(flow)
                if info is None:
                    continue
                packet = udp4_packet(_packed(rip), info["guest_ip"], rport,
                                     info["guest_port"], payload)
                self._count("uplink udp in")
                self.outbox.append(Air(self.data_protected(
                    info["station"].address, b"\x08\x00", packet), awaited=True))
            elif kind == "tcp-connected":
                key = event[1]
                entry = self.conns.get(key)
                if entry is None or entry["conn"] is not None:
                    continue
                self.tcp_isn = (self.tcp_isn + 0x10000) & 0xFFFFFFFF
                conn = entry["conn"] = tcp.Connection(iss=self.tcp_isn)
                seq, window = entry["syn"]
                log.info("uplink: tcp to %s:%d connected; answering the SYN",
                         _dotted(entry["server_ip"]), entry["server_port"])
                for seg in conn.on_segment(seq, 0, tcp.SYN, window, b"",
                                           self.clock):
                    self.outbox.append(self._tcp_frame(entry, seg))
            elif kind == "tcp-failed":
                _, key, reason = event
                entry = self.conns.pop(key, None)
                if entry is None:
                    continue
                self._tcp_refuse(entry["station"], entry["client_ip"],
                                 entry["client_port"], entry["server_ip"],
                                 entry["server_port"], entry["syn"][0], 0,
                                 tcp.SYN, b"",
                                 why="the host could not connect: " + reason)
            elif kind == "tcp-reset":
                _, key, reason = event
                entry = self.conns.pop(key, None)
                if entry is None or entry["conn"] is None:
                    continue
                log.info("uplink: tcp to %s:%d broke on the host (%s); "
                         "resetting the station's end",
                         _dotted(entry["server_ip"]), entry["server_port"], reason)
                seg = entry["conn"].abort()
                if seg is not None:
                    self.outbox.append(self._tcp_frame(entry, seg))
            elif kind == "resolved":
                self._nat_dns_answer(*event[1:])

    def _nat_dns_answer(self, key: tuple, name: str,
                        ips: list[str] | None) -> None:
        """The host's answer to a forwarded name, as the gateway's reply."""
        pending = self.nat_dns.pop(key, None)
        if pending is None:
            return
        station, querier_ip, src_port, parsed, reply_port = pending
        if ips is None:
            reply, what = dns.servfail(parsed), "SERVFAIL"
        elif not ips:
            reply, what = dns.nxdomain(parsed), "NXDOMAIN"
        else:
            reply, what = dns.answer(parsed, [_packed(ip) for ip in ips]), \
                ", ".join(ips)
        self._count("dns reply through the host")
        log.info("dns A %s -> %s for %s (through the host)", name, what,
                 station.address.hex(":"))
        packet = udp4_packet(SERVER_IP, querier_ip, reply_port, src_port, reply)
        self.outbox.append(Air(self.data_protected(station.address, b"\x08\x00",
                                                   packet), awaited=True))

    def _arp(self, station: "Station", arp: bytes) -> bytes | None:
        """An ARP request. Answer one that asks for the gateway."""
        if len(arp) < 28 or int.from_bytes(arp[6:8], "big") != 1:
            return None                         # not a request
        sender_mac, sender_ip = arp[8:14], arp[14:18]
        target_ip = arp[24:28]
        if target_ip != SERVER_IP:              # only the gateway is ours
            return None
        self._count("arp reply")
        reply = (arp[:6] + b"\x00\x02"          # htype/ptype/hlen/plen, op reply
                 + AP_ADDRESS + SERVER_IP       # sender: the gateway
                 + sender_mac + sender_ip)      # target: the asker
        return self.data_protected(station.address, b"\x08\x06", reply)


class FrameChannel:
    """The socket the device asks down. One connection, one peer, one thread."""

    def __init__(self, path: str | Path, point: AccessPoint):
        self.path = str(path)
        self.point = point
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self.gaps: list[float] = []
        self.service: list[float] = []
        self._last: float | None = None

    def start(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(1)
        threading.Thread(target=self._serve, name="wifi-ap", daemon=True).start()
        if self.point.uplink is not None:
            self.point.uplink.start()

    def stop(self) -> None:
        self._stop.set()
        if self.point.uplink is not None:
            self.point.uplink.stop()
        for sock in (self._conn, self._listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def _recv(self, conn: socket.socket, want: int) -> bytes | None:
        out = b""
        while len(out) < want:
            chunk = conn.recv(want - len(out))
            if not chunk:
                return None
            out += chunk
        return out

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self._conn = conn
        log.info("the access point's frame channel is connected")
        try:
            while not self._stop.is_set():
                head = self._recv(conn, HEAD.size)
                if head is None:
                    break
                started = time.monotonic()
                kind, channel, length, clock, free = HEAD.unpack(head)
                frame = self._recv(conn, length) if length else b""
                if length and frame is None:
                    break
                # The high bit acknowledges the last frame offered on a tick
                # (ADR 0095); it is not part of the request's type.
                delivered = bool(kind & REQ_DELIVERED)
                unacked = bool(kind & REQ_UNACKED)
                kind &= ~(REQ_DELIVERED | REQ_UNACKED)
                heard = True
                try:
                    if delivered:
                        self.point.clock = clock
                        self.point.delivered()
                    if unacked:
                        self.point.clock = clock
                        self.point.unacked()
                    if kind == REQ_TX:
                        out = self.point.take(frame, channel, clock)
                        heard = self.point.last_heard
                    elif kind == REQ_TICK:
                        out = self.point.tick(channel, clock, free)
                    else:
                        out = None
                except Exception:
                    # A peer that raises must not take the guest down with it:
                    # say nothing, log it, and let the run carry on so the
                    # failure shows up as silence on the air rather than a
                    # dead emulator.
                    log.exception("the access point raised; answering nothing")
                    out = None
                payload = out.frame if out else b""
                flags = AWAITED if (out and out.awaited) else 0
                if not heard:
                    flags |= REPLY_UNACKED
                # Hand the temporal key to the MAC model when one is waiting
                # (ADR 0094): it rides between the reply header and the frame, so
                # the header's length still counts only the frame.
                key = self.point.pending_key
                self.point.pending_key = None
                if key is not None:
                    flags |= REPLY_KEY
                conn.sendall(REPLY.pack(len(payload), flags)
                             + (key or b"") + payload)
                # How long the device waits on us, against how often it asks.
                # A round trip happens inside a QEMU timer callback, so time
                # spent here is time the guest is stopped and the beacon
                # period grows by.
                now = time.monotonic()
                if self._last is not None:
                    self.gaps.append(now - self._last)
                self.service.append(now - started)
                self._last = now
        except OSError:
            pass
        finally:
            if self.gaps:
                log.info("the device asked %d time(s), every %.1f ms on average "
                         "(worst %.0f ms); this side took %.2f ms on average "
                         "(worst %.0f ms)",
                         len(self.service), 1000 * sum(self.gaps) / len(self.gaps),
                         1000 * max(self.gaps),
                         1000 * sum(self.service) / len(self.service),
                         1000 * max(self.service))
            log.info("the frame channel closed after %s",
                     ", ".join(f"{v} {k}" for k, v in
                               sorted(self.point.counts.items())) or "nothing")
