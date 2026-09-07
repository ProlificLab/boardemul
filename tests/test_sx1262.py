#!/usr/bin/env python3
"""What the radio answers (ADR 0034).

The part replies to byte N while byte N is being clocked, so these tests clock
a whole window and read the reply positionally, which is what the host does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.sx1262 import (  # noqa: E402
    COMMAND_OK, MODE_STANDBY_RC, MODE_TX, READ_REGISTER, Sx1262,
    VERSION_ADDRESS, VERSION_STRING, WRITE_REGISTER,
)


@pytest.fixture()
def radio() -> Sx1262:
    return Sx1262()


def window(radio: Sx1262, frame: bytes) -> bytes:
    """One chip-select window: clock the bytes, then release."""
    reply = radio.on_segment(1, frame, True)
    radio.on_deselect()
    return reply


def test_every_byte_comes_back_with_a_status(radio: Sx1262) -> None:
    """A host checks this constantly, and the two values it reads as an absent
    chip are all-zeroes and all-ones. Neither may ever be the answer."""
    reply = window(radio, bytes([0x80, 0x00]))
    assert set(reply) == {radio.status}
    assert radio.status not in (0x00, 0xFF)
    assert (radio.status >> 4) & 0x7 == MODE_STANDBY_RC
    assert (radio.status >> 1) & 0x7 == COMMAND_OK


def test_the_version_string_is_where_a_host_looks_for_it(radio: Sx1262) -> None:
    """Read sixteen bytes from the version register: opcode, two address bytes
    and one of padding come back as status, then the string.

    Both the offset and the name matter, and the name is not the one on the
    module: with any other, both firmwares refuse the part outright (ADR 0034).
    """
    assert VERSION_STRING == b"SX1261"
    reply = window(radio, bytes([READ_REGISTER, 0x03, 0x20, 0x00]) + bytes(16))
    assert reply[:4] == bytes([radio.status]) * 4
    assert reply[4:4 + len(VERSION_STRING)] == VERSION_STRING


def test_a_written_register_reads_back(radio: Sx1262) -> None:
    window(radio, bytes([WRITE_REGISTER, 0x07, 0x40, 0xAB, 0xCD]))
    reply = window(radio, bytes([READ_REGISTER, 0x07, 0x40, 0x00, 0x00, 0x00]))
    assert reply[4:6] == bytes([0xAB, 0xCD])


def test_the_version_register_survives_a_write_elsewhere(radio: Sx1262) -> None:
    """Guards the thing a host identifies the part by."""
    window(radio, bytes([WRITE_REGISTER, 0x07, 0x40, 0x01]))
    assert bytes(radio.registers[VERSION_ADDRESS:VERSION_ADDRESS + 6]) == VERSION_STRING


def test_a_command_is_framed_by_the_chip_select(radio: Sx1262) -> None:
    """The length of a read is the host's choice, so only the select going high
    says the command is over. Two windows must not run together."""
    radio.on_segment(1, bytes([0x80]), True)
    radio.on_segment(1, bytes([0x01]), True)        # same window, still standby
    radio.on_deselect()
    assert radio.commands == 1
    window(radio, bytes([0x80, 0x00]))
    assert radio.commands == 2


def test_transmitting_reports_that_it_finished(radio: Sx1262) -> None:
    """Nothing goes out, but a host that sends waits for this flag and blocks
    for its whole timeout without it."""
    window(radio, bytes([0x83, 0x00, 0x00, 0x00]))
    assert radio.mode == MODE_TX
    reply = window(radio, bytes([0x12, 0x00, 0x00, 0x00]))   # GetIrqStatus
    assert (reply[2] << 8 | reply[3]) & 0x0001, "TxDone was not raised"


def test_clearing_the_flags_clears_them(radio: Sx1262) -> None:
    window(radio, bytes([0x83, 0x00, 0x00, 0x00]))
    window(radio, bytes([0x02, 0xFF, 0xFF]))                # ClearIrqStatus
    reply = window(radio, bytes([0x12, 0x00, 0x00, 0x00]))
    assert (reply[2] << 8 | reply[3]) == 0


def test_nothing_is_ever_received(radio: Sx1262) -> None:
    """A node on its own hears nothing, and the strengths it reads are the
    floor rather than a plausible-looking signal."""
    window(radio, bytes([0x82, 0x00, 0x00, 0x00]))           # SetRx
    reply = window(radio, bytes([0x12, 0x00, 0x00, 0x00]))
    assert (reply[2] << 8 | reply[3]) == 0, "a receive flag was raised with no radio"
