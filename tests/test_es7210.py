#!/usr/bin/env python3
"""The microphone codec, which the vendor firmware probes for by name."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.es7210 import (  # noqa: E402
    CHIP_ID_HIGH, CHIP_ID_HIGH_REGISTER, CHIP_ID_LOW, CHIP_ID_LOW_REGISTER,
    Es7210,
)


@pytest.fixture()
def codec() -> Es7210:
    return Es7210()


def select(codec: Es7210, register: int, *payload: int) -> None:
    codec.start(reading=False)
    codec.write(bytes([register, *payload]))


def read(codec: Es7210, count: int = 1) -> bytes:
    codec.start(reading=True)
    return codec.read(count)


def test_it_sits_where_the_firmware_probes(codec: Es7210) -> None:
    """`ES7210 address not found` was the firmware saying nothing answered
    here."""
    assert codec.address == 0x40


def test_it_says_which_part_it_is(codec: Es7210) -> None:
    """The two registers a driver reads spell the part number."""
    select(codec, CHIP_ID_HIGH_REGISTER)
    assert read(codec)[0] == CHIP_ID_HIGH
    select(codec, CHIP_ID_LOW_REGISTER)
    assert read(codec)[0] == CHIP_ID_LOW


def test_the_identity_survives_configuration(codec: Es7210) -> None:
    """A driver writes most of the register space while setting the part up,
    and must still be able to confirm what it is talking to afterwards."""
    for register in range(0x00, 0x60):
        select(codec, register, 0xA5)
    select(codec, CHIP_ID_HIGH_REGISTER)
    assert read(codec)[0] == CHIP_ID_HIGH


def test_a_written_register_reads_back(codec: Es7210) -> None:
    select(codec, 0x0B, 0x37)
    select(codec, 0x0B)
    assert read(codec)[0] == 0x37


def test_a_read_walks_on_through_the_registers(codec: Es7210) -> None:
    """Registers are read in runs, so the second byte is the next one along."""
    select(codec, 0x20, 0x11, 0x22, 0x33)
    select(codec, 0x20)
    assert read(codec, 3) == bytes([0x11, 0x22, 0x33])
