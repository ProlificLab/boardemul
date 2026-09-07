#!/usr/bin/env python3
"""The card in the slot, and what is on it (ADR 0033).

The card is a byte stream: every byte the host clocks gets one back, and a
reply arrives a few bytes after the command that asked for it. So these tests
send a command, then clock idle bytes, and read the first byte that is not
idle -- which is exactly what a host driver does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import sdimage  # noqa: E402
from emu.devices.sdcard import (  # noqa: E402
    DATA_TOKEN, IDLE, OCR_READY_SDHC, R1_IDLE, R1_ILLEGAL, R1_READY, SdCard,
    crc16,
)

SECTOR = sdimage.SECTOR


def command(card: SdCard, number: int, argument: int = 0) -> None:
    frame = bytes([0x40 | number]) + argument.to_bytes(4, "big") + b"\xff"
    card.on_segment(1, frame, True)


def answer(card: SdCard, length: int = 1, patience: int = 8) -> bytes:
    """Clock idle bytes until the card says something, then take `length`."""
    out = bytearray()
    for _ in range(patience + length):
        byte = card.on_segment(1, b"\xff", True)[0]
        if out or byte != IDLE:
            out.append(byte)
        if len(out) == length:
            break
    return bytes(out)


@pytest.fixture()
def card() -> SdCard:
    return SdCard(sdimage.build(size_mb=8))


def initialise(card: SdCard) -> None:
    command(card, 0); answer(card)
    command(card, 55); answer(card)
    command(card, 41, 0x40000000); answer(card)


# -- the conversation -------------------------------------------------------

def test_a_reset_leaves_the_card_idle(card: SdCard) -> None:
    command(card, 0)
    assert answer(card) == bytes([R1_IDLE])


def test_the_interface_condition_is_echoed_back(card: SdCard) -> None:
    """How a host tells a version two card from an older one: it sends a check
    pattern and the card repeats it."""
    command(card, 8, 0x000001AA)
    assert answer(card, 5) == bytes([R1_IDLE, 0x00, 0x00, 0x01, 0xAA])


def test_the_card_reports_ready_once_initialised(card: SdCard) -> None:
    initialise(card)
    command(card, 13)
    assert answer(card, 2) == bytes([R1_READY, 0x00])


def test_the_operating_conditions_say_high_capacity(card: SdCard) -> None:
    """Bit 30 is what tells the host to address blocks, not bytes. Without it
    every read after the first megabyte would land somewhere else."""
    initialise(card)
    command(card, 58)
    reply = answer(card, 5)
    assert reply[0] == R1_READY
    assert int.from_bytes(reply[1:], "big") == OCR_READY_SDHC
    assert OCR_READY_SDHC & (1 << 30)


def test_turning_crc_checking_on_is_accepted(card: SdCard) -> None:
    """The regression that mattered: refusing this stopped the vendor firmware
    dead, two commands into its start-up, and it never reached the card."""
    command(card, 59, 1)
    assert answer(card) == bytes([R1_IDLE])
    assert card.crc_enabled


def test_an_unknown_command_is_refused_rather_than_ignored(card: SdCard) -> None:
    """Silence looks like a card that has died, and hosts retry those."""
    command(card, 60)
    assert answer(card) == bytes([R1_ILLEGAL])


# -- the blocks -------------------------------------------------------------

def read_block(card: SdCard, number: int) -> bytes:
    command(card, 17, number)
    assert answer(card) == bytes([R1_READY])
    out = bytearray()
    while len(out) < 1 + SECTOR + 2:
        byte = card.on_segment(1, b"\xff", True)[0]
        if out or byte == DATA_TOKEN:
            out.append(byte)
    return bytes(out)


def test_a_read_returns_the_block_and_a_real_crc(card: SdCard) -> None:
    """A host that turned checking on verifies this, so the value has to be
    computed rather than filled in."""
    initialise(card)
    framed = read_block(card, 0)
    assert framed[0] == DATA_TOKEN
    block = framed[1:1 + SECTOR]
    assert block == bytes(card.image[:SECTOR])
    assert int.from_bytes(framed[1 + SECTOR:], "big") == crc16(block)


def test_a_written_block_reads_back(card: SdCard) -> None:
    initialise(card)
    payload = bytes(range(256)) * 2
    command(card, 24, 40)
    answer(card)
    card.on_segment(1, bytes([DATA_TOKEN]) + payload + b"\x00\x00", True)
    assert card.blocks_written == 1
    assert read_block(card, 40)[1:1 + SECTOR] == payload


def test_a_read_past_the_end_is_zeros_not_a_crash(card: SdCard) -> None:
    initialise(card)
    assert read_block(card, len(card.image) // SECTOR + 10)[1:1 + SECTOR] == bytes(SECTOR)


# -- what is on the card ----------------------------------------------------

def test_the_image_carries_a_partition_and_a_volume() -> None:
    image = sdimage.build(size_mb=8)
    assert len(image) == 8 * 1024 * 1024
    assert image[510:512] == b"\x55\xaa", "no boot signature on the master record"
    assert image[446 + 4] == sdimage.PARTITION_TYPE_FAT16
    start = int.from_bytes(image[446 + 8:446 + 12], "little")
    assert start == sdimage.PARTITION_LBA

    volume = start * SECTOR
    assert image[volume + 510:volume + 512] == b"\x55\xaa"
    assert bytes(image[volume + 54:volume + 62]) == b"FAT16   "
    assert int.from_bytes(image[volume + 11:volume + 13], "little") == SECTOR


def test_a_file_is_placed_where_its_directory_entry_says() -> None:
    """The point of building the image here rather than shipping one: a test
    can put a file on the card and then look for it."""
    payload = b"from the emulator" * 4
    image = sdimage.build(size_mb=8, files=(sdimage.File("HELLO.TXT", payload),))
    volume = sdimage.PARTITION_LBA * SECTOR
    per_fat = int.from_bytes(image[volume + 22:volume + 24], "little")
    root = volume + (sdimage.RESERVED_SECTORS + sdimage.FAT_COPIES * per_fat) * SECTOR

    entry = root + 32                       # the volume label takes the first
    assert bytes(image[entry:entry + 11]) == b"HELLO   TXT"
    assert int.from_bytes(image[entry + 28:entry + 32], "little") == len(payload)

    cluster = int.from_bytes(image[entry + 26:entry + 28], "little")
    data = root + sdimage.ROOT_ENTRIES * 32
    at = data + (cluster - sdimage.FIRST_DATA_CLUSTER) * sdimage.SECTORS_PER_CLUSTER * SECTOR
    assert bytes(image[at:at + len(payload)]) == payload
