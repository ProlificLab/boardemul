#!/usr/bin/env python3
"""How long a LoRa packet occupies the channel, and what that implies.

Time on air is not a judgement call. It follows from the modulation the
firmware itself programs -- spreading factor, bandwidth, coding rate -- and the
length of the packet, by the arithmetic in Semtech's datasheet. Every number
here is read out of the radio's own registers, which is what separates this
from inventing propagation (ADR 0037).

    symbol time         2^SF / bandwidth
    preamble            (symbols + 4.25) * symbol time
    payload symbols     8 + max(0, ceil(...) * (coding rate + 4))
    time on air         preamble + payload symbols * symbol time

What follows from it is equally not a judgement call: a radio transmitting for
that long cannot hear anything, and two packets overlapping in the air arrive
on top of each other and neither is decoded.
"""

from __future__ import annotations

import math
import struct

# The bandwidth each code means, in hertz, as the part defines them.
BANDWIDTHS = {
    0x00: 7810, 0x08: 10420, 0x01: 15630, 0x09: 20830, 0x02: 31250,
    0x0A: 41670, 0x03: 62500, 0x04: 125000, 0x05: 250000, 0x06: 500000,
}
DEFAULT_BANDWIDTH = 125000

# A frame on the air carries what a receiver needs to decide whether it could
# have heard it: where it was sent, when, and for how long.
FRAME = struct.Struct("<4sIdd")
MAGIC = b"LORA"


def bandwidth_hz(code: int) -> int:
    return BANDWIDTHS.get(code, DEFAULT_BANDWIDTH)


def time_on_air(payload: int, spreading: int, bandwidth_code: int,
                coding_rate: int, low_data_rate: bool = False,
                preamble: int = 8, crc: bool = True,
                explicit_header: bool = True) -> float:
    """Seconds a packet of `payload` bytes occupies the channel."""
    spreading = max(5, min(12, spreading))
    hz = bandwidth_hz(bandwidth_code)
    symbol = (2 ** spreading) / hz

    # The datasheet's own expression. The denominator drops by two when the
    # low data rate optimisation is on, which is what it is for.
    denominator = 4 * (spreading - (2 if low_data_rate else 0))
    numerator = (8 * payload - 4 * spreading + 28
                 + (16 if crc else 0) - (20 if not explicit_header else 0))
    symbols = 8 + max(0, math.ceil(numerator / denominator) * (min(coding_rate, 4) + 4))
    return (preamble + 4.25) * symbol + symbols * symbol


def encode(frequency: int, sent_at: float, airtime: float, payload: bytes) -> bytes:
    return FRAME.pack(MAGIC, frequency, sent_at, airtime) + payload


def decode(frame: bytes):
    """The header and payload, or None if this is not one of ours."""
    if len(frame) < FRAME.size:
        return None
    magic, frequency, sent_at, airtime = FRAME.unpack(frame[:FRAME.size])
    if magic != MAGIC:
        return None
    return frequency, sent_at, airtime, frame[FRAME.size:]
