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

from emu import wpa2

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
FIRST_RETRY_MS = 100
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


# The little network the peer hands out once a station connects (ADR 0084).
# There is one station, so a "DHCP server" here is a table of one: a fixed
# address, with the access point as gateway and DNS.
SERVER_IP = bytes([192, 168, 4, 1])
CLIENT_IP = bytes([192, 168, 4, 2])
NETMASK = bytes([255, 255, 255, 0])
LEASE_SECONDS = 7200


def ip_checksum(data: bytes) -> int:
    """The one's-complement sum an IPv4 or UDP header carries."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


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
    total = 20 + len(udp)
    ip = (struct.pack(">BBHHHBBH", 0x45, 0, total, 0, 0x4000, 64, 17, 0)
          + src_ip + dst_ip)
    ip = ip[:10] + struct.pack(">H", ip_checksum(ip)) + ip[12:]
    return ip + udp


def dhcp_reply(xid: bytes, chaddr: bytes, message_type: int,
               yiaddr: bytes) -> bytes:
    """A BOOTP reply: an OFFER (type 2) or an ACK (type 5).

    The client's transaction id and hardware address are echoed, the offered
    address goes in yiaddr, and the options carry what the DISCOVER asked for:
    the mask, the router, the DNS and a lease.
    """
    bootp = bytes([2, 1, 6, 0])                           # op reply, ethernet, hlen 6, hops 0
    bootp += xid + struct.pack(">HH", 0, 0)               # xid, secs, flags
    bootp += bytes(4) + yiaddr + SERVER_IP + bytes(4)     # ciaddr, yiaddr, siaddr, giaddr
    bootp += chaddr + bytes(16 - len(chaddr))             # chaddr, padded to 16
    bootp += bytes(64) + bytes(128)                       # sname, file
    bootp += bytes.fromhex("63825363")                    # magic cookie
    opts = bytes([53, 1, message_type])
    opts += bytes([54, 4]) + SERVER_IP                    # server identifier
    opts += bytes([51, 4]) + struct.pack(">I", LEASE_SECONDS)
    opts += bytes([1, 4]) + NETMASK                       # subnet mask
    opts += bytes([3, 4]) + SERVER_IP                     # router
    opts += bytes([6, 4]) + SERVER_IP                     # DNS
    opts += bytes([255])                                  # end
    return bootp + opts


def _dns_name(labels: bytes) -> str:
    """The dotted name a DNS QNAME's length-prefixed labels spell, for the log."""
    parts, i = [], 0
    while i < len(labels) and labels[i]:
        length = labels[i]
        parts.append(labels[i + 1:i + 1 + length].decode("ascii", "replace"))
        i += 1 + length
    return ".".join(parts)


