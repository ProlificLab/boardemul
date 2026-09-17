#!/usr/bin/env python3
"""DHCP: a lease pool keyed by hardware address, and the BOOTP reply.

The peer used to hand one fixed address to whoever asked (ADR 0084) -- a table
of one, with the station's address written into the source. That is enough for a
single station and wrong for a second, which would be given the same address.
This is a real pool instead (ADR 0091): a range of host numbers, allocated to a
hardware address and remembered, so a station that asks again gets the same lease
and a different station gets a different one.

It is deliberately small: no declines, no explicit release, no lease expiry
within a run (a run is short, and an expiry timer would be state the tests would
have to wind). What it does do faithfully is the part that was a shortcut --
distinct, stable addresses per client -- and the BOOTP framing the client reads.
"""

from __future__ import annotations

import struct

# BOOTP/DHCP message types (option 53).
DISCOVER = 1
OFFER = 2
REQUEST = 3
ACK = 5
NAK = 6

_MAGIC = bytes.fromhex("63825363")      # the option-field cookie


class Pool:
    """A range of addresses on one /24, leased by hardware address.

    `prefix` is the three network octets; `first` and `last` are the host
    numbers the pool may hand out. A lease is stable: the same hardware address
    always gets the address it first got, so a renewing client keeps its lease.
    """

    def __init__(self, prefix: bytes, first: int, last: int):
        assert len(prefix) == 3 and 1 <= first <= last <= 254
        self.prefix = prefix
        self.first = first
        self.last = last
        self.leases: dict[bytes, bytes] = {}

    def allocate(self, mac: bytes) -> bytes | None:
        """The address for a hardware address: its existing lease, or the lowest
        free host number, or None if the pool is used up."""
        if mac in self.leases:
            return self.leases[mac]
        taken = {ip[3] for ip in self.leases.values()}
        for host in range(self.first, self.last + 1):
            if host not in taken:
                ip = self.prefix + bytes([host])
                self.leases[mac] = ip
                return ip
        return None

    def ip_for(self, mac: bytes) -> bytes | None:
        return self.leases.get(mac)


def request(dhcp: bytes) -> tuple[bytes, bytes, int] | None:
    """Read a BOOTP request: return (xid, chaddr, message type), or None if it is
    not one this understands. The message type is option 53; the client hardware
    address is the first six octets of the sixteen-byte chaddr field."""
    cookie = dhcp.find(_MAGIC)
    if cookie < 0 or len(dhcp) < 34:
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
        if i + 1 >= len(opts):
            break
        length = opts[i + 1]
        if code == 53 and i + 2 < len(opts):
            message_type = opts[i + 2]
        i += 2 + length
    if message_type is None:
        return None
    return xid, chaddr, message_type


def reply(xid: bytes, chaddr: bytes, message_type: int, yiaddr: bytes,
          server_ip: bytes, netmask: bytes, dns_ip: bytes,
          lease_seconds: int) -> bytes:
    """A BOOTP reply: an OFFER (type 2) or an ACK (type 5).

    The client's transaction id and hardware address are echoed, the offered
    address goes in yiaddr, and the options carry the mask, the router, the DNS
    and the lease -- what a DISCOVER's parameter-request list asks for.
    """
    bootp = bytes([2, 1, 6, 0])                          # op reply, ethernet, hlen 6
    bootp += xid + struct.pack(">HH", 0, 0)              # xid, secs, flags
    bootp += bytes(4) + yiaddr + server_ip + bytes(4)    # ciaddr, yiaddr, siaddr, giaddr
    bootp += chaddr + bytes(16 - len(chaddr))            # chaddr, padded to 16
    bootp += bytes(64) + bytes(128)                      # sname, file
    bootp += _MAGIC
    opts = bytes([53, 1, message_type])
    opts += bytes([54, 4]) + server_ip                   # server identifier
    opts += bytes([51, 4]) + struct.pack(">I", lease_seconds)
    opts += bytes([1, 4]) + netmask                      # subnet mask
    opts += bytes([3, 4]) + server_ip                    # router
    opts += bytes([6, 4]) + dns_ip                       # DNS
    opts += bytes([255])                                 # end
    return bootp + opts
