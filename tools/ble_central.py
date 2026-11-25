#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A Bluetooth Low Energy central on a run's Bluetooth air (ADRs 0045, 0168).

It connects to the board, keeps the link with a packet every connection
interval, answers what the peripheral's link layer asks (its features, its
data length), and speaks ATT over L2CAP: MTU exchange, primary services,
characteristics, reads and writes. It pairs, too (ADR 0171): LE Secure
Connections passkey entry over the Security Manager, then the link layer's
encryption, AES-CCM on every packet after it. Standard library only.

Frames on the air start with their access address (ADR 0173), then a data
channel PDU's first header octet and its payload (ADR 0168), or an
advertisement's type octet, the advertiser's address and its data (ADR
0171); the connection request is ADR 0047's.
Nothing here is retransmitted by the link layer, which assumes delivery, so
an ATT request that gets no answer is sent again.

    python3 tools/ble_central.py AIR_DIR services
    python3 tools/ble_central.py AIR_DIR meshtastic      # want_config, FromRadio
    python3 tools/ble_central.py AIR_DIR meshtastic --passkey 123456

Run where the board's air directory is, which is the build container for a
run started there.
"""

from __future__ import annotations

import argparse
import secrets
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.air import AirMedium  # noqa: E402
from tools import ble_crypto  # noqa: E402

PDU_CONNECT_IND = 0x05
TX_ADD, RX_ADD = 0x40, 0x80             # an advertising header's address types
LLID_CONTINUE, LLID_START, LLID_CONTROL = 1, 2, 3
LL_ENC_REQ, LL_ENC_RSP, LL_START_ENC_REQ, LL_START_ENC_RSP = 0x03, 0x04, 0x05, 0x06
LL_REJECT_IND, LL_REJECT_EXT_IND = 0x0D, 0x11
LL_FEATURE_REQ, LL_FEATURE_RSP, LL_VERSION_IND = 0x08, 0x09, 0x0C
LL_PERIPHERAL_FEATURE_REQ, LL_LENGTH_REQ, LL_LENGTH_RSP = 0x0E, 0x14, 0x15
# What every link layer accepts in one packet, before or without a length
# exchange; larger L2CAP frames go in pieces of this.
LL_PAYLOAD = 27
CID_ATT, CID_SMP = 4, 6
# The Security Manager (Core Specification Vol 3, Part H, 3).
SMP_PAIRING_REQ, SMP_PAIRING_RSP, SMP_CONFIRM, SMP_RANDOM = 0x01, 0x02, 0x03, 0x04
SMP_PAIRING_FAILED, SMP_PUBLIC_KEY, SMP_DHKEY_CHECK = 0x05, 0x0C, 0x0D
IO_KEYBOARD_ONLY = 0x02
AUTH_BONDING, AUTH_MITM, AUTH_SC = 0x01, 0x04, 0x08
SMP_FAILURES = {0x01: "passkey entry failed", 0x02: "OOB not available",
                0x03: "authentication requirements", 0x04: "confirm value failed",
                0x05: "pairing not supported", 0x06: "encryption key size",
                0x08: "unspecified reason", 0x09: "repeated attempts",
                0x0B: "DHKey check failed"}
ATT_ERROR, ATT_MTU_REQ, ATT_MTU_RSP = 0x01, 0x02, 0x03
ATT_FIND_INFO_REQ, ATT_FIND_INFO_RSP = 0x04, 0x05
ATT_READ_BY_TYPE_REQ, ATT_READ_BY_TYPE_RSP = 0x08, 0x09
ATT_READ_REQ, ATT_READ_RSP = 0x0A, 0x0B
ATT_READ_BY_GROUP_REQ, ATT_READ_BY_GROUP_RSP = 0x10, 0x11
ATT_WRITE_REQ, ATT_WRITE_RSP = 0x12, 0x13
# What a server sends unasked: a notification, or an indication, which the
# client confirms (Vol 3, Part F, 3.4.7).
ATT_NOTIFY, ATT_INDICATE, ATT_CONFIRM = 0x1B, 0x1D, 0x1E
CCCD = "2902"                             # Client Characteristic Configuration
ATT_ERRORS = {0x01: "invalid handle", 0x02: "read not permitted",
              0x03: "write not permitted", 0x05: "insufficient authentication",
              0x0A: "attribute not found", 0x0E: "unlikely error",
              0x0F: "insufficient encryption"}
PRIMARY_SERVICE, CHARACTERISTIC = 0x2800, 0x2803
# A 30 ms interval, the one tests/test_bluetooth_connect.py measures.
INTERVAL_UNITS = 24
INTERVAL_S = INTERVAL_UNITS * 1.25 / 1000

# Meshtastic's service and characteristics (its src/BluetoothCommon.h).
MESHTASTIC_SERVICE = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM = "ed9da18c-a800-4f66-a670-aa7547e34453"


def uuid_text(raw: bytes) -> str:
    """A UUID as ATT carries it (little-endian), written as people write it."""
    if len(raw) == 2:
        return f"{struct.unpack('<H', raw)[0]:04x}"
    h = raw[::-1].hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# The central's own address, public, least significant octet first.
INITIATOR = bytes.fromhex("ccbbaa286f24")
NO_ADDRESS = bytes.fromhex("0100000000c0")
# The access addresses a frame starts with (ADR 0173): advertising's, and the
# one this central's connection request gives its connection.
ADV_AA, CONN_AA = 0x8E89BED6, 0xAF9AB4C3


def on(aa: int, pdu: bytes) -> bytes:
    """A frame as the air carries it: the access address, then the PDU."""
    return struct.pack("<I", aa) + pdu


def split(frame: bytes) -> tuple[int, bytes]:
    """A frame's access address and PDU; an address of -1 if it has none."""
    if len(frame) < 5:
        return -1, b""
    return struct.unpack_from("<I", frame)[0], frame[4:]


