#!/usr/bin/env python3
"""The LoRa radio, as far as a host can tell without one (ADR 0034).

Everything the host learns about this part it learns over SPI, and every
exchange is framed by the chip select: an opcode, its parameters, and for a
read as many bytes as the host chooses to clock. The reply to byte N is decided
while byte N is being clocked, which is why this answers per byte rather than
per command, and applies the command once the window closes.

The chip answers every byte with a status: which mode it is in and how the last
command went. A host checks it constantly, and a part that answers `0xFF` --
which is what an empty chip select reads as -- looks to RadioLib like a radio
that is not there. That is precisely what both firmwares concluded.

There is no radio here and nothing is transmitted. What this models is the
conversation: the firmware configures a radio, is told the configuration took,
and hears no traffic, which is what a real node alone in a room experiences.
"""

from __future__ import annotations

import logging

from emu.spi_link import SpiDevice

log = logging.getLogger("spi")

REGISTER_SPACE = 0x1000
BUFFER_SIZE = 256

# Opcodes, named as the datasheet names them. Only what a firmware has sent.
SET_SLEEP = 0x84
SET_STANDBY = 0x80
SET_FS = 0xC1
SET_TX = 0x83
SET_RX = 0x82
STOP_TIMER_ON_PREAMBLE = 0x9F
SET_RX_DUTY_CYCLE = 0x94
SET_CAD = 0xC5_00           # not a real opcode; CAD is 0xC5 in some revisions
CALIBRATE = 0x89
CALIBRATE_IMAGE = 0x98
SET_PA_CONFIG = 0x95
SET_RX_TX_FALLBACK = 0x93
WRITE_REGISTER = 0x0D
READ_REGISTER = 0x1D
WRITE_BUFFER = 0x0E
READ_BUFFER = 0x1E
SET_DIO_IRQ_PARAMS = 0x08
GET_IRQ_STATUS = 0x12
CLEAR_IRQ_STATUS = 0x02
SET_DIO2_AS_RF_SWITCH = 0x9D
SET_DIO3_AS_TCXO = 0x97
SET_RF_FREQUENCY = 0x86
SET_PACKET_TYPE = 0x8A
GET_PACKET_TYPE = 0x11
SET_TX_PARAMS = 0x8E
SET_MODULATION_PARAMS = 0x8B
SET_PACKET_PARAMS = 0x8C
SET_BUFFER_BASE = 0x8F
SET_LORA_SYMB_TIMEOUT = 0xA0
GET_STATUS = 0xC0
GET_RSSI_INST = 0x15_00     # placeholder, see GET_DEVICE_ERRORS
GET_RX_BUFFER_STATUS = 0x13
GET_PACKET_STATUS = 0x14
GET_DEVICE_ERRORS = 0x17
CLEAR_DEVICE_ERRORS = 0x07
SET_REGULATOR_MODE = 0x96

# Interrupt flags, in the register a host reads after being nudged.
IRQ_TX_DONE = 0x0001
IRQ_RX_DONE = 0x0002

# Chip modes, in the top nibble of the status byte.
MODE_STANDBY_RC = 2
MODE_STANDBY_XOSC = 3
MODE_FS = 4
MODE_RX = 5
MODE_TX = 6

# Command status, in bits 3:1. Three, four and five are the failures a host
# reacts to, so a working part reports none of them.
COMMAND_OK = 2

# Where the part keeps the string a host reads to find out what it is talking
# to. RadioLib compares the first characters against the model it expects, and
# a mismatch is reported as no chip at all.
VERSION_ADDRESS = 0x0320
# What the part answers when a host asks what it is. Not a guess and not the
# name on the module: both firmwares refuse "SX1262" and accept this, which is
# the only evidence available and is what ADR 0006 says to follow. With it the
# vendor build prints "Start Radio success!" and Meshtastic goes on to
# configure the radio; with "SX1262" both give up.
VERSION_STRING = b"SX1261"

# Where the contents start in a register read: the opcode, two address bytes
# and one of padding come first, which is what RadioLib's own framing gives.
DATA_INDEX = 4


