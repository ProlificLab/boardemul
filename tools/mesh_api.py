#!/usr/bin/env python3
"""Speak Meshtastic's client API to a node over a serial port's TCP bridge.

A Meshtastic node's API on a serial port is the stream protocol: each message
is 0x94 0xC3, a big-endian sixteen-bit length, and a protobuf (ToRadio to the
node, FromRadio from it); anything between frames is the node's text log.
The field numbers here are those of meshtastic/protobufs (mesh.proto,
admin.proto, config.proto, portnums.proto); only what this needs is encoded.

With a device's OTG port on TCP (`--serial-tcp 5555:otg`, ADR 0166):

    python tools/mesh_api.py 127.0.0.1:5555 info
    python tools/mesh_api.py 127.0.0.1:5555 wifi SSID PSK

`info` asks for the node's configuration and prints its node number, network
settings and display mode; `wifi` sets the network the node joins as the
apps' WiFi page does, and `display-mode` the display mode. Each changes only
its own fields of the section the node sent, then the node saves it and
restarts. Under display mode `color` (the T-Deck's colour interface)
Meshtastic 2.7 offers no TCP API on WiFi; `display-mode default` is the
classic screen, under which it does.
"""

from __future__ import annotations

import argparse
import random
import socket
import struct
import sys
import time

START = b"\x94\xc3"
WAKE = b"\xc3" * 32         # what the Python client sends first, to wake a sleeping port

# Field numbers.
TORADIO_PACKET, TORADIO_WANT_CONFIG = 1, 3
FROMRADIO_ID, FROMRADIO_PACKET, FROMRADIO_MY_INFO = 1, 2, 3
FROMRADIO_NODE_INFO, FROMRADIO_CONFIG, FROMRADIO_LOG = 4, 5, 6
FROMRADIO_CONFIG_COMPLETE, FROMRADIO_REBOOTED = 7, 8
MYINFO_NODE_NUM = 1
PACKET_FROM, PACKET_TO, PACKET_DECODED, PACKET_ID = 1, 2, 4, 6
DATA_PORTNUM, DATA_PAYLOAD, DATA_WANT_RESPONSE = 1, 2, 3
ADMIN_APP = 6
ADMIN_SET_CONFIG = 34
CONFIG_NETWORK, CONFIG_DISPLAY, CONFIG_BLUETOOTH = 4, 5, 7
BLUETOOTH_MODE, BLUETOOTH_FIXED_PIN = 2, 3
# BluetoothConfig.PairingMode. Under anything but NO_PIN Meshtastic reads and
# writes its characteristics only on an encrypted, authenticated link
# (src/nimble/NimbleBluetooth.cpp; ADR 0157).
PAIRING_MODES = {"random-pin": 0, "fixed-pin": 1, "no-pin": 2}
NETWORK_WIFI_ENABLED, NETWORK_SSID, NETWORK_PSK = 1, 3, 4
DISPLAY_MODE = 8
# DisplayConfig.DisplayMode. COLOR selects the colour interface (the "MUI"),
# under which Meshtastic 2.7 starts neither its web server nor its TCP API
# on WiFi (src/mesh/wifi/WiFiAPClient.cpp, onNetworkConnected).
DISPLAY_MODES = {"default": 0, "twocolor": 1, "inverted": 2, "color": 3}


# -- protobuf, as far as needed ---------------------------------------------