def connect_request(advertiser: bytes = NO_ADDRESS, random: bool = False) -> bytes:
    """A CONNECT_IND from INITIATOR (public) to the advertiser heard."""
    data = (struct.pack("<I", CONN_AA) + bytes.fromhex("555555") + bytes([3])
            + struct.pack("<HHHH", 1, INTERVAL_UNITS, 0, 72)
            + bytes.fromhex("ffffffff1f") + bytes([0x0D]))
    return (bytes([PDU_CONNECT_IND | (RX_ADD if random else 0)])
            + INITIATOR + advertiser + data)


class LinkLost(Exception):
    """The peripheral advertises again: the connection is gone."""


class PairingError(Exception):
    """The Security Manager or the link's encryption start failed."""


class AttError(Exception):
    def __init__(self, opcode: int, handle: int, code: int):
        super().__init__(f"ATT error 0x{code:02x} ({ATT_ERRORS.get(code, '?')}) "
                         f"on request 0x{opcode:02x}, handle 0x{handle:04x}")
        self.code = code


class Central:
    def __init__(self, air: str | Path, name: str = "central"):
        self.air = AirMedium(air, name)
        self.sn = self.nesn = 0
        self.outbox: list[tuple[int, bytes]] = []
        self.l2cap = bytearray()
        self.frames: list[tuple[int, bytes]] = []
        self.control: list[bytes] = []        # every control PDU heard
        self.connected_at: float | None = None
        self.heard_since_send = True          # the peripheral answered our last packet
        self.sent_at = 0.0
        self.adverts_since = 0                # advertisements heard while connected
        self.peer: bytes | None = None        # the advertiser's address, LSO first
        self.peer_random = False
        # The link's encryption (ADR 0171): its session key in FIPS-197 order,
        # the IV, and each direction's packet counter, once started.
        self.sk: bytes | None = None
        self.iv = b""
        self.tx_crypt = self.rx_crypt = False
        self.tx_count = self.rx_count = 0
        self.mic_failures = 0
        self.starting: tuple[bytes, bytes, bytes] | None = None   # LTK, SKDm, IVm
        self.notifications: list[tuple[int, bytes]] = []   # (handle, value), oldest first

    def close(self) -> None:
        self.air.close()

    # -- the link layer --------------------------------------------------

    def connect(self, wait_s: float = 240, address: bytes | None = None) -> bool:
        """Wait to hear a board advertise -- the one with this address, if
        given, least significant octet first -- then ask it to connect."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            adverts = [pdu for aa, pdu in map(split, self.air.poll())
                       if aa == ADV_AA and len(pdu) >= 7 and not pdu[0] & 0x0F
                       and (address is None or pdu[1:7] == address)]
            if adverts:
                self.peer, self.peer_random = adverts[0][1:7], bool(adverts[0][0] & TX_ADD)
                self.sn = self.nesn = 0
                self.heard_since_send = True
                self.outbox.clear()
                self.frames.clear()
                self.l2cap = bytearray()
                self.sk, self.tx_crypt, self.rx_crypt = None, False, False
                self.starting = None
                self.tx_count = self.rx_count = self.mic_failures = 0
                self.air.send(on(ADV_AA, connect_request(self.peer, self.peer_random)))
                self.connected_at = time.monotonic()
                self.adverts_since = 0
                return True
            time.sleep(0.5)
        return False

    def _nonce(self, counter: int, from_central: bool) -> bytes:
        """Vol 6, Part E, 2.1: the 39-bit counter, the direction bit, the IV."""
        return (counter | (from_central << 39)).to_bytes(5, "little") + self.iv

    def _send(self, llid: int, payload: bytes = b"") -> None:
        header = llid | self.nesn << 2 | self.sn << 3
        if self.tx_crypt and payload:
            payload = ble_crypto.ccm_encrypt(self.sk, self._nonce(self.tx_count, True),
                                             payload, aad=bytes([header & 0xE3]))
            self.tx_count += 1
        self.air.send(on(CONN_AA, bytes([header]) + payload))
        self.sn ^= 1

    def send_l2cap(self, cid: int, pdu: bytes) -> None:
        """An L2CAP frame, in packets no longer than any link layer accepts."""
        frame = struct.pack("<HH", len(pdu), cid) + pdu
        for at in range(0, len(frame), LL_PAYLOAD):
            self.outbox.append((LLID_START if at == 0 else LLID_CONTINUE,
                                frame[at:at + LL_PAYLOAD]))

    def _listen(self) -> None:
        for aa, frame in map(split, self.air.poll()):
            if aa == ADV_AA:
                # Its peripheral advertising well after it was asked to
                # connect has dropped the link (or restarted its stack).
                # Another board's advertising says nothing about this link.
                if (self.connected_at is not None and frame[1:7] == self.peer
                        and time.monotonic() > self.connected_at + 2):
                    self.adverts_since += 1
                continue
            if aa != CONN_AA:
                continue                       # another connection's
            llid = frame[0] & 3
            self.nesn ^= 1
            self.heard_since_send = True
            body = frame[1:]
            if self.rx_crypt and body:
                body = ble_crypto.ccm_decrypt(self.sk, self._nonce(self.rx_count, False),
                                              body, aad=bytes([frame[0] & 0xE3]))
                if body is None:
                    self.mic_failures += 1
                    continue
                self.rx_count += 1
            if llid == LLID_CONTROL and body:
                self.control.append(body)
                if body[0] == LL_ENC_RSP and self.starting and len(body) >= 13:
                    ltk, skdm, ivm = self.starting
                    self.sk = ble_crypto.aes_encrypt(ltk[::-1], (skdm + body[1:9])[::-1])
                    self.iv = ivm + body[9:13]
                elif body[0] == LL_START_ENC_REQ and self.sk:
                    # Vol 6, Part B, 5.1.3.1: answered encrypted, and from
                    # here on both directions are.
                    self.tx_crypt = self.rx_crypt = True
                    self.outbox.insert(0, (LLID_CONTROL, bytes([LL_START_ENC_RSP])))
                if body[0] in (LL_PERIPHERAL_FEATURE_REQ, LL_FEATURE_REQ):
                    self.outbox.append((LLID_CONTROL, bytes([LL_FEATURE_RSP]) + bytes(8)))
                elif body[0] == LL_LENGTH_REQ:
                    self.outbox.append((LLID_CONTROL, bytes([LL_LENGTH_RSP]) + body[1:9]))
                continue
            if llid == LLID_START:
                self.l2cap = bytearray(body)
            elif body:
                self.l2cap += body
            if len(self.l2cap) >= 4:
                length, cid = struct.unpack_from("<HH", self.l2cap)
                if len(self.l2cap) >= 4 + length:
                    pdu = bytes(self.l2cap[4:4 + length])
                    self.l2cap = bytearray()
                    if cid == CID_ATT and pdu[:1] in (bytes([ATT_NOTIFY]),
                                                      bytes([ATT_INDICATE])) and len(pdu) >= 3:
                        # Unasked: kept apart, so no request mistakes it for
                        # its answer; an indication is confirmed.
                        self.notifications.append((struct.unpack_from("<H", pdu, 1)[0],
                                                   pdu[3:]))
                        if pdu[0] == ATT_INDICATE:
                            self.send_l2cap(CID_ATT, bytes([ATT_CONFIRM]))
                    else:
                        self.frames.append((cid, pdu))

    def step(self) -> None:
        """One connection event: listen, then send what is due, or an empty
        packet so the peripheral knows its central is there.

        A central sends once per event and the peripheral answers every
        event (an empty packet if it has nothing, ADR 0169), so the next
        packet waits for that answer. Sent on a timer instead, packets piled
        up in front of a guest running slower than the wall -- under -icount
        and a busy host, hundreds deep -- and a request waited behind them
        all. A packet that got no answer within a second is followed anyway,
        in case it was lost."""
        self._listen()
        if self.adverts_since >= 3:
            raise LinkLost("the peripheral is advertising again")
        if self.heard_since_send or time.monotonic() > self.sent_at + 1.0:
            self._send(*(self.outbox.pop(0) if self.outbox else (LLID_CONTINUE,)))
            self.heard_since_send = False
            self.sent_at = time.monotonic()
        time.sleep(0.005)

    def run(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.step()

    def version(self, timeout_s: float = 30) -> bytes | None:
        self.outbox.append((LLID_CONTROL, bytes([LL_VERSION_IND, 0x09])
                            + struct.pack("<HH", 0x0059, 1)))
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            self.step()
            for body in self.control:
                if body[0] == LL_VERSION_IND:
                    return body
        return None

    # -- ATT over L2CAP -------------------------------------------------------

    def transact(self, pdu: bytes, answers: tuple[int, ...], tries: int = 4,
                 wait_s: float = 15) -> bytes:
        """Send an ATT request and return its response, sending it again if
        nothing came back; an error response raises AttError."""
        for _ in range(tries):
            self.send_l2cap(CID_ATT, pdu)
            end = time.monotonic() + wait_s
            while time.monotonic() < end:
                self.step()
                while self.frames:
                    cid, p = self.frames.pop(0)
                    if cid != CID_ATT or not p:
                        continue
                    if p[0] == ATT_ERROR and len(p) >= 5 and p[1] == pdu[0]:
                        _, opcode, handle, code = struct.unpack_from("<BBHB", p)
                        raise AttError(opcode, handle, code)
                    if p[0] in answers:
                        return p
        raise TimeoutError(f"no answer to ATT request 0x{pdu[0]:02x}")

    def exchange_mtu(self, mtu: int = 247) -> int:
        p = self.transact(bytes([ATT_MTU_REQ]) + struct.pack("<H", mtu), (ATT_MTU_RSP,))
        return struct.unpack_from("<H", p, 1)[0]

    def services(self) -> list[tuple[int, int, str]]:
        found, start = [], 1
        while start <= 0xFFFF:
            try:
                p = self.transact(bytes([ATT_READ_BY_GROUP_REQ])
                                  + struct.pack("<HHH", start, 0xFFFF, PRIMARY_SERVICE),
                                  (ATT_READ_BY_GROUP_RSP,))
            except AttError as error:
                if error.code == 0x0A:
                    break
                raise
            size = p[1]
            for at in range(2, len(p) - size + 1, size):
                first, last = struct.unpack_from("<HH", p, at)
                found.append((first, last, uuid_text(p[at + 4:at + size])))
                start = last + 1
        return found

    def characteristics(self, first: int, last: int) -> dict[str, tuple[int, int]]:
        """uuid -> (value handle, properties) within a service's handles."""
        found, start = {}, first
        while start <= last:
            try:
                p = self.transact(bytes([ATT_READ_BY_TYPE_REQ])
                                  + struct.pack("<HHH", start, last, CHARACTERISTIC),
                                  (ATT_READ_BY_TYPE_RSP,))
            except AttError as error:
                if error.code == 0x0A:
                    break
                raise
            size = p[1]
            for at in range(2, len(p) - size + 1, size):
                declaration, properties, value = struct.unpack_from("<HBH", p, at)
                found[uuid_text(p[at + 5:at + size])] = (value, properties)
                start = declaration + 1
        return found

    # -- pairing and encryption (ADR 0171) ------------------------------------

    def smp_wait(self, code: int, wait_s: float = 120) -> bytes:
        """The Security Manager's next PDU of this kind; a Pairing Failed
        raises. Its procedures time out after thirty seconds of the
        peripheral's time, which under -icount is longer in the wall's."""
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            self.step()
            for i, (cid, p) in enumerate(self.frames):
                if cid != CID_SMP or not p:
                    continue
                del self.frames[i]
                if p[0] == SMP_PAIRING_FAILED:
                    reason = p[1] if len(p) > 1 else 0
                    raise PairingError(f"pairing failed: 0x{reason:02x} "
                                       f"({SMP_FAILURES.get(reason, '?')})")
                if p[0] == code:
                    return p
                break
        raise TimeoutError(f"no Security Manager PDU 0x{code:02x}")

    def _smp_fail(self, reason: int) -> None:
        self.send_l2cap(CID_SMP, bytes([SMP_PAIRING_FAILED, reason]))
        self.run(1.0)
        raise PairingError(f"pairing abandoned: 0x{reason:02x} "
                           f"({SMP_FAILURES.get(reason, '?')})")

    def pair(self, passkey: int) -> bytes:
        """LE Secure Connections passkey entry, this side typing the passkey
        the peripheral shows (Vol 3, Part H, 2.3.5.6.3). Returns the LTK,
        least significant octet first."""
        if self.peer is None:
            raise PairingError("no advertiser address heard; pairing needs it")
        auth = AUTH_BONDING | AUTH_MITM | AUTH_SC
        # No keys distributed either way: Secure Connections derives the LTK.
        preq = bytes([SMP_PAIRING_REQ, IO_KEYBOARD_ONLY, 0, auth, 16, 0, 0])
        self.send_l2cap(CID_SMP, preq)
        pres = self.smp_wait(SMP_PAIRING_RSP)
        if not pres[3] & AUTH_SC:
            self._smp_fail(0x03)
        private, public = ble_crypto.p256_keypair()
        pka = ble_crypto.public_key_le(public)
        self.send_l2cap(CID_SMP, bytes([SMP_PUBLIC_KEY]) + pka)
        pkb = self.smp_wait(SMP_PUBLIC_KEY)[1:65]
        peer_key = ble_crypto.public_key_from_le(pkb)
        if not ble_crypto.on_curve(peer_key):
            self._smp_fail(0x08)
        dhkey = ble_crypto.p256_dhkey(private, peer_key)
        # Twenty rounds, one per bit of the passkey, each committing to a
        # fresh nonce before revealing it.
        for i in range(20):
            z = 0x80 | (passkey >> i) & 1
            na = secrets.token_bytes(16)
            self.send_l2cap(CID_SMP, bytes([SMP_CONFIRM])
                            + ble_crypto.f4(pka[:32], pkb[:32], na, z))
            cb = self.smp_wait(SMP_CONFIRM)[1:17]
            self.send_l2cap(CID_SMP, bytes([SMP_RANDOM]) + na)
            nb = self.smp_wait(SMP_RANDOM)[1:17]
            if ble_crypto.f4(pkb[:32], pka[:32], nb, z) != cb:
                self._smp_fail(0x04)
        a = ble_crypto.typed_address(INITIATOR, random=False)
        b = ble_crypto.typed_address(self.peer, random=self.peer_random)
        mackey, ltk = ble_crypto.f5(dhkey, na, nb, a, b)
        r = passkey.to_bytes(16, "little")
        io_a, io_b = bytes(preq[1:4]), bytes(pres[1:4])
        self.send_l2cap(CID_SMP, bytes([SMP_DHKEY_CHECK])
                        + ble_crypto.f6(mackey, na, nb, r, io_a, a, b))
        eb = self.smp_wait(SMP_DHKEY_CHECK)[1:17]
        if ble_crypto.f6(mackey, nb, na, r, io_b, b, a) != eb:
            self._smp_fail(0x0B)
        return ltk

    def encrypt(self, ltk: bytes, wait_s: float = 120) -> None:
        """The link layer's encryption start (Vol 6, Part B, 5.1.3.1) with a
        Secure Connections LTK: no Rand, no EDIV. The session key is
        e(LTK, SKDm || SKDs) and the IV IVm || IVs, the central's half least
        significant in both."""
        skdm, ivm = secrets.token_bytes(8), secrets.token_bytes(4)
        self.control.clear()
        self.starting = (ltk, skdm, ivm)
        self.outbox.append((LLID_CONTROL, bytes([LL_ENC_REQ]) + bytes(10) + skdm + ivm))
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            self.step()
            for body in self.control:
                if body[0] in (LL_REJECT_IND, LL_REJECT_EXT_IND):
                    raise PairingError(f"encryption refused: {body.hex()}")
                elif body[0] == LL_START_ENC_RSP and self.rx_crypt:
                    return
            if self.mic_failures:
                raise PairingError("the peripheral's packets do not decrypt")
        raise TimeoutError("the link's encryption did not start")

    def descriptors(self, first: int, last: int) -> dict[str, int]:
        """uuid -> handle of what lies between a characteristic's value and
        the next declaration (Find Information)."""
        found, start = {}, first
        while start <= last:
            try:
                p = self.transact(bytes([ATT_FIND_INFO_REQ]) + struct.pack("<HH", start, last),
                                  (ATT_FIND_INFO_RSP,))
            except AttError as error:
                if error.code == 0x0A:
                    break
                raise
            size = 4 if p[1] == 1 else 18          # 16-bit UUIDs, or 128-bit
            for at in range(2, len(p) - size + 1, size):
                handle = struct.unpack_from("<H", p, at)[0]
                uuid = uuid_text(p[at + 2:at + size])
                if uuid == f"{CHARACTERISTIC:04x}":
                    return found                    # the next characteristic begins
                found[uuid] = handle
                start = handle + 1
        return found

    def subscribe(self, value_handle: int, last: int) -> int:
        """Ask for notifications of a characteristic, as a phone app does:
        write 1 to its Client Characteristic Configuration descriptor, found
        between its value and `last`. Returns the descriptor's handle."""
        cccd = self.descriptors(value_handle + 1, last).get(CCCD)
        if cccd is None:
            raise AttError(ATT_FIND_INFO_REQ, value_handle + 1, 0x0A)
        self.write(cccd, struct.pack("<H", 1))
        return cccd

    def notified(self, handle: int, wait_s: float = 60) -> bytes | None:
        """The next notification of this handle, waiting for one."""
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            for i, (h, value) in enumerate(self.notifications):
                if h == handle:
                    del self.notifications[i]
                    return value
            self.step()
        return None

    def read(self, handle: int) -> bytes:
        return self.transact(bytes([ATT_READ_REQ]) + struct.pack("<H", handle),
                             (ATT_READ_RSP,))[1:]

    def write(self, handle: int, value: bytes) -> None:
        self.transact(bytes([ATT_WRITE_REQ]) + struct.pack("<H", handle) + value,
                      (ATT_WRITE_RSP,))


