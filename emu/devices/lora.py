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
from typing import NamedTuple

# Thermal noise in a one-hertz band at room temperature, in dBm: the floor
# every radio link is measured against.
THERMAL_NOISE_DBM = -174.0

# What the receiver adds to that floor. Six decibels is the figure quoted for
# this family and is what makes the arithmetic below reproduce the datasheet's
# own sensitivity numbers.
NOISE_FIGURE_DB = 6.0

# The signal-to-noise ratio each spreading factor can still decode, in dB.
# Slower spreading digs further into the noise, which is what it is for.
SNR_LIMIT_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5,
                10: -15.0, 11: -17.5, 12: -20.0}

# The bandwidth each code means, in hertz, as the part defines them.
BANDWIDTHS = {
    0x00: 7810, 0x08: 10420, 0x01: 15630, 0x09: 20830, 0x02: 31250,
    0x0A: 41670, 0x03: 62500, 0x04: 125000, 0x05: 250000, 0x06: 500000,
}
DEFAULT_BANDWIDTH = 125000

# A frame on the air carries what a receiver needs to decide whether it could
# have heard it: where it was sent, when, and for how long.
FRAME = struct.Struct("<4sIddffff")
MAGIC = b"LORA"


class Frame(NamedTuple):
    """A packet in the air, and what a receiver needs to judge it."""

    frequency: int
    sent_at: float
    airtime: float
    power_dbm: float
    origin: tuple[float, float]
    gain_dbi: float
    payload: bytes


# The part counts frequency in steps of the crystal divided by two to the
# twenty-fifth, not in hertz, so a register value has to be converted before it
# means anything in a propagation sum.
CRYSTAL_HZ = 32_000_000
FREQUENCY_STEP_HZ = CRYSTAL_HZ / (2 ** 25)


def register_to_hz(raw: int) -> int:
    """The frequency a register value actually stands for."""
    return int(round(raw * FREQUENCY_STEP_HZ))


def bandwidth_hz(code: int) -> int:
    return BANDWIDTHS.get(code, DEFAULT_BANDWIDTH)


def symbol_time(spreading: int, bandwidth_code: int) -> float:
    """Seconds one symbol lasts: 2^SF / bandwidth."""
    return (2 ** max(5, min(12, spreading))) / bandwidth_hz(bandwidth_code)


def time_on_air(payload: int, spreading: int, bandwidth_code: int,
                coding_rate: int, low_data_rate: bool = False,
                preamble: int = 8, crc: bool = True,
                explicit_header: bool = True) -> float:
    """Seconds a packet of `payload` bytes occupies the channel."""
    spreading = max(5, min(12, spreading))
    symbol = symbol_time(spreading, bandwidth_code)

    # The datasheet's own expression. The denominator drops by two when the
    # low data rate optimisation is on, which is what it is for.
    denominator = 4 * (spreading - (2 if low_data_rate else 0))
    numerator = (8 * payload - 4 * spreading + 28
                 + (16 if crc else 0) - (20 if not explicit_header else 0))
    symbols = 8 + max(0, math.ceil(numerator / denominator) * (min(coding_rate, 4) + 4))
    return (preamble + 4.25) * symbol + symbols * symbol


def noise_floor_dbm(bandwidth_code: int) -> float:
    """The noise in the channel the receiver is listening to."""
    return (THERMAL_NOISE_DBM + 10 * math.log10(bandwidth_hz(bandwidth_code))
            + NOISE_FIGURE_DB)


def sensitivity_dbm(bandwidth_code: int, spreading: int) -> float:
    """The weakest signal this setting can still decode.

    Derived, not tabulated: the channel's noise floor plus how far under it the
    spreading factor can reach. It reproduces the figures the datasheet quotes,
    which is the check that the derivation is right rather than convenient.
    """
    limit = SNR_LIMIT_DB.get(max(5, min(12, spreading)), -20.0)
    return noise_floor_dbm(bandwidth_code) + limit


# How fast signal falls away, by the kind of place it is crossing. Two is free
# space and follows from the geometry; the rest are the representative values
# quoted for these environments in the standard texts, where each is given as a
# range. They are named so a scenario can say where it is rather than pick a
# number, and any number can still be given instead.
ENVIRONMENTS = {
    "free-space": 2.0,
    "open": 2.5,
    "suburban": 3.0,
    "urban": 3.5,
    "indoor": 4.5,
}
FREE_SPACE_EXPONENT = 2.0
REFERENCE_METRES = 1.0


def free_space_loss_db(distance_m: float, frequency_hz: int) -> float:
    """Loss between two points with nothing in the way, by the standard
    expression. This is the geometry alone and has no parameters to choose."""
    metres = max(REFERENCE_METRES, distance_m)
    hz = max(1, frequency_hz)
    return (20 * math.log10(metres) + 20 * math.log10(hz)
            - 147.55)          # 20*log10(4*pi/c), the constant of the formula


def path_loss_db(distance_m: float, frequency_hz: int,
                 exponent: float = FREE_SPACE_EXPONENT,
                 shadowing_db: float = 0.0) -> float:
    """Loss over a distance in a given kind of place.

    The log-distance model: free space out to a reference metre, and beyond it
    the loss grows with the exponent for the environment. At two it reduces
    exactly to free space, which is why that is the default and why nothing
    about existing behaviour changes.

    `shadowing_db` is whatever obstruction the scenario wants to state, added
    as it is. It is deliberately not drawn at random here: a run that cannot be
    repeated is worth less than one that can (ADR 0004).
    """
    metres = max(REFERENCE_METRES, distance_m)
    reference = free_space_loss_db(REFERENCE_METRES, frequency_hz)
    return (reference + 10 * exponent * math.log10(metres / REFERENCE_METRES)
            + shadowing_db)


def received_dbm(transmit_dbm: float, distance_m: float, frequency_hz: int,
                 exponent: float = FREE_SPACE_EXPONENT,
                 transmit_gain_dbi: float = 0.0,
                 receive_gain_dbi: float = 0.0,
                 shadowing_db: float = 0.0) -> float:
    """What arrives at the far end: what was sent, plus what the antennas at
    each end contribute, less what the path takes."""
    return (transmit_dbm + transmit_gain_dbi + receive_gain_dbi
            - path_loss_db(distance_m, frequency_hz, exponent, shadowing_db))


def encode(frequency: int, sent_at: float, airtime: float, payload: bytes,
           power_dbm: float = 22.0, x: float = 0.0, y: float = 0.0,
           gain_dbi: float = 0.0) -> bytes:
    return (FRAME.pack(MAGIC, frequency, sent_at, airtime, power_dbm, x, y,
                       gain_dbi) + payload)


def decode(frame: bytes):
    """The header and payload, or None if this is not one of ours."""
    if len(frame) < FRAME.size:
        return None
    (magic, frequency, sent_at, airtime, power, x, y,
     gain) = FRAME.unpack(frame[:FRAME.size])
    if magic != MAGIC:
        return None
    return Frame(frequency, sent_at, airtime, power, (x, y), gain,
                 frame[FRAME.size:])
