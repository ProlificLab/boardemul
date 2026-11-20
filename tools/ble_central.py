#!/usr/bin/env python3
"""A Bluetooth Low Energy central on a run's Bluetooth air (ADRs 0045, 0168).

It connects to the board, keeps the link with a packet every connection
interval, answers what the peripheral's link layer asks (its features, its
data length), and speaks ATT over L2CAP: MTU exchange, primary services,
characteristics, reads and writes. Standard library only.

Frames on the air are a data channel PDU's first header octet, then its
payload (ADR 0168); the connection request is ADR 0047's. Nothing here is
retransmitted by the link layer, which assumes delivery, so an ATT request
that gets no answer is sent again.

    python3 tools/ble_central.py AIR_DIR services
    python3 tools/ble_central.py AIR_DIR meshtastic      # want_config, FromRadio

Run where the board's air directory is, which is the build container for a
run started there.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.air import AirMedium  # noqa: E402

PDU_CONNECT_IND = 0x05
LLID_CONTINUE, LLID_START, LLID_CONTROL = 1, 2, 3
LL_FEATURE_REQ, LL_FEATURE_RSP, LL_VERSION_IND = 0x08, 0x09, 0x0C
LL_PERIPHERAL_FEATURE_REQ, LL_LENGTH_REQ, LL_LENGTH_RSP = 0x0E, 0x14, 0x15
CID_ATT = 4
ATT_ERROR, ATT_MTU_REQ, ATT_MTU_RSP = 0x01, 0x02, 0x03
ATT_READ_BY_TYPE_REQ, ATT_READ_BY_TYPE_RSP = 0x08, 0x09
ATT_READ_REQ, ATT_READ_RSP = 0x0A, 0x0B
ATT_READ_BY_GROUP_REQ, ATT_READ_BY_GROUP_RSP = 0x10, 0x11
ATT_WRITE_REQ, ATT_WRITE_RSP = 0x12, 0x13
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


def connect_request() -> bytes:
    initiator = bytes.fromhex("ccbbaa286f24")
    advertiser = bytes.fromhex("0100000000c0")
    data = (struct.pack("<I", 0xAF9AB4C3) + bytes.fromhex("555555") + bytes([3])
            + struct.pack("<HHHH", 1, INTERVAL_UNITS, 0, 72)
            + bytes.fromhex("ffffffff1f") + bytes([0x0D]))
    return bytes([PDU_CONNECT_IND]) + initiator + advertiser + data


class LinkLost(Exception):
    """The peripheral advertises again: the connection is gone."""


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
        self.adverts_since = 0                # advertisements heard while connected

    def close(self) -> None:
        self.air.close()

    # -- the link layer --------------------------------------------------

    def connect(self, wait_s: float = 240) -> bool:
        """Wait to hear the board advertise, then ask to connect."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.air.poll():
                self.sn = self.nesn = 0
                self.outbox.clear()
                self.frames.clear()
                self.l2cap = bytearray()
                self.air.send(connect_request())
                self.connected_at = time.monotonic()
                self.adverts_since = 0
                return True
            time.sleep(0.5)
        return False

    def _send(self, llid: int, payload: bytes = b"") -> None:
        self.air.send(bytes([llid | self.nesn << 2 | self.sn << 3]) + payload)
        self.sn ^= 1

    def _listen(self) -> None:
        for frame in self.air.poll():
            llid = frame[0] & 3
            if not llid:                       # an advertising PDU, type 0
                # A peripheral advertising well after it was asked to connect
                # has dropped the link (or restarted its stack).
                if (self.connected_at is not None
                        and time.monotonic() > self.connected_at + 2):
                    self.adverts_since += 1
                continue
            self.nesn ^= 1
            body = frame[1:]
            if llid == LLID_CONTROL and body:
                self.control.append(body)
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
                    self.frames.append((cid, bytes(self.l2cap[4:4 + length])))
                    self.l2cap = bytearray()

    def step(self) -> None:
        """One connection interval: listen, then send what is due, or an
        empty packet so the peripheral knows its central is there."""
        self._listen()
        if self.adverts_since >= 3:
            raise LinkLost("the peripheral is advertising again")
        self._send(*(self.outbox.pop(0) if self.outbox else (LLID_CONTINUE,)))
        time.sleep(INTERVAL_S)

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
                 wait_s: float = 8) -> bytes:
        """Send an ATT request and return its response, sending it again if
        nothing came back; an error response raises AttError."""
        for _ in range(tries):
            self.outbox.append((LLID_START, struct.pack("<HH", len(pdu), CID_ATT) + pdu))
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

    def read(self, handle: int) -> bytes:
        return self.transact(bytes([ATT_READ_REQ]) + struct.pack("<H", handle),
                             (ATT_READ_RSP,))[1:]

    def write(self, handle: int, value: bytes) -> None:
        self.transact(bytes([ATT_WRITE_REQ]) + struct.pack("<H", handle) + value,
                      (ATT_WRITE_RSP,))


def session(central: Central, command: str) -> int:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("air", help="the run's Bluetooth air directory (--ble-air)")
    ap.add_argument("command", choices=("services", "meshtastic"))
    ap.add_argument("--attempts", type=int, default=3,
                    help="connections to try if the link is lost (default 3)")
    args = ap.parse_args(argv)
    central = Central(args.air)
    try:
        for attempt in range(args.attempts):
            if not central.connect():
                print("heard nothing advertise", file=sys.stderr)
                return 1
            try:
                return session(central, args.command)
            except (LinkLost, TimeoutError) as error:
                print(f"connection {attempt + 1} ended: {error}; connecting again",
                      flush=True)
        return 1
    finally:
        central.close()


if __name__ == "__main__":
    raise SystemExit(main())