def session(central: Central, command: str, passkey: int | None = None) -> int:
    version = central.version()
    if version:
        _op, ver, company, sub = struct.unpack("<BBHH", version[:6])
        print(f"link: version {ver}, company 0x{company:04x}, subversion 0x{sub:04x}")
    print(f"ATT MTU {min(247, central.exchange_mtu())}")
    services = central.services()
    for first, last, uuid in services:
        print(f"service 0x{first:04x}-0x{last:04x} {uuid}")
    if command != "meshtastic":
        return 0
    mesh = next((s for s in services if s[2] == MESHTASTIC_SERVICE), None)
    if not mesh:
        print("no Meshtastic service", file=sys.stderr)
        return 1
    chars = central.characteristics(mesh[0], mesh[1])
    for uuid, (handle, props) in chars.items():
        print(f"  characteristic 0x{handle:04x} props 0x{props:02x} {uuid}")
    if passkey is not None:
        try:
            central.encrypt(central.pair(passkey))
        except PairingError as error:
            print(f"  {error}")
            return 1
        print(f"  paired with passkey {passkey:06d}; link encrypted")
    from tools import mesh_api
    nonce = 42
    seen: dict = {}
    count = 0
    try:
        central.write(chars[TORADIO][0], mesh_api.want_config(nonce))
        for _ in range(200):
            value = central.read(chars[FROMRADIO][0])
            if not value:
                break
            count += 1
            seen.update({k: v for k, v in mesh_api.describe(value).items()
                         if not k.startswith("raw_")})
    except AttError as error:
        print(f"  {error}")
        return 1
    print(f"  FromRadio: {count} message(s); node "
          + (f"!{seen['my_node_num']:08x}" if "my_node_num" in seen else "unknown")
          + f"; config complete {seen.get('config_complete_id') == nonce}")
    return 0 if seen.get("config_complete_id") == nonce else 1


