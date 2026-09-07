#!/usr/bin/env python3
"""An SDHC card in SPI mode (ADR 0033).

The host clocks a byte and gets a byte back, so this is a byte stream with a
queue of replies rather than a request-and-response device. A command is six
bytes and announces itself: the first has its top two bits set to `01`, which
is how a host resynchronises after the idle `0xFF` padding it sends while
waiting. Everything here was put in because a firmware asked for it.

Responses come in the shapes the specification names:

    R1  one byte of status; 0x01 while idle, 0x00 once initialised
    R3  R1 then the four-byte operating conditions register
    R7  R1 then the four bytes echoing the interface condition
    data  R1, then a gap, then the token 0xFE and the block, then a CRC

The gap matters. A real card takes a few byte times to answer, and hosts are
written to skip up to eight bytes looking for a response that is not 0xFF; a
card that answers instantly is not what they are written against.
"""

from __future__ import annotations

import logging

from emu.spi_link import SpiDevice

log = logging.getLogger("spi")


def crc16(payload: bytes) -> int:
    """The CRC a data block carries: CCITT, polynomial 0x1021, starting at zero.

    Worth computing rather than faking. A host that has turned checking on with
    CMD59 verifies this one, and a wrong value looks to it like a bad card.
    """
    remainder = 0
    for byte in payload:
        remainder ^= byte << 8
        for _ in range(8):
            remainder = ((remainder << 1) ^ 0x1021) if remainder & 0x8000 else remainder << 1
            remainder &= 0xFFFF
    return remainder

BLOCK = 512
IDLE = 0xFF

# Commands, by their number. Only what a host has actually sent is here.
GO_IDLE_STATE = 0
SEND_IF_COND = 8
SEND_CSD = 9
SEND_CID = 10
STOP_TRANSMISSION = 12
SEND_STATUS = 13                # answered with two bytes, not one
SET_BLOCKLEN = 16
READ_SINGLE_BLOCK = 17
READ_MULTIPLE_BLOCK = 18
WRITE_BLOCK = 24
APP_CMD = 55
CRC_ON_OFF = 59
READ_OCR = 58
APP_SEND_OP_COND = 41           # ACMD41, after CMD55
APP_SET_CLR_CARD_DETECT = 42    # ACMD42: the pull-up on the detect line
APP_SEND_SCR = 51               # ACMD51: the configuration register

R1_IDLE = 0x01
R1_READY = 0x00
R1_ILLEGAL = 0x04

DATA_TOKEN = 0xFE               # starts a block, both directions
DATA_ACCEPTED = 0x05            # the card took a written block

# High capacity, powered up and ready. Bit 30 is the flag that tells the host
# to address blocks rather than bytes, which is what makes this an SDHC card.
OCR_READY_SDHC = 0xC0FF8000

# The card configuration register: physical layer version two, both bus widths.
# Eight bytes, and it arrives as a data block like any other.
SCR = bytes((0x02, 0x35, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00))


