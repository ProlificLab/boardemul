#!/usr/bin/env python3
"""The DHCP pool and BOOTP framing.

The pool is where "a table of one" (ADR 0084) becomes a real allocator, so the
properties to check are the ones a second station would have exposed: two
hardware addresses get two different leases, the same one gets its lease back,
and the pool refuses when it is used up. The request parser and reply builder are
checked against the bytes a client actually sends and reads.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import dhcp  # noqa: E402

PREFIX = bytes([192, 168, 4])
SERVER = bytes([192, 168, 4, 1])
MASK = bytes([255, 255, 255, 0])


def _discover(mac: bytes, xid: bytes = b"\xde\xad\xbe\xef",
              message_type: int = dhcp.DISCOVER) -> bytes:
    """A minimal BOOTP request carrying a message-type option."""
    bootp = bytes([1, 1, 6, 0]) + xid + struct.pack(">HH", 0, 0)
    bootp += bytes(4) * 4                                 # ciaddr..giaddr
    bootp += mac + bytes(16 - len(mac)) + bytes(64) + bytes(128)
    bootp += bytes.fromhex("63825363")
    bootp += bytes([53, 1, message_type]) + bytes([255])
    return bootp


def test_two_stations_get_two_addresses() -> None:
    pool = dhcp.Pool(PREFIX, first=2, last=254)
    a = pool.allocate(bytes.fromhex("020000000010"))
    b = pool.allocate(bytes.fromhex("020000000011"))
    assert a == bytes([192, 168, 4, 2]), "the first lease is .2, as before"
    assert b == bytes([192, 168, 4, 3]), "the second is a different address"
    assert a != b


def test_a_station_keeps_its_lease() -> None:
    pool = dhcp.Pool(PREFIX, first=2, last=254)
    mac = bytes.fromhex("020000000010")
    first = pool.allocate(mac)
    # Another station comes and goes; the first asks again.
    pool.allocate(bytes.fromhex("020000000011"))
    assert pool.allocate(mac) == first, "a renewing station keeps its address"
    assert pool.ip_for(mac) == first


def test_the_pool_can_be_used_up() -> None:
    pool = dhcp.Pool(PREFIX, first=2, last=3)          # room for two
    pool.allocate(bytes.fromhex("020000000010"))
    pool.allocate(bytes.fromhex("020000000011"))
    assert pool.allocate(bytes.fromhex("020000000012")) is None, "exhausted"


def test_the_request_type_is_read() -> None:
    mac = bytes.fromhex("0254444d0001")
    parsed = dhcp.request(_discover(mac, message_type=dhcp.REQUEST))
    assert parsed is not None
    xid, chaddr, mt = parsed
    assert chaddr == mac and mt == dhcp.REQUEST and xid == b"\xde\xad\xbe\xef"


def test_a_datagram_without_the_cookie_is_not_a_request() -> None:
    assert dhcp.request(b"\x00" * 40) is None


def test_the_reply_carries_the_lease_and_the_options() -> None:
    out = dhcp.reply(b"\xde\xad\xbe\xef", bytes.fromhex("0254444d0001"),
                     dhcp.OFFER, bytes([192, 168, 4, 2]), SERVER, MASK, SERVER,
                     7200)
    assert out[0] == 2, "a BOOTP reply"
    assert out[4:8] == b"\xde\xad\xbe\xef", "the xid is echoed"
    assert out[16:20] == bytes([192, 168, 4, 2]), "yiaddr is the offered address"
    assert bytes([53, 1, dhcp.OFFER]) in out, "the message-type option"
    assert bytes([51, 4]) + struct.pack(">I", 7200) in out, "the lease time"
    assert bytes([1, 4]) + MASK in out, "the subnet mask"
