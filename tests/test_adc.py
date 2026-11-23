#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The SAR ADC handshake the vendor firmware's analogRead() spins on."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.peripherals.adc import (  # noqa: E402
    DATA_MASK, DEFAULT_TSENS_OUT, DONE_SAR, R_MEAS1_CTRL2, R_TSENS_CTRL,
    START_SAR, TSENS_DUMP_OUT, TSENS_OUT_MASK, TSENS_READY, SarAdc,
)

SENS = 0x60008800


def test_a_start_pulse_completes_with_a_result() -> None:
    """The exact loop from the firmware: clear START, set START, then spin on
    DONE. The result is in the same register's low half."""
    adc = SarAdc("sens", SENS, raw=2200)
    adc.write(R_MEAS1_CTRL2, 0x0000A000, 4)             # START clear
    adc.write(R_MEAS1_CTRL2, 0x0000A000 | START_SAR, 4)  # START set
    v = adc.read(R_MEAS1_CTRL2, 4)
    assert v & DONE_SAR
    assert v & DATA_MASK == 2200
    assert adc.conversions == 1


def test_clearing_start_drops_done_so_the_next_read_waits_again() -> None:
    adc = SarAdc("sens", SENS)
    adc.write(R_MEAS1_CTRL2, START_SAR, 4)
    assert adc.read(R_MEAS1_CTRL2, 4) & DONE_SAR
    adc.write(R_MEAS1_CTRL2, 0, 4)
    assert not adc.read(R_MEAS1_CTRL2, 4) & DONE_SAR
    adc.write(R_MEAS1_CTRL2, START_SAR, 4)
    assert adc.conversions == 2


def test_holding_start_high_is_one_conversion_not_many() -> None:
    """Only the edge starts a conversion; rewriting START high is the driver's
    own read-modify-write, not a new request."""
    adc = SarAdc("sens", SENS)
    for _ in range(5):
        adc.write(R_MEAS1_CTRL2, START_SAR | 0x5, 4)
    assert adc.conversions == 1


def test_every_other_register_is_a_plain_store() -> None:
    adc = SarAdc("sens", SENS)
    adc.write(0x10, 0x12345678, 4)      # SAR_MEAS1_MUX
    adc.write(0x14, 0x0000000F, 4)      # SAR_ATTEN1
    assert adc.read(0x10, 4) == 0x12345678
    assert adc.read(0x14, 4) == 0x0000000F
    assert adc.read(0x4, 4) == 0


def test_a_temperature_dump_is_ready_at_once_and_only_while_asked() -> None:
    """The loop Meshtastic spins on after RF calibration: set DUMP_OUT, wait
    for READY, take the low byte. Nothing is stored back, so the driver's own
    read-modify-writes keep seeing what they wrote."""
    adc = SarAdc("sens", SENS)
    adc.write(R_TSENS_CTRL, (1 << 22) | (1 << 23), 4)          # powered, not asked
    assert not adc.read(R_TSENS_CTRL, 4) & TSENS_READY
    adc.write(R_TSENS_CTRL, (1 << 22) | (1 << 23) | TSENS_DUMP_OUT, 4)
    v = adc.read(R_TSENS_CTRL, 4)
    assert v & TSENS_READY and v & TSENS_OUT_MASK == DEFAULT_TSENS_OUT
    assert v & TSENS_DUMP_OUT
    adc.write(R_TSENS_CTRL, (1 << 22) | (1 << 23), 4)          # dump cleared
    assert not adc.read(R_TSENS_CTRL, 4) & TSENS_READY
