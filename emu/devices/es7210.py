#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The microphone codec the vendor firmware looks for and does not find.

`ES7210 address not found` is the firmware saying it probed 0x40 and nothing
answered. It is a four-channel audio analogue-to-digital converter, and this
models the only thing a host can learn from it without sound: that it is
there, and which part it is.

Registers are read and written the ordinary way -- write the number, then read
or write the contents -- and the two a driver checks are the identity pair near
the top of the space, which spell the part number.
"""

from __future__ import annotations

import logging

from emu.i2c_link import I2CDevice

log = logging.getLogger("i2c")

REGISTER_SPACE = 0x100

# The part number, in the two registers a driver reads to confirm what it is
# talking to: 0x72 and 0x10 spell 7210.
CHIP_ID_HIGH_REGISTER = 0xFD
CHIP_ID_LOW_REGISTER = 0xFE
CHIP_ID_HIGH = 0x72
CHIP_ID_LOW = 0x10


class Es7210(I2CDevice):
    """Answers as an ES7210 at its usual address."""

    address = 0x40
    name = "ES7210"

    def __init__(self, address: int = 0x40):
        self.address = address
        self.regs = bytearray(REGISTER_SPACE)
        self.regs[CHIP_ID_HIGH_REGISTER] = CHIP_ID_HIGH
        self.regs[CHIP_ID_LOW_REGISTER] = CHIP_ID_LOW
        self.pointer = 0
        self.reads = 0
        self.writes = 0
        self._out = b""
        self._pos = 0

    def start(self, reading: bool) -> None:
        if reading:
            # Latched when the read begins, so a split read stays coherent
            # (ADR 0006), and the pointer walks on as the host clocks bytes.
            self._out = bytes(self.regs[self.pointer:]) + bytes(self.regs[:self.pointer])
            self._pos = 0
            self.reads += 1

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        self.pointer = data[0] % REGISTER_SPACE
        for offset, value in enumerate(data[1:]):
            self.regs[(self.pointer + offset) % REGISTER_SPACE] = value
            self.writes += 1
        return True

    def read(self, count: int) -> bytes:
        out = self._out[self._pos:self._pos + count]
        self._pos += len(out)
        if len(out) < count:
            out += bytes(count - len(out))
        return out

    def __repr__(self) -> str:
        return (f"<{self.name}@0x{self.address:02x} reads={self.reads} "
                f"writes={self.writes}>")