def varint(n: int) -> bytes:
    out = bytearray()
    n &= (1 << 64) - 1
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field_bytes(number: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return varint(number << 3 | 2) + varint(len(value)) + value


def field_fixed32(number: int, value: int) -> bytes:
    return varint(number << 3 | 5) + struct.pack("<I", value & 0xFFFFFFFF)


def fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """(number, wire type, value) for each field; values of length-delimited
    fields are bytes, to be read again as a message if they are one."""
    out, pos = [], 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, pos = _read_varint(data, pos)
        elif kind == 1:
            value, pos = struct.unpack_from("<Q", data, pos)[0], pos + 8
        elif kind == 2:
            length, pos = _read_varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif kind == 5:
            value, pos = struct.unpack_from("<I", data, pos)[0], pos + 4
        else:
            raise ValueError(f"wire type {kind} at {pos}")
        out.append((number, kind, value))
    return out


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def first(data: bytes, number: int, default=None):
    return next((v for n, _k, v in fields(data) if n == number), default)


# -- the messages ------------------------------------------------------------

def frame(message: bytes) -> bytes:
    return START + struct.pack(">H", len(message)) + message


def want_config(nonce: int) -> bytes:
    return field_varint(TORADIO_WANT_CONFIG, nonce)


def patch(message: bytes, replacements: dict[int, bytes]) -> bytes:
    """`message` with every occurrence of the given fields dropped and their
    new encodings appended; every other field kept as it was, unknown ones
    included. Meshtastic replaces a whole config section on set_config, so a
    change is made to the section the node sent, never to an empty one."""
    out, pos = bytearray(), 0
    while pos < len(message):
        start = pos
        key, pos = _read_varint(message, pos)
        number, kind = key >> 3, key & 7
        if kind == 0:
            _value, pos = _read_varint(message, pos)
        elif kind == 1:
            pos += 8
        elif kind == 2:
            length, pos = _read_varint(message, pos)
            pos += length
        elif kind == 5:
            pos += 4
        else:
            raise ValueError(f"wire type {kind} at {start}")
        if number not in replacements:
            out += message[start:pos]
    for encoded in replacements.values():
        out += encoded
    return bytes(out)


def set_config(node: int, section: int, body: bytes) -> bytes:
    """A ToRadio carrying an AdminMessage set_config for one section, to the
    node itself."""
    admin = field_bytes(ADMIN_SET_CONFIG, field_bytes(section, body))
    data = (field_varint(DATA_PORTNUM, ADMIN_APP) + field_bytes(DATA_PAYLOAD, admin)
            + field_varint(DATA_WANT_RESPONSE, 1))
    packet = (field_fixed32(PACKET_TO, node) + field_bytes(PACKET_DECODED, data)
              + field_fixed32(PACKET_ID, random.getrandbits(32)))
    return field_bytes(TORADIO_PACKET, packet)


def set_wifi(node: int, ssid: str, psk: str, network: bytes = b"",
             enabled: bool = True) -> bytes:
    """Join `ssid`, keeping the rest of the node's network section."""
    body = patch(network, {NETWORK_WIFI_ENABLED: field_varint(NETWORK_WIFI_ENABLED, int(enabled)),
                           NETWORK_SSID: field_bytes(NETWORK_SSID, ssid),
                           NETWORK_PSK: field_bytes(NETWORK_PSK, psk)})
    return set_config(node, CONFIG_NETWORK, body)


def set_pairing_mode(node: int, mode: int, bluetooth: bytes = b"",
                     pin: int | None = None) -> bytes:
    """Change the Bluetooth pairing mode, and the fixed PIN if one is given,
    keeping the rest of the section. A central pairs with the PIN as its
    passkey (ADR 0171)."""
    changes = {BLUETOOTH_MODE: field_varint(BLUETOOTH_MODE, mode)}
    if pin is not None:
        if not 0 <= pin <= 999999:
            raise ValueError("a Bluetooth PIN is six digits")
        changes[BLUETOOTH_FIXED_PIN] = field_varint(BLUETOOTH_FIXED_PIN, pin)
    return set_config(node, CONFIG_BLUETOOTH, patch(bluetooth, changes))


def set_display_mode(node: int, mode: int, display: bytes = b"") -> bytes:
    """Change the display mode, keeping the rest of the display section."""
    return set_config(node, CONFIG_DISPLAY,
                      patch(display, {DISPLAY_MODE: field_varint(DISPLAY_MODE, mode)}))


class Reader:
    """Splits a node's serial stream into frames and its text log."""

    def __init__(self):
        self.buffer = bytearray()
        self.text = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer += data
        frames = []
        while True:
            at = self.buffer.find(START)
            if at < 0:
                keep = 1 if self.buffer.endswith(START[:1]) else 0
                self.text += self.buffer[:len(self.buffer) - keep]
                del self.buffer[:len(self.buffer) - keep]
                return frames
            self.text += self.buffer[:at]
            del self.buffer[:at]
            if len(self.buffer) < 4:
                return frames
            length = struct.unpack_from(">H", self.buffer, 2)[0]
            if length > 512:
                # Not a frame after all: the two octets were log.
                self.text += self.buffer[:1]
                del self.buffer[:1]
                continue
            if len(self.buffer) < 4 + length:
                return frames
            frames.append(bytes(self.buffer[4:4 + length]))
            del self.buffer[:4 + length]


def describe(message: bytes) -> dict:
    """What a FromRadio says, as far as this tool cares."""
    out: dict = {}
    for number, _kind, value in fields(message):
        if number == FROMRADIO_MY_INFO:
            out["my_node_num"] = first(value, MYINFO_NODE_NUM)
        elif number == FROMRADIO_CONFIG:
            display = first(value, CONFIG_DISPLAY)
            if display is not None:
                out["raw_display"] = display
                mode = first(display, DISPLAY_MODE, 0)
                out["display_mode"] = next(
                    (name for name, n in DISPLAY_MODES.items() if n == mode), str(mode))
            bluetooth = first(value, CONFIG_BLUETOOTH)
            if bluetooth is not None:
                out["raw_bluetooth"] = bluetooth
                mode = first(bluetooth, BLUETOOTH_MODE, 0)
                out["pairing_mode"] = next(
                    (name for name, n in PAIRING_MODES.items() if n == mode), str(mode))
                out["fixed_pin"] = first(bluetooth, BLUETOOTH_FIXED_PIN, 0)
            network = first(value, CONFIG_NETWORK)
            if network is not None:
                out["raw_network"] = network
                out["network"] = {
                    "wifi_enabled": bool(first(network, NETWORK_WIFI_ENABLED, 0)),
                    "wifi_ssid": first(network, NETWORK_SSID, b"").decode(errors="replace"),
                    "wifi_psk_set": bool(first(network, NETWORK_PSK, b"")),
                }
        elif number == FROMRADIO_CONFIG_COMPLETE:
            out["config_complete_id"] = value
        elif number == FROMRADIO_REBOOTED:
            out["rebooted"] = bool(value)
        elif number == FROMRADIO_PACKET:
            decoded = first(value, PACKET_DECODED)
            if decoded is not None:
                out.setdefault("packets", []).append(first(decoded, DATA_PORTNUM, 0))
    return out


# -- talking to a node -------------------------------------------------------

class Node:
    def __init__(self, address: str, timeout: float = 60):
        host, port = address.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=10)
        self.sock.settimeout(0.5)
        self.reader = Reader()
        self.timeout = timeout

    def send(self, message: bytes) -> None:
        self.sock.sendall(frame(message))

    def receive(self, until, seconds: float | None = None) -> dict:
        """Read until `until(state)` holds; state gathers what was described."""
        state: dict = {}
        deadline = time.monotonic() + (seconds or self.timeout)
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            for message in self.reader.feed(data):
                for key, value in describe(message).items():
                    if key == "packets":
                        state.setdefault(key, []).extend(value)
                    else:
                        state[key] = value
            if until(state):
                break
        return state

    def configuration(self) -> dict:
        nonce = random.randint(1, 0x7FFFFFFF)
        self.sock.sendall(WAKE)
        time.sleep(0.2)
        self.send(want_config(nonce))
        return self.receive(lambda s: s.get("config_complete_id") == nonce)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address", help="HOST:PORT of the serial port's TCP bridge")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="print the node number, network settings and "
                                "display mode")
    wifi = sub.add_parser("wifi", help="set the WiFi network the node joins")
    wifi.add_argument("ssid")
    wifi.add_argument("psk")
    display = sub.add_parser("display-mode", help="set the display mode; 'color' is "
                             "the colour interface, which offers no TCP API on WiFi")
    display.add_argument("mode", choices=sorted(DISPLAY_MODES))
    pairing = sub.add_parser("bluetooth-pairing", help="set how Bluetooth pairs; "
                             "'no-pin' lets a central read and write unpaired")
    pairing.add_argument("mode", choices=sorted(PAIRING_MODES))
    pairing.add_argument("--pin", type=int,
                         help="the fixed PIN, six digits; a central pairs with it")
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args(argv)

    node = Node(args.address, timeout=args.timeout)
    state = node.configuration()
    if "my_node_num" not in state:
        print("the node did not answer with its configuration", file=sys.stderr)
        return 1
    print(f"node !{state['my_node_num']:08x}; network {state.get('network')}; "
          f"display mode {state.get('display_mode')}; "
          f"Bluetooth pairing {state.get('pairing_mode')}")
    change = None
    if args.command == "wifi":
        change = set_wifi(state["my_node_num"], args.ssid, args.psk,
                          state.get("raw_network", b""))
    elif args.command == "display-mode":
        change = set_display_mode(state["my_node_num"], DISPLAY_MODES[args.mode],
                                  state.get("raw_display", b""))
    elif args.command == "bluetooth-pairing":
        change = set_pairing_mode(state["my_node_num"], PAIRING_MODES[args.mode],
                                  state.get("raw_bluetooth", b""), pin=args.pin)
    if change is not None:
        node.send(change)
        state = node.receive(lambda s: "rebooted" in s, seconds=args.timeout)
        print("sent; the node " + ("restarted" if state.get("rebooted")
                                   else "has not said it restarted"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
