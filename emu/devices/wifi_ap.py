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
            # A fresh handshake, so its first message is a first send and must
            # not wait out the previous one's retry deadline.
            station.first_at = 0
        return (header(ASSOC_RESPONSE, station.address, self._next())
                + bytes([0x11 if self.network.wpa2 else 0x01, 0x00,
                         0x00, 0x00, 0x01, 0xC0,
                         0x01, 0x04, 0x82, 0x84, 0x8B, 0x96]))

    def _data(self, frame: bytes) -> bytes | None:
        """A data frame from the station. Only EAPOL is understood so far."""
        station = self.stations.get(frame[10:16])
        if station is None or station.shake is None:
            return None
        body = frame[24:]
        if body[:8] != bytes.fromhex("aaaa03000000888e"):
            return None
        reply = station.shake.take(body[8:])
        if reply is not None:
            # Message two verified, so message one arrived and is no longer
            # owed. Anything that did not verify leaves it owed, and the
            # deadline in tick() offers it again.
            station.owes_first = False
        self._count("eapol %d" % (2 if not station.shake.done else 4))
        if station.shake.done:
            self._count("handshake done")
            log.info("the station %s completed the four-way handshake",
                     station.address.hex(":"))
        if reply is None:
            return None
        self._count("eapol 3")
        return self.data(station.address, reply)


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
