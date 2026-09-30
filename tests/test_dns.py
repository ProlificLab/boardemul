#!/usr/bin/env python3
"""The authoritative resolver: it answers for its zone and refuses the rest.

The captive resolver it replaces (ADR 0085/0086) answered every name with the
gateway, so there was nothing to test -- every query had the same reply. This
one has two behaviours that matter and must be kept apart: a held name gets its
record, and a name that is not held gets NXDOMAIN, not the gateway. The queries
are built here from the bytes so the parser is exercised on real wire shapes,
including the firmware's `.local` mDNS query, which has a DNS query's body.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import dns  # noqa: E402

GATEWAY = bytes([192, 168, 4, 1])
ZONE = {"lilygo.local": GATEWAY, "pool.ntp.org": GATEWAY, "time.nist.gov": GATEWAY}


def _query(name: str, qtype: int = dns.TYPE_A, qclass: int = dns.CLASS_IN,
           txn: bytes = b"\x12\x34") -> bytes:
    """A single-question DNS query for a name, built label by label."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    header = txn + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)   # RD set, one question
    return header + qname + struct.pack(">HH", qtype, qclass)


def _rcode(reply: bytes) -> int:
    return int.from_bytes(reply[2:4], "big") & 0x0F


def _first_a(reply: bytes) -> bytes:
    """The four address octets of the first A record, found past the echoed
    question by the compression pointer that introduces the answer."""
    at = reply.index(b"\xc0\x0c")
    rdlength = int.from_bytes(reply[at + 10:at + 12], "big")
    return reply[at + 12:at + 12 + rdlength]


def test_a_held_name_gets_its_record() -> None:
    reply = dns.respond(_query("lilygo.local"), ZONE)
    assert reply is not None
    assert _rcode(reply) == 0, "a held name is NOERROR"
    assert int.from_bytes(reply[6:8], "big") == 1, "one answer"
    assert _first_a(reply) == GATEWAY
    assert reply[:2] == b"\x12\x34", "the transaction id is echoed"


def test_a_name_outside_the_zone_is_refused() -> None:
    reply = dns.respond(_query("example.com"), ZONE)
    assert reply is not None
    assert _rcode(reply) == 3, "an unheld name is NXDOMAIN, not the gateway"
    assert int.from_bytes(reply[6:8], "big") == 0, "and carries no answer"


def test_the_query_is_echoed_in_the_reply() -> None:
    """A resolver repeats the question; a stub that did not would confuse the
    client about which name was answered."""
    q = _query("pool.ntp.org")
    reply = dns.respond(q, ZONE)
    assert reply is not None and q[12:] in reply, "the question is not echoed back"


def test_names_are_matched_without_regard_to_case() -> None:
    assert dns.respond(_query("LilyGo.Local"), ZONE) is not None
    assert _rcode(dns.respond(_query("LilyGo.Local"), ZONE)) == 0


def test_only_a_records_in_the_in_class_are_served() -> None:
    assert dns.respond(_query("lilygo.local", qtype=28), ZONE) is None, "AAAA"
    assert dns.respond(_query("lilygo.local", qclass=3), ZONE) is None, "CHAOS"


def test_a_malformed_query_is_not_answered() -> None:
    assert dns.respond(b"", ZONE) is None
    assert dns.respond(b"\x12\x34", ZONE) is None, "a header with no question"
    # Two questions is not something this serves; it must not half-answer.
    two = b"\x12\x34" + struct.pack(">HHHHH", 0x0100, 2, 0, 0, 0)
    assert dns.respond(two + b"\x00\x00\x01\x00\x01", ZONE) is None


def test_a_forwarded_answer_carries_every_address() -> None:
    """What the host's resolver returned, one A record each, in order (ADR 0097)."""
    q = dns.parse_query(_query("pool.ntp.org"))
    ips = [bytes([1, 2, 3, 4]), bytes([5, 6, 7, 8])]
    reply = dns.answer(q, ips)
    assert _rcode(reply) == 0 and int.from_bytes(reply[6:8], "big") == 2
    body = reply[12 + len(q.question):]
    assert body[12:16] == ips[0] and body[28:32] == ips[1]


def test_a_failed_lookup_is_servfail_not_nxdomain() -> None:
    q = dns.parse_query(_query("pool.ntp.org"))
    assert _rcode(dns.servfail(q)) == 2


def test_the_mdns_query_shape_parses() -> None:
    """The firmware's `.local` lookup is a unicast DNS query in shape (ADR 0086),
    so the same resolver answers it."""
    q = dns.parse_query(_query("lilygo.local"))
    assert q is not None and q.name == "lilygo.local" and q.qtype == dns.TYPE_A