# -- a list of operations, for the page's API (ADR 0188) ----------------------

OPS = ("services", "characteristics", "read", "write", "pair")


class OpError(ValueError):
    """An operation the central cannot do as asked."""


def check_ops(ops) -> list[dict]:
    """The operations a request asks for, refused before any connection if
    one is not something the central does."""
    if not isinstance(ops, list) or not ops:
        raise OpError("ops is a list of operations")
    for n, op in enumerate(ops, 1):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise OpError(f"operation {n}: op is one of {', '.join(OPS)}")
        kind = op["op"]
        if kind in ("characteristics", "read", "write") and not isinstance(op.get("uuid"), str):
            raise OpError(f"operation {n}: {kind} needs a uuid")
        if kind == "write":
            try:
                bytes.fromhex(str(op.get("hex", "")))
            except ValueError:
                raise OpError(f"operation {n}: write needs hex, the bytes to write") from None
        if kind == "pair" and not (isinstance(op.get("passkey"), int)
                                   and 0 <= op["passkey"] <= 999999):
            raise OpError(f"operation {n}: pair needs a passkey, 0 to 999999")
    return ops


def run_ops(central, ops: list[dict]) -> list[dict]:
    """Each operation's result, in order, on a connected central. A
    characteristic is found by its UUID among every service's."""
    results: list[dict] = []
    services: list[tuple[int, int, str]] | None = None
    chars: dict[str, tuple[int, int]] = {}

    def known_services():
        nonlocal services
        if services is None:
            services = central.services()
        return services

    def handle_of(uuid: str) -> int:
        uuid = uuid.lower()
        if not chars:
            for first, last, _ in known_services():
                chars.update({u.lower(): v for u, v in central.characteristics(first, last).items()})
        if uuid not in chars:
            raise OpError(f"no characteristic {uuid}")
        return chars[uuid][0]

    for op in ops:
        kind = op["op"]
        if kind == "services":
            results.append({"op": kind, "services": [
                {"uuid": uuid, "first": first, "last": last}
                for first, last, uuid in known_services()]})
        elif kind == "characteristics":
            wanted = op["uuid"].lower()
            found = [s for s in known_services() if s[2].lower() == wanted]
            if not found:
                raise OpError(f"no service {wanted}")
            got = central.characteristics(found[0][0], found[0][1])
            results.append({"op": kind, "characteristics": [
                {"uuid": uuid, "handle": handle, "properties": props}
                for uuid, (handle, props) in got.items()]})
        elif kind == "read":
            results.append({"op": kind, "uuid": op["uuid"],
                            "hex": central.read(handle_of(op["uuid"])).hex()})
        elif kind == "write":
            central.write(handle_of(op["uuid"]), bytes.fromhex(op["hex"]))
            results.append({"op": kind, "uuid": op["uuid"]})
        elif kind == "pair":
            central.encrypt(central.pair(op["passkey"]))
            results.append({"op": kind, "encrypted": True})
    return results


