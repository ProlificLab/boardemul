#!/usr/bin/env python3
"""The T-Deck's two input devices, both on I2C0.

The keyboard is not a matrix like the T-Deck Max's: it is a second
microcontroller running LilyGO's own firmware, and it answers a read with the
ASCII code of whatever was pressed since the last one. The touch controller is
a GT911, which unlike the CST328 next door addresses its registers with two
bytes rather than one.

Both latch their reply when the register is chosen rather than building it when
the bytes are asked for. One logical read arrives here as several calls, and
regenerating the answer part-way through is the bug that has cost this project
more time than any other (ADR 0012).
"""

from __future__ import annotations

import logging

from emu.peripherals.i2c import I2CDevice

log = logging.getLogger("i2c.tdeck")

# GT911 register space. The controller numbers its registers from 0x8000; only
# the window the driver actually touches is kept.
GT911_BASE = 0x8000
GT911_SIZE = 0x0200

GT911_PRODUCT_ID = 0x8140       # "911" as four bytes, how the driver identifies it
GT911_MODULE_SWITCH1 = 0x804D   # low two bits: how the INT line signals a report
GT911_STATUS = 0x814E           # bit 7 says a report is ready, low nibble counts points
GT911_POINT1 = 0x814F           # eight bytes per point

STATUS_READY = 0x80


class GT911(I2CDevice):
    """Capacitive touch, 320x240 landscape on this board."""

    address = 0x5D
    name = "GT911"

    def __init__(self, address: int = 0x5D, width: int = 320, height: int = 240):
        self.address = address
        self.width = width
        self.height = height
        self.regs = bytearray(GT911_SIZE)
        self.regs[GT911_PRODUCT_ID - GT911_BASE:GT911_PRODUCT_ID - GT911_BASE + 4] = b"911\x00"
        # Firmware version, and the resolution in both places a driver looks
        # for it. The config block at 0x8048 is the one the datasheet leads
        # with, but the vendor's driver reads the product-information block and
        # printed "Resolution : X = 0 Y = 0" until this was filled in too.
        self.regs[0x8144 - GT911_BASE] = 0x60
        self.regs[0x8145 - GT911_BASE] = 0x10
        self._set16(0x8146, width)
        self._set16(0x8148, height)
        self._set16(0x8048, width)
        self._set16(0x804A, height)
        # How many points can be reported. One, because that is what this model
        # tracks; the real part defaults to five.
        self.regs[0x804C - GT911_BASE] = 1

        self.point: tuple[int, int] | None = None
        # The INT trigger mode the firmware wrote, or None until it writes one.
        # It selects which of two drivers' readings of the line the board
        # follows (ADR 0029).
        self.int_mode: int | None = None
        self.ptr = GT911_PRODUCT_ID
        self._pending = bytearray()
        self._out = b""
        self._pos = 0
        self.reports = 0
        self.acks = 0

    def _set16(self, reg: int, value: int) -> None:
        self.regs[reg - GT911_BASE] = value & 0xFF
        self.regs[reg - GT911_BASE + 1] = (value >> 8) & 0xFF

    # -- what the host does -------------------------------------------------

    def touch(self, x: int, y: int) -> None:
        self.point = (max(0, min(x, self.width - 1)),
                      max(0, min(y, self.height - 1)))

    def release(self) -> None:
        self.point = None

    @property
    def int_level(self) -> int:
        """The level the INT line carries now (ADR 0029).

        With no mode written the line rests low and stays low: the driver seen
        that writes none reads the status register only while it is low. Mode
        0 (rising edge) and 3 (high level) are the vendor's convention, high
        while a finger is down; modes 1 and 2 are the same driver's table for
        the falling and low-level settings, and have not been seen on a wire.
        """
        if self.int_mode is None:
            return 0
        pressed = self.point is not None
        if self.int_mode in (0, 3):
            return 1 if pressed else 0
        return 0 if pressed else 1

    # -- the bus ------------------------------------------------------------

    def start(self, reading: bool) -> None:
        # Deliberately does not re-latch on a read: the reply was built when the
        # register was chosen, and a read simply continues through it.
        if not reading:
            self._pending.clear()

    def write(self, data: bytes) -> bool:
        self._pending += data
        if len(self._pending) < 2:
            return True
        self.ptr = (self._pending[0] << 8) | self._pending[1]
        payload = bytes(self._pending[2:])
        self._pending.clear()

        for i, b in enumerate(payload):
            off = self.ptr - GT911_BASE + i
            if 0 <= off < GT911_SIZE:
                self.regs[off] = b
            if self.ptr + i == GT911_MODULE_SWITCH1:
                self.int_mode = b & 0x03
        if payload and self.ptr == GT911_STATUS and payload[0] == 0:
            # The driver clears the ready flag once it has taken the report.
            self.acks += 1

        self._latch()
        return True

    def read(self, count: int) -> bytes:
        out = self._out[self._pos:self._pos + count]
        self._pos += len(out)
        if len(out) < count:
            out += bytes(count - len(out))
        return out

    def _latch(self) -> None:
        """Freeze the answer at the moment the register was chosen."""
        if self.point is None:
            self.regs[GT911_STATUS - GT911_BASE] = STATUS_READY
        else:
            self.regs[GT911_STATUS - GT911_BASE] = STATUS_READY | 1
            x, y = self.point
            point = bytes([0,                       # track id
                           x & 0xFF, (x >> 8) & 0xFF,
                           y & 0xFF, (y >> 8) & 0xFF,
                           0x20, 0x00,              # contact size
                           0])
            off = GT911_POINT1 - GT911_BASE
            self.regs[off:off + len(point)] = point
            if self.ptr == GT911_STATUS:
                self.reports += 1

        start = self.ptr - GT911_BASE
        self._out = bytes(self.regs[start:]) if 0 <= start < GT911_SIZE else b""
        self._pos = 0

    def __repr__(self) -> str:
        return f"<{self.name}@0x{self.address:02x} reports={self.reports}>"


