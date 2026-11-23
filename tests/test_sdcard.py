#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The card in the slot, and what is on it (ADR 0033).

The card is a byte stream: every byte the host clocks gets one back, and a
reply arrives a few bytes after the command that asked for it. So these tests
send a command, then clock idle bytes, and read the first byte that is not
idle -- which is exactly what a host driver does.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import sdimage  # noqa: E402
from emu.devices.sdcard import (  # noqa: E402
    BUSY, DATA_ACCEPTED, DATA_TOKEN, IDLE, MULTIPLE_TOKEN, OCR_READY_SDHC,
    R1_IDLE, R1_ILLEGAL, R1_READY, STOP_TOKEN, SdCard, crc16,
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


# -- runs of blocks, as the host drives them (ADR 0118) -----------------------
#
# These follow arduino-esp32's sd_diskio.cpp (2.0.17), the driver the
# Meshtastic build links: what it sends, and which byte it reads each answer
# from.

def xfer(card: SdCard, byte: int = IDLE) -> int:
    return card.on_segment(1, bytes([byte]), True)[0]


def host_command(card: SdCard, number: int, argument: int = 0) -> int:
    """sdCommand: the frame, with a stuff byte after CMD12, its replies
    ignored; then up to nine bytes looking for one with the top bit clear."""
    frame = bytes([0x40 | number]) + argument.to_bytes(4, "big") + b"\x01"
    card.on_segment(1, frame + (b"\xff" if number == 12 else b""), False)
    token = IDLE
    for _ in range(9):
        token = xfer(card)
        if not token & 0x80:
            break
    return token


def host_read_bytes(card: SdCard, patience: int = 64) -> bytes:
    """sdReadBytes: poll for the token, then the block, and check its CRC."""
    token = IDLE
    for _ in range(patience):
        token = xfer(card)
        if token != IDLE:
            break
    assert token == DATA_TOKEN, f"no data token, got 0x{token:02x}"
    data = card.on_segment(1, b"\xff" * (SECTOR + 2), True)
    block = data[:SECTOR]
    assert int.from_bytes(data[SECTOR:], "big") == crc16(block)
    return block


def host_wait(card: SdCard, patience: int = 64) -> int:
    """sdWait: clock until the card stops saying busy."""
    for _ in range(patience):
        byte = xfer(card)
        if byte != BUSY:
            return byte
    return BUSY


def host_write_bytes(card: SdCard, payload: bytes, token: int) -> int:
    """sdWriteBytes: wait, then token, block and CRC, and the data response
    from the very next byte."""
    assert host_wait(card) != BUSY
    card.on_segment(1, bytes([token]) + payload + crc16(payload).to_bytes(2, "big"), False)
    return xfer(card) & 0x1F


def blocks(first: int, count: int) -> list[bytes]:
    return [bytes([first + n]) * SECTOR for n in range(count)]


def test_a_multiple_block_read_streams_until_it_is_stopped(card: SdCard) -> None:
    """sdReadSectors: one CMD18 for a run of sectors, then CMD12. A card that
    sent one block left the host polling 500 ms for the second."""
    initialise(card)
    wanted = blocks(100, 3)
    for n, block in enumerate(wanted):
        card.image[(100 + n) * SECTOR:(101 + n) * SECTOR] = block
    assert host_command(card, 18, 100) == R1_READY
    assert [host_read_bytes(card) for _ in wanted] == wanted
    assert host_command(card, 12) == R1_READY
    assert all(xfer(card) == IDLE for _ in range(SECTOR + 8)), "still streaming"


def test_a_read_stopped_partway_through_a_block_sends_nothing_more(card: SdCard) -> None:
    initialise(card)
    assert host_command(card, 18, 0) == R1_READY
    while xfer(card) != DATA_TOKEN:
        pass
    card.on_segment(1, b"\xff" * 100, True)
    assert host_command(card, 12) == R1_READY
    assert all(xfer(card) == IDLE for _ in range(SECTOR + 8))


def test_a_multiple_block_write_lands_every_block(card: SdCard) -> None:
    """sdWriteSectors: ACMD23, CMD25, each block behind 0xFC answered 0x05,
    then 0xFD and busy, then the card's status."""
    initialise(card)
    payloads = blocks(7, 3)
    assert host_command(card, 55) == R1_READY
    assert host_command(card, 23, len(payloads)) == R1_READY
    assert host_command(card, 25, 200) == R1_READY
    assert [host_write_bytes(card, p, MULTIPLE_TOKEN) for p in payloads] == [DATA_ACCEPTED] * 3
    assert host_wait(card) != BUSY
    card.on_segment(1, bytes([STOP_TOKEN]), False)           # sdStop
    assert xfer(card) == BUSY, "busy follows the stop token"
    assert host_wait(card) != BUSY
    assert host_command(card, 13) == R1_READY and xfer(card) == 0x00
    assert card.blocks_written == 3
    assert [read_block(card, 200 + n)[1:1 + SECTOR] for n in range(3)] == payloads


def test_a_single_write_is_answered_in_the_byte_after_its_crc(card: SdCard) -> None:
    """Where sdWriteBytes reads it. An idle byte there read as 0x1F."""
    initialise(card)
    assert host_command(card, 24, 60) == R1_READY
    assert host_write_bytes(card, bytes(SECTOR), DATA_TOKEN) == DATA_ACCEPTED


# -- answers given ahead (ADR 0172) -------------------------------------------

def offer_holds(card: SdCard) -> bytes:
    """What the card offers is what idle bytes are then answered with, byte
    for byte. Checked on a copy, so the card itself is left as it was."""
    offer = card.ahead(2048)
    twin = copy.deepcopy(card)
    assert twin.on_segment(1, b"\xff" * len(offer), True) == offer
    return offer


def test_an_offer_is_what_idle_bytes_are_answered_with(card: SdCard) -> None:
    initialise(card)
    for n, block in enumerate(blocks(40, 4)):
        card.image[(40 + n) * SECTOR:(41 + n) * SECTOR] = block
    assert offer_holds(card) == b""                    # nothing queued, no read open
    card.on_segment(1, bytes([0x40 | 17]) + (40).to_bytes(4, "big") + b"\x01", False)
    single = offer_holds(card)
    assert DATA_TOKEN in single and blocks(40, 1)[0] in single
    card.on_segment(1, b"\xff" * len(single), True)
    assert host_command(card, 18, 41) == R1_READY
    streamed = offer_holds(card)
    assert len(streamed) == 2048, "an open read offers as far as it may"
    assert blocks(41, 1)[0] in streamed and blocks(42, 1)[0] in streamed
    card.on_segment(1, b"\xff" * 700, True)            # part-way into the second
    offer_holds(card)


def test_nothing_is_offered_inside_a_command_or_a_write(card: SdCard) -> None:
    initialise(card)
    card.on_segment(1, bytes([0x40 | 17, 0, 0]), True)    # half a command
    assert card.ahead(64) == b""
    card.on_segment(1, bytes([0, 0, 0x01]), True)
    card.on_segment(1, b"\xff" * 600, True)
    assert host_command(card, 24, 9) == R1_READY          # a write waiting for its block
    assert card.ahead(64) == b""


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
