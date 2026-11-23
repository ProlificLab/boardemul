#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A small authoritative DNS resolver: answer for a zone, refuse outside it.

The peer used to answer every A query with the gateway's address (ADR 0085/0086),
which connects a firmware that trusts whatever it gets but is not a resolver --
ask it for a name that cannot exist and it says the gateway too. This answers
from a *zone* instead: a table of the names the LAN actually hosts, each with its
address. A name in the zone gets its record; a name outside it gets `NXDOMAIN`,
which is what a real resolver says and what a firmware ought to be told.

It is deliberately just enough of DNS for that: single-question queries, A
records, the IN class. A DNS message and the firmware's unicast-shaped mDNS query
have the same body, so both go through here (ADR 0086). Everything it does not do
-- other record types, multiple questions, compression in the question, EDNS --
is left alone rather than guessed at, and a query it does not understand is not
answered at all.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

TYPE_A = 1
CLASS_IN = 1

# Response flags. QR set, recursion available, and the response code in the low
# four bits: 0 for an answer, 3 (NXDOMAIN) for a name the zone does not hold.
_FLAGS_OK = 0x8180
_FLAGS_SERVFAIL = 0x8182
_FLAGS_NXDOMAIN = 0x8183


@dataclass
class Query:
    """A parsed single-question query, and the bytes to echo back in the reply.

    `question` is the QNAME, its terminating null, the QTYPE and the QCLASS,
    verbatim, so a reply repeats exactly what was asked.
    """
    id: bytes
    name: str
    qtype: int
    qclass: int
    question: bytes


def parse_query(msg: bytes) -> Query | None:
    """Read a single-question query, or return None if it is not one.

    The name is lower-cased, because DNS names are case-insensitive and a zone is
    easiest to hold in one case. A compression pointer in the question is
    refused: it is not something a resolver sends in a query, and following one
    here would be reading beyond the question this is meant to parse.
    """
    if len(msg) < 12:
        return None
    if int.from_bytes(msg[4:6], "big") != 1:    # exactly one question
        return None
    labels: list[bytes] = []
    i = 12
    while i < len(msg) and msg[i]:
        length = msg[i]
        if length & 0xC0:                       # a compression pointer, not a label
            return None
        if i + 1 + length > len(msg):
            return None
        labels.append(msg[i + 1:i + 1 + length])
        i += 1 + length
    if i >= len(msg) or i + 5 > len(msg):       # the null, then QTYPE and QCLASS
        return None
    qtype = int.from_bytes(msg[i + 1:i + 3], "big")
    qclass = int.from_bytes(msg[i + 3:i + 5], "big")
    name = ".".join(b.decode("ascii", "replace") for b in labels).lower()
    return Query(id=msg[0:2], name=name, qtype=qtype, qclass=qclass,
                 question=msg[12:i + 5])


def a_answer(query: Query, ip: bytes, ttl: int = 60) -> bytes:
    """A response carrying one A record for the queried name.

    The answer names the question by a compression pointer to it (0xC00C, the
    offset of the QNAME right after the twelve-byte header), which is how every
    resolver refers back to the question it is answering.
    """
    head = query.id + struct.pack(">HHHHH", _FLAGS_OK, 1, 1, 0, 0)
    answer = (b"\xC0\x0C" + struct.pack(">HHI", TYPE_A, CLASS_IN, ttl)
              + struct.pack(">H", len(ip)) + ip)
    return head + query.question + answer


def answer(query: Query, ips: list[bytes], ttl: int = 60) -> bytes:
    """A response carrying one A record per address, in the order given -- what
    a name forwarded to the host's resolver comes back with (ADR 0097)."""
    head = query.id + struct.pack(">HHHHH", _FLAGS_OK, 1, len(ips), 0, 0)
    records = b"".join(b"\xC0\x0C" + struct.pack(">HHI", TYPE_A, CLASS_IN, ttl)
                       + struct.pack(">H", len(ip)) + ip for ip in ips)
    return head + query.question + records


def nxdomain(query: Query) -> bytes:
    """A response that says the name does not exist."""
    head = query.id + struct.pack(">HHHHH", _FLAGS_NXDOMAIN, 1, 0, 0, 0)
    return head + query.question


def servfail(query: Query) -> bytes:
    """A response that says the lookup itself failed, which is not the same as
    the name not existing: a client may try again."""
    head = query.id + struct.pack(">HHHHH", _FLAGS_SERVFAIL, 1, 0, 0, 0)
    return head + query.question


def respond(msg: bytes, zone: dict[str, bytes], ttl: int = 60) -> bytes | None:
    """Answer a query against a zone: the record if the name is held, NXDOMAIN if
    it is an A query for a name that is not, and nothing for a query this does not
    serve (a type other than A, or something that does not parse as a query)."""
    query = parse_query(msg)
    if query is None:
        return None
    if query.qtype != TYPE_A or query.qclass != CLASS_IN:
        return None
    ip = zone.get(query.name)
    return a_answer(query, ip, ttl) if ip is not None else nxdomain(query)