# The keyboard reports characters, not scan codes, so the emulator's key names
# have to become the character the firmware would have received.
KEY_CHARS = {"SPACE": " ", "ENTER": "\r", "DEL": "\b"}

# Controls the board really has which send no character of their own.
#
# ALT and SYM are modifiers resolved inside the keyboard's own microcontroller:
# pressing one changes what the next key sends rather than reporting anything
# itself. The alternate layer they select is deliberately not mapped -- it has
# never been seen on the wire here, and ADR 0006 is that behaviour is learned
# from the wire rather than guessed at from a keyboard that looks similar.
#
# The trackball is not on this bus at all. Its four directions are ordinary
# GPIOs, so a direction pressed on the drawn device has nowhere to go until
# that is modelled.
SILENT_KEYS = {"ALT", "SYM", "UP", "DOWN", "LEFT", "RIGHT"}


class TDeckKeyboard(I2CDevice):
    """LilyGO's keyboard co-processor: one read, one character, zero if idle."""

    address = 0x55
    name = "T-Deck keyboard"

    def __init__(self, address: int = 0x55):
        self.address = address
        self.pending: list[int] = []
        self._out = b""
        self._pos = 0
        self.reads = 0
        self.delivered = 0

    def key_event(self, name: str, down: bool) -> bool:
        """Queue a character, and say whether the key is one this board has.

        Only the press matters: the real keyboard reports on the way down and
        says nothing on release. A control in SILENT_KEYS is accepted and
        queues nothing, which is what the hardware does; an unrecognised name
        is refused rather than turned into an invented keystroke.
        """
        if not down:
            return False
        if name in SILENT_KEYS:
            return True
        char = KEY_CHARS.get(name)
        if char is None:
            if len(name) != 1:
                return False
            char = name.lower()
        self.pending.append(ord(char))
        return True

    def start(self, reading: bool) -> None:
        if reading:
            # Latched here, not in read(): a split read must not consume two
            # keystrokes to satisfy one request.
            value = self.pending.pop(0) if self.pending else 0
            if value:
                self.delivered += 1
            self._out = bytes([value])
            self._pos = 0
            self.reads += 1

    def read(self, count: int) -> bytes:
        out = self._out[self._pos:self._pos + count]
        self._pos += len(out)
        if len(out) < count:
            out += bytes(count - len(out))
        return out

    def __repr__(self) -> str:
        return (f"<{self.name}@0x{self.address:02x} "
                f"reads={self.reads} delivered={self.delivered}>")