def ops_session(air: str, ops: list[dict], attempts: int = 3, wait_s: float = 240) -> dict:
    """Connect to the board on `air`, do `ops`, and say what came of them."""
    central = Central(air)
    try:
        for attempt in range(attempts):
            if not central.connect(wait_s):
                return {"ok": False, "error": "nothing advertised on the air"}
            try:
                central.exchange_mtu()
                return {"ok": True, "results": run_ops(central, ops)}
            except (LinkLost, TimeoutError) as error:
                last = f"the link was lost: {error}"
            except (OpError, AttError, PairingError) as error:
                return {"ok": False, "error": str(error)}
        return {"ok": False, "error": last}
    finally:
        central.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("air", help="the run's Bluetooth air directory (--ble-air)")
    ap.add_argument("command", choices=("services", "meshtastic", "ops"))
    ap.add_argument("--ops", help="with ops: the operations, as JSON (ADR 0188)")
    ap.add_argument("--passkey", type=int,
                    help="pair with this passkey first (the node's PIN), and encrypt")
    ap.add_argument("--attempts", type=int, default=3,
                    help="connections to try if the link is lost (default 3)")
    args = ap.parse_args(argv)
    if args.command == "ops":
        import json
        try:
            ops = check_ops(json.loads(args.ops or "null"))
        except (OpError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}))
            return 2
        answer = ops_session(args.air, ops, args.attempts)
        print(json.dumps(answer))
        return 0 if answer["ok"] else 1
    central = Central(args.air)
    try:
        for attempt in range(args.attempts):
            if not central.connect():
                print("heard nothing advertise", file=sys.stderr)
                return 1
            try:
                return session(central, args.command, args.passkey)
            except (LinkLost, TimeoutError) as error:
                print(f"connection {attempt + 1} ended: {error}; connecting again",
                      flush=True)
        return 1
    finally:
        central.close()


if __name__ == "__main__":
    raise SystemExit(main())