class SdCard(SpiDevice):
    """Answers the SPI-mode conversation, over an in-memory image."""

    name = "sd-card"

    def __init__(self, image: bytearray, response_gap: int = 2):
        self.image = image
        self.response_gap = response_gap
        self.out: list[int] = []
        self.frame: list[int] = []
        self.expect_app_cmd = False
        self.initialised = False
        self.crc_enabled = False
        # Set when a write command is waiting for its data token and block.
        self.writing_to: int | None = None
        self.write_buffer: list[int] = []
        self.commands = 0
        self.blocks_read = 0
        self.blocks_written = 0

    # -- the bus ------------------------------------------------------------

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        reply = bytearray()
        for byte in data:
            reply.append(self.out.pop(0) if self.out else IDLE)
            self._consume(byte)
        return bytes(reply) if duplex else None

    # -- the conversation ---------------------------------------------------

    def _consume(self, byte: int) -> None:
        if self.writing_to is not None:
            self._consume_write(byte)
            return
        # Idle padding between commands, which is most of the traffic.
        if not self.frame and (byte & 0xC0) != 0x40:
            return
        self.frame.append(byte)
        if len(self.frame) < 6:
            return
        frame, self.frame = self.frame, []
        self._command(frame[0] & 0x3F,
                      int.from_bytes(bytes(frame[1:5]), "big"))

    def _consume_write(self, byte: int) -> None:
        if not self.write_buffer and byte != DATA_TOKEN:
            return                                  # still waiting for the token
        self.write_buffer.append(byte)
        if len(self.write_buffer) < 1 + BLOCK + 2:  # token, block, CRC
            return
        block = bytes(self.write_buffer[1:1 + BLOCK])
        at = self.writing_to * BLOCK
        if 0 <= at and at + BLOCK <= len(self.image):
            self.image[at:at + BLOCK] = block
            self.blocks_written += 1
        self.writing_to = None
        self.write_buffer = []
        # Accepted, then a moment of being busy, which a host waits out.
        self.out += [IDLE, DATA_ACCEPTED, 0x00, 0x00, IDLE]

    def _command(self, number: int, argument: int) -> None:
        self.commands += 1
        log.debug("sdcard: CMD%d arg 0x%08x", number, argument)
        app = self.expect_app_cmd
        self.expect_app_cmd = False

        if app and number == APP_SEND_OP_COND:
            # The host polls this until the card stops saying it is idle. One
            # poll is enough here; there is nothing to wait for.
            self.initialised = True
            self._respond([R1_READY])
        elif app and number == APP_SET_CLR_CARD_DETECT:
            # Whether the detect line keeps its pull-up. Nothing here has a
            # detect line, but refusing the command stopped a host dead.
            self._respond([self._status()])
        elif app and number == APP_SEND_SCR:
            self._respond([self._status()])
            self._block(SCR)
        elif number == GO_IDLE_STATE:
            self.initialised = False
            self._respond([R1_IDLE])
        elif number == SEND_IF_COND:
            # Echo the check pattern and the voltage back, which is how a host
            # tells a version two card from an older one.
            self._respond([R1_IDLE, 0x00, 0x00, 0x01, argument & 0xFF])
        elif number == APP_CMD:
            self.expect_app_cmd = True
            self._respond([R1_IDLE if not self.initialised else R1_READY])
        elif number == READ_OCR:
            self._respond([self._status()] + list(OCR_READY_SDHC.to_bytes(4, "big")))
        elif number == SET_BLOCKLEN:
            self._respond([self._status()])
        elif number == CRC_ON_OFF:
            # The host asks for its checks back. Saying no here is what made a
            # real firmware give up on the card entirely, so say yes and mean
            # it: every data block below carries a computed CRC.
            self.crc_enabled = bool(argument & 1)
            self._respond([self._status()])
        elif number in (SEND_CSD, SEND_CID):
            self._respond([self._status()])
            self._block(self._csd() if number == SEND_CSD else self._cid())
        elif number in (READ_SINGLE_BLOCK, READ_MULTIPLE_BLOCK):
            self._respond([self._status()])
            self._block(self._read_block(argument))
            self.blocks_read += 1
        elif number == WRITE_BLOCK:
            self._respond([self._status()])
            self.writing_to = argument
        elif number == STOP_TRANSMISSION:
            self._respond([self._status()])
        elif number == SEND_STATUS:
            # R2: the usual status byte and a second one for the faults a card
            # can report. Nothing here has any, so the second byte is zero.
            self._respond([self._status(), 0x00])
        else:
            # Saying so is what lets a host move on; silence looks like a card
            # that has died, and hosts retry those for a long time.
            log.debug("sdcard: unsupported command %d", number)
            self._respond([R1_ILLEGAL])

    # -- pieces of an answer ------------------------------------------------

    def _status(self) -> int:
        return R1_READY if self.initialised else R1_IDLE

    def _respond(self, payload: list[int]) -> None:
        self.out += [IDLE] * self.response_gap + payload

    def _block(self, payload: bytes) -> None:
        self.out += [IDLE] * self.response_gap
        self.out.append(DATA_TOKEN)
        self.out += list(payload)
        self.out += list(crc16(payload).to_bytes(2, "big"))

    def _read_block(self, number: int) -> bytes:
        at = number * BLOCK
        if at < 0 or at + BLOCK > len(self.image):
            return bytes(BLOCK)
        return bytes(self.image[at:at + BLOCK])

    def _csd(self) -> bytes:
        """Card-specific data, version two, which states the size in a field
        counting half-megabyte units minus one."""
        size = len(self.image) // (512 * 1024) - 1
        csd = bytearray(16)
        csd[0] = 0x40                               # version two
        csd[1] = 0x0E                               # data read access time
        csd[3] = 0x32                               # transfer speed, 25 MHz
        csd[4] = 0x5B                               # the command classes
        csd[5] = 0x59                               # read block length, 512
        csd[6] = 0x00
        csd[7] = (size >> 16) & 0x3F
        csd[8] = (size >> 8) & 0xFF
        csd[9] = size & 0xFF
        csd[10] = 0x7F
        csd[11] = 0x80
        csd[13] = 0x0A
        csd[14] = 0x40
        csd[15] = 0x01                              # the CRC slot, always odd
        return bytes(csd)

    def _cid(self) -> bytes:
        cid = bytearray(16)
        cid[0] = 0x02                               # manufacturer
        cid[1:3] = b"BE"                            # who made it
        cid[3:8] = b"EMUSD"                         # what it is called
        cid[8] = 0x10                               # revision
        cid[9:13] = (1).to_bytes(4, "big")          # serial
        cid[13:15] = b"\x01\x40"                    # made, notionally
        cid[15] = 0x01
        return bytes(cid)

    def __repr__(self) -> str:
        return (f"<{self.name} {len(self.image) // (1024 * 1024)} MB "
                f"commands={self.commands} read={self.blocks_read} "
                f"written={self.blocks_written}>")
