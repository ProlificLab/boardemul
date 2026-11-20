#!/usr/bin/env python3
"""tools/mesh_api.py: Meshtastic's stream framing and the few messages it
encodes, checked against the field numbers of meshtastic/protobufs."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import mesh_api as m  # noqa: E402


def test_varints_encode_as_protobuf_does() -> None:
    assert m.varint(0) == b"\x00"
    assert m.varint(1) == b"\x01"
    assert m.varint(300) == b"\xac\x02"
    assert m._read_varint(b"\xac\x02", 0) == (300, 2)


def test_a_want_config_is_field_three() -> None:
    assert m.want_config(42) == b"\x18\x2a"
    assert m.frame(m.want_config(42)) == b"\x94\xc3\x00\x02\x18\x2a"


def test_setting_wifi_nests_admin_in_a_packet_to_the_node() -> None:
    message = m.set_wifi(0x1234ABCD, "lab", "secret123")
    packet = m.first(message, m.TORADIO_PACKET)
    assert m.first(packet, m.PACKET_TO) == 0x1234ABCD
    data = m.first(packet, m.PACKET_DECODED)
    assert m.first(data, m.DATA_PORTNUM) == m.ADMIN_APP == 6
    assert m.first(data, m.DATA_WANT_RESPONSE) == 1
    admin = m.first(data, m.DATA_PAYLOAD)
    network = m.first(m.first(admin, m.ADMIN_SET_CONFIG), m.CONFIG_NETWORK)
    assert m.fields(network) == [(1, 0, 1), (3, 2, b"lab"), (4, 2, b"secret123")]


def test_the_reader_separates_frames_from_the_log() -> None:
    reader = m.Reader()
    stream = (b"INFO | booted\r\n" + m.frame(b"\x40\x01")
              + b"DEBUG | more\r\n" + m.frame(m.field_varint(7, 99)))
    frames = []
    for i in range(0, len(stream), 3):            # in pieces, as a socket gives it
        frames += reader.feed(stream[i:i + 3])
    assert frames == [b"\x40\x01", b"\x38\x63"]
    assert bytes(reader.text) == b"INFO | booted\r\nDEBUG | more\r\n"
    assert m.describe(frames[0]) == {"rebooted": True}
    assert m.describe(frames[1]) == {"config_complete_id": 99}


def test_my_info_and_network_config_are_described() -> None:
    my_info = m.field_bytes(m.FROMRADIO_MY_INFO, m.field_varint(m.MYINFO_NODE_NUM, 0xA1B2C3D4))
    assert m.describe(my_info) == {"my_node_num": 0xA1B2C3D4}
    network = m.field_varint(1, 1) + m.field_bytes(3, "lab") + m.field_bytes(4, "pw")
    config = m.field_bytes(m.FROMRADIO_CONFIG, m.field_bytes(m.CONFIG_NETWORK, network))
    described = m.describe(config)
    assert described["network"] == {"wifi_enabled": True, "wifi_ssid": "lab",
                                     "wifi_psk_set": True}
    assert described["raw_network"] == network
    display = m.field_varint(m.DISPLAY_MODE, 3)
    config = m.field_bytes(m.FROMRADIO_CONFIG, m.field_bytes(m.CONFIG_DISPLAY, display))
    assert m.describe(config)["display_mode"] == "color"


def test_a_change_keeps_the_rest_of_the_section() -> None:
    """Meshtastic replaces a whole section on set_config: the NTP server, and
    a field this tool does not know (99), must survive a WiFi change."""
    network = (m.field_varint(1, 0) + m.field_bytes(5, "pool.ntp.org")
               + m.field_bytes(99, b"future") + m.field_bytes(3, "old"))
    body = m.first(m.first(m.first(m.first(
        m.set_wifi(1, "lab", "pw", network), m.TORADIO_PACKET),
        m.PACKET_DECODED), m.DATA_PAYLOAD), m.ADMIN_SET_CONFIG)
    sent = m.first(body, m.CONFIG_NETWORK)
    assert m.first(sent, 5) == b"pool.ntp.org"
    assert m.first(sent, 99) == b"future"
    assert [v for n, _k, v in m.fields(sent) if n == 3] == [b"lab"]
    assert m.first(sent, 1) == 1 and m.first(sent, 4) == b"pw"


def test_the_display_mode_is_set_within_its_section() -> None:
    display = m.field_varint(1, 30) + m.field_varint(m.DISPLAY_MODE, 3)
    body = m.first(m.first(m.first(m.first(
        m.set_display_mode(1, m.DISPLAY_MODES["default"], display), m.TORADIO_PACKET),
        m.PACKET_DECODED), m.DATA_PAYLOAD), m.ADMIN_SET_CONFIG)
    sent = m.first(body, m.CONFIG_DISPLAY)
    assert m.first(sent, 1) == 30
    assert m.first(sent, m.DISPLAY_MODE) == 0


def test_a_lone_start_octet_in_the_log_is_not_lost() -> None:
    reader = m.Reader()
    assert reader.feed(b"abc\x94") == []
    assert reader.feed(b"x") == []
    assert bytes(reader.text) == b"abc\x94x"
    assert struct.pack(">H", 2) == b"\x00\x02"


def test_the_fixed_pin_is_set_with_the_pairing_mode() -> None:
    bluetooth = m.field_varint(1, 1) + m.field_varint(m.BLUETOOTH_MODE, 0)
    body = m.first(m.first(m.first(m.first(
        m.set_pairing_mode(1, m.PAIRING_MODES["fixed-pin"], bluetooth, pin=271828),
        m.TORADIO_PACKET), m.PACKET_DECODED), m.DATA_PAYLOAD), m.ADMIN_SET_CONFIG)
    sent = m.first(body, m.CONFIG_BLUETOOTH)
    assert m.first(sent, 1) == 1                       # enabled, kept
    assert m.first(sent, m.BLUETOOTH_MODE) == 1
    assert m.first(sent, m.BLUETOOTH_FIXED_PIN) == 271828