class Sx1262(SpiDevice):
    """Answers the SX126x command set over one chip select."""

    name = "sx1262"

    def __init__(self, air=None, signal=None) -> None:
        # `air` carries frames to and from other boards; `signal` is how this
        # part nudges the processor, which it does on a line of its own.
        self.air = air
        self.signal = signal
        self.tx_base = 0
        self.rx_base = 0
        self.payload_length = 0
        self.received_length = 0
        self.sent = 0
        self.heard = 0
        self.registers = bytearray(REGISTER_SPACE)
        self.registers[VERSION_ADDRESS:VERSION_ADDRESS + len(VERSION_STRING)] = VERSION_STRING
        self.buffer = bytearray(BUFFER_SIZE)
        self.mode = MODE_STANDBY_RC
        self.irq = 0
        self.irq_mask = 0
        self.packet_type = 0
        self.frequency = 0
        self.frame = bytearray()
        self.commands = 0
        self.unknown: set[int] = set()

    # -- the bus ------------------------------------------------------------

    @property
    def status(self) -> int:
        return ((self.mode & 0x7) << 4) | ((COMMAND_OK & 0x7) << 1)

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        reply = bytearray()
        for byte in data:
            reply.append(self._reply_at(len(self.frame)))
            self.frame.append(byte)
        return bytes(reply) if duplex else None

    def on_deselect(self) -> None:
        if self.frame:
            self._apply(bytes(self.frame))
        self.frame = bytearray()

    def pump(self) -> None:
        """Take anything off the air. Called between the guest's accesses, so
        a frame lands without a second thread touching this model."""
        if self.air is None:
            return
        for frame in self.air.poll():
            if self.mode != MODE_RX:
                continue        # not listening; a real part hears nothing
            payload = frame[:BUFFER_SIZE]
            for offset, value in enumerate(payload):
                self.buffer[(self.rx_base + offset) % BUFFER_SIZE] = value
            self.received_length = len(payload)
            self.heard += 1
            self._raise(IRQ_RX_DONE)

    def _raise(self, flag: int) -> None:
        """Set a flag and put the line where the flags say it should be.

        A host that configured an interrupt waits on the line rather than
        polling, and never looks at the flags until the line moves.
        """
        self.irq |= flag
        self._drive_line()

    def _drive_line(self) -> None:
        if self.signal is not None:
            self.signal(bool(self.irq & self.irq_mask))

    # -- what the host reads while it clocks --------------------------------

    def _reply_at(self, index: int) -> int:
        """The byte going out while byte `index` of this window comes in.

        The part answers with its status throughout, except where the host is
        clocking out something it asked for, and then it answers with that.
        """
        if index == 0:
            return self.status
        opcode = self.frame[0]

        if opcode == READ_REGISTER and index >= DATA_INDEX:
            # opcode, two address bytes, one of padding, then the contents
            address = (self.frame[1] << 8) | self.frame[2]
            return self._register(address + index - DATA_INDEX)
        if opcode == READ_BUFFER and index >= 3:
            return self.buffer[(self.frame[1] + index - 3) % BUFFER_SIZE]
        if opcode == GET_IRQ_STATUS:
            return (self.status, self.status, (self.irq >> 8) & 0xFF,
                    self.irq & 0xFF)[min(index, 3)]
        if opcode == GET_RX_BUFFER_STATUS:
            # status, then how many bytes arrived and where they start.
            return (self.status, self.status, self.received_length,
                    self.rx_base)[min(index, 3)]
        if opcode == GET_PACKET_STATUS:
            # No signal: the strengths a host reads are the floor. They are
            # reported in negative half-decibels, so a large number is quiet.
            return (self.status, self.status, 0xFF, 0x80, 0xFF)[min(index, 4)]
        if opcode == GET_DEVICE_ERRORS:
            return (self.status, self.status, 0x00, 0x00)[min(index, 3)]
        if opcode == GET_PACKET_TYPE:
            return (self.status, self.status, self.packet_type)[min(index, 2)]
        return self.status

    def _transmit(self) -> None:
        """Put the packet the host wrote into the buffer on the air."""
        if self.air is None or not self.payload_length:
            return
        payload = bytes(self.buffer[(self.tx_base + offset) % BUFFER_SIZE]
                        for offset in range(self.payload_length))
        self.air.send(payload)
        self.sent += 1
        log.debug("sx1262: transmitted %d bytes", len(payload))

    def _register(self, address: int) -> int:
        return self.registers[address] if address < REGISTER_SPACE else 0

    # -- what the command does ----------------------------------------------

    def _apply(self, frame: bytes) -> None:
        opcode = frame[0]
        self.commands += 1
        log.debug("sx1262: %s", frame[:24].hex())

        if opcode == WRITE_REGISTER and len(frame) >= 3:
            address = (frame[1] << 8) | frame[2]
            for offset, value in enumerate(frame[3:]):
                if address + offset < REGISTER_SPACE:
                    self.registers[address + offset] = value
        elif opcode == WRITE_BUFFER and len(frame) >= 2:
            for offset, value in enumerate(frame[2:]):
                self.buffer[(frame[1] + offset) % BUFFER_SIZE] = value
        elif opcode == SET_STANDBY:
            self.mode = MODE_STANDBY_XOSC if len(frame) > 1 and frame[1] else MODE_STANDBY_RC
        elif opcode == SET_SLEEP:
            self.mode = MODE_STANDBY_RC
        elif opcode == SET_FS:
            self.mode = MODE_FS
        elif opcode == SET_TX:
            self.mode = MODE_TX
            self._transmit()
            # The host waits for the flag that says the transmission finished,
            # and blocks for its whole timeout if it never arrives.
            self._raise(IRQ_TX_DONE)
        elif opcode == SET_RX:
            self.mode = MODE_RX
        elif opcode == CLEAR_IRQ_STATUS and len(frame) >= 3:
            self.irq &= ~((frame[1] << 8) | frame[2]) & 0xFFFF
            # The line follows the flags, so clearing them lowers it and the
            # next event is an edge rather than nothing.
            self._drive_line()
        elif opcode == SET_DIO_IRQ_PARAMS and len(frame) >= 3:
            self.irq_mask = (frame[1] << 8) | frame[2]
            self._drive_line()
        elif opcode == SET_PACKET_TYPE and len(frame) >= 2:
            self.packet_type = frame[1]
        elif opcode == SET_RF_FREQUENCY and len(frame) >= 5:
            self.frequency = int.from_bytes(frame[1:5], "big")
        elif opcode == SET_BUFFER_BASE and len(frame) >= 3:
            self.tx_base, self.rx_base = frame[1], frame[2]
        elif opcode == SET_PACKET_PARAMS and len(frame) >= 5:
            # For this modem the fourth parameter is how long the packet is:
            # preamble takes two, then the header type, then the length.
            self.payload_length = frame[4]
        elif opcode in (GET_STATUS, GET_IRQ_STATUS, GET_RX_BUFFER_STATUS,
                        GET_PACKET_STATUS, GET_DEVICE_ERRORS, GET_PACKET_TYPE,
                        READ_REGISTER, READ_BUFFER):
            pass                                # answered while it was clocked
        elif opcode in (CALIBRATE, CALIBRATE_IMAGE, SET_PA_CONFIG,
                        SET_REGULATOR_MODE, SET_DIO2_AS_RF_SWITCH,
                        SET_DIO3_AS_TCXO, SET_TX_PARAMS, SET_MODULATION_PARAMS,
                        SET_PACKET_PARAMS, SET_BUFFER_BASE, SET_RX_TX_FALLBACK,
                        SET_LORA_SYMB_TIMEOUT, STOP_TIMER_ON_PREAMBLE,
                        SET_RX_DUTY_CYCLE, CLEAR_DEVICE_ERRORS):
            pass                                # accepted; nothing to model yet
        elif opcode != 0x00:                    # a lone zero is the host idling
            if opcode not in self.unknown:
                self.unknown.add(opcode)
                log.debug("sx1262: opcode 0x%02x not modelled (%s)",
                          opcode, frame[:8].hex())

    def __repr__(self) -> str:
        return (f"<{self.name} mode={self.mode} commands={self.commands} "
                f"frequency={self.frequency} sent={self.sent} "
                f"heard={self.heard}>")