def header(kind: int, to: bytes, sequence: int, frm: bytes = AP_ADDRESS,
           bssid: bytes = AP_ADDRESS) -> bytes:
    """Twenty-four bytes of management header.

    The sequence control field carries the number in its top twelve bits and a
    fragment number in the bottom four, which is always zero here.
    """
    return (bytes([kind, 0x00, 0x00, 0x00]) + to + frm + bssid
            + ((sequence & 0xFFF) << 4).to_bytes(2, "little"))


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

    def body(self, channel: int) -> bytes:
        """The fixed fields and elements a beacon and a probe response share.

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
    shake: wpa2.Handshake | None = None
    # Message one is sent on a tick rather than in reply to the association
    # request, because a real access point does not answer a frame twice.
    # It stays owed until message two arrives: handing it to the radio is no
    # evidence it was heard (ADR 0067).
    owes_first: bool = False
    first_at: int = 0
    # When the association response went, so message one can be held back
    # until the station is ready for it (ADR 0076).
    associated_at: int = 0


class AccessPoint:
    """Answers what the station sends, and beacons between times."""

    def __init__(self, network: Network):
        self.network = network
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
        log.info("access point %r on channel %d, %s", network.ssid,
                 network.channel,
                 "WPA2 with a master key" if self.master
                 else ("WPA2 but no passphrase" if network.wpa2 else "open"))

    def _next(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFFF
        return self.sequence

    def _count(self, what: str) -> None:
        self.counts[what] = self.counts.get(what, 0) + 1

    # ---------------------------------------------------------------- ticks

    def tick(self, channel: int, clock: int, free: int = 9) -> Air | None:
        """What to put on the air when nothing was asked.

        A handshake that owes its first message gets it here: the association
        response has already gone, and this is the next thing the station is
        waiting for.
        """
        self.channel = channel or self.channel
        # Anything owed comes first, and is never held back for a schedule.
        for station in self.stations.values():
            if not (station.owes_first and station.shake is not None):
                continue
            if clock - station.associated_at < FIRST_HOLD_MS:
                continue                # not ready for it yet; see ADR 0076
            again = station.first_at != 0
            if again and clock - station.first_at < FIRST_RETRY_MS:
                continue                # sent recently; give it time to reply
            station.first_at = clock
            self._count("eapol 1 again" if again else "eapol 1")
            if not again:
                log.info("at %d ms handshake message one goes to %s", clock,
                         station.address.hex(":"))
            self.last_beacon = clock
            return Air(self.data(station.address, station.shake.first()),
                       awaited=True)
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
        # one is twenty milliseconds away.
        return Air(header(BEACON, BROADCAST, self._next())
                   + self.network.body(channel))

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

        The mirror of what the station sends once it is connected (ADR 0084):
        the cipher is identity, so the payload rides plaintext behind an
        eight-byte CCMP header, and an eight-byte trailer stands in for the MIC
        the station strips. Only the packet number has to advance.
        """
        fc = bytes([DATA, 0x42])        # data, From-DS, Protected
        head = (fc + b"\x00\x00" + to + AP_ADDRESS + AP_ADDRESS
                + ((self._next() & 0xFFF) << 4).to_bytes(2, "little"))
        self.pn += 1
        pn = self.pn
        ccmp = bytes([pn & 0xFF, (pn >> 8) & 0xFF, 0x00, 0x20,
                      (pn >> 16) & 0xFF, (pn >> 24) & 0xFF, 0x00, 0x00])
        llc = bytes.fromhex("aaaa03000000") + ethertype
        return head + ccmp + llc + payload + bytes(8)

    def take(self, frame: bytes, channel: int, clock: int = 0) -> Air | None:
        """A frame the guest transmitted. What to send back, or nothing.

        Everything this returns answers something the station just sent, so
        the station is waiting for it by construction and it is marked so.
        """
        answer = self._answer(frame, channel, clock)
        return Air(answer, awaited=True) if answer else None

    def _answer(self, frame: bytes, channel: int,
                clock: int = 0) -> bytes | None:
        """Which frame answers this one, as bytes."""
        self.channel = channel or self.channel
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
        return (header(AUTH, station.address, self._next())
                + bytes([0x00, 0x00, 0x02, 0x00, 0x00, 0x00]))

    def _assoc(self, station: Station, clock: int = 0) -> bytes:
        """Accepted, with association identifier one.

        And, on a protected network, the handshake starts: its first message
        goes out on the next tick rather than now, because one frame gets one
        reply.
        """
        self._count("assoc")
        station.associated = True
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
            # not wait out the previous one's retry deadline.
            station.first_at = 0
        return (header(ASSOC_RESPONSE, station.address, self._next())
                + bytes([0x11 if self.network.wpa2 else 0x01, 0x00,
                         0x00, 0x00, 0x01, 0xC0,
                         0x01, 0x04, 0x82, 0x84, 0x8B, 0x96]))

    def _data(self, frame: bytes) -> bytes | None:
        """A data frame from the station. Only EAPOL is understood so far.

        Every way out of here used to be a silent `return None`, so a frame in
        a shape this did not expect vanished without appearing in any count --
        which is indistinguishable from a station that sent nothing, and that
        is the question this code is usually being asked.
        """
        self._count("data frame in")
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
        # A Protected frame carries an eight-byte CCMP header before the LLC.
        # The cipher is identity here (ADR 0084), so the payload after it is
        # plaintext -- just skip the header. EAPOL is sent unprotected, before
        # a key exists, so it has no CCMP header.
        body = frame[at + 8:] if (frame[1] & 0x40) else frame[at:]
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
        ihl = (ip[0] & 0x0F) * 4
        if ip[9] == 6:                          # TCP
            tcp = ip[ihl:]
            if len(tcp) >= 14 and (tcp[13] & 0x02) and not (tcp[13] & 0x10):
                # A SYN with no ACK: a connection the station wants opened and
                # nothing here answers. The port names the service it is after
                # (ADR 0086); no TCP layer serves it yet.
                self._count("tcp syn to %d" % int.from_bytes(tcp[2:4], "big"))
            return None
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
            return self._dns(station, src_port, payload)
        if dst_port == 123:                     # NTP
            return self._ntp(station, src_port, payload)
        if dst_port == 5353:                    # mDNS (.local lookups)
            return self._mdns(station, src_port, payload)
        self._count("udp, unanswered to %d" % dst_port)
        return None

    def _dhcp(self, station: "Station", dhcp: bytes) -> bytes | None:
        """A BOOTP request: answer a DISCOVER with an OFFER, a REQUEST with ACK."""
        cookie = dhcp.find(bytes.fromhex("63825363"))
        if cookie < 0:
            return None
        xid = dhcp[4:8]
        chaddr = dhcp[28:34]
        message_type = None
        opts = dhcp[cookie + 4:]
        i = 0
        while i < len(opts):
            code = opts[i]
            if code == 255:                     # end
                break
            if code == 0:                       # pad
                i += 1
                continue
            length = opts[i + 1]
            if code == 53:                      # DHCP message type
                message_type = opts[i + 2]
            i += 2 + length
        if message_type == 1:                   # DISCOVER
            kind, name = 2, "dhcp offer"
        elif message_type == 3:                 # REQUEST
            kind, name = 5, "dhcp ack"
        else:
            return None
        self._count(name)
        log.info("dhcp %s to %s: %s", "OFFER" if kind == 2 else "ACK",
                 station.address.hex(":"), ".".join(map(str, CLIENT_IP)))
        bootp = dhcp_reply(xid, chaddr, kind, CLIENT_IP)
        packet = udp4_packet(SERVER_IP, bytes([255, 255, 255, 255]), 67, 68, bootp)
        return self.data_protected(station.address, b"\x08\x00", packet)

    def _a_reply(self, dns: bytes):
        """Build the answer to a DNS/mDNS A query: the same name, pointed at the
        gateway. Returns (name, reply-bytes) or None if it is not an A query.
        A unicast DNS query and the firmware's `.local` mDNS query have the same
        body, so both go through here."""
        if len(dns) < 13 or int.from_bytes(dns[4:6], "big") != 1:
            return None                          # not a single-question query
        i = 12                                    # walk the QNAME labels to their end
        while i < len(dns) and dns[i]:
            i += 1 + dns[i]
        question = dns[12:i + 5]                  # QNAME + null + QTYPE + QCLASS
        if question[-4:-2] != b"\x00\x01":        # only A records
            return None
        head = dns[0:2] + b"\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00"
        answer = (b"\xc0\x0c" + b"\x00\x01\x00\x01"      # name pointer, type A, class IN
                  + struct.pack(">I", 60) + b"\x00\x04" + SERVER_IP)
        return _dns_name(dns[12:i]), head + question + answer

    def _dns(self, station: "Station", src_port: int, dns: bytes) -> bytes | None:
        """A DNS query. Answer an A question by pointing the name at the gateway.

        The station resolves its NTP servers (pool.ntp.org, time.nist.gov) and
        then talks NTP to whatever address comes back. Returning the gateway --
        the peer itself -- is what routes that NTP query here to be answered.
        """
        made = self._a_reply(dns)
        if made is None:
            self._count("dns, not an A query")
            return None
        name, reply = made
        self._count("dns reply")
        log.info("dns A %s -> %s for %s", name, ".".join(map(str, SERVER_IP)),
                 station.address.hex(":"))
        packet = udp4_packet(SERVER_IP, CLIENT_IP, 53, src_port, reply)
        return self.data_protected(station.address, b"\x08\x00", packet)

    def _mdns(self, station: "Station", src_port: int, dns: bytes) -> bytes | None:
        """An mDNS A query (the firmware's `.local` lookups). Answered like DNS,
        unicast back to the querier -- the query is unicast-shaped, from an
        ephemeral port, so it wants a unicast reply (ADR 0086)."""
        made = self._a_reply(dns)
        if made is None:
            return None
        name, reply = made
        self._count("mdns reply")
        log.info("mdns A %s -> %s for %s", name, ".".join(map(str, SERVER_IP)),
                 station.address.hex(":"))
        packet = udp4_packet(SERVER_IP, CLIENT_IP, 5353, src_port, reply)
        return self.data_protected(station.address, b"\x08\x00", packet)

    def _ntp(self, station: "Station", src_port: int, ntp: bytes) -> bytes | None:
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
        packet = udp4_packet(SERVER_IP, CLIENT_IP, 123, src_port, reply)
        return self.data_protected(station.address, b"\x08\x00", packet)

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

    def stop(self) -> None:
        self._stop.set()
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
                try:
                    if kind == REQ_TX:
                        out = self.point.take(frame, channel, clock)
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
                conn.sendall(REPLY.pack(len(payload), flags) + payload)
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
