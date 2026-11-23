#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
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
# What the part on this board holds there. Neither firmware writes this
# register -- the vendor's driver reads it and only logs what it says -- so
# the model has to state it, and an interrupt that asserts low is both the
# usual arrangement and the one both drivers work with (ADR 0031).
DEFAULT_INT_MODE = 2            # low level
GT911_STATUS = 0x814E           # bit 7 says a report is ready, low nibble counts points
GT911_POINT1 = 0x814F           # eight bytes per point

STATUS_READY = 0x80


class GT911(I2CDevice):
    """Capacitive touch, reporting in its own raster.

    That raster is portrait, 240x320, the same way round as the ST7789 behind
    it: both parts are mounted turned a quarter turn from the landscape
    interface the firmware draws. The controller knows nothing of that turn and
    reports where the finger is on its own glass, so a board that wants a
    display coordinate touched has to state the mounting (ADR 0030).
    """

    address = 0x5D
    name = "GT911"

    def __init__(self, address: int = 0x5D, width: int = 240, height: int = 320):
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
        # How the INT line signals. The part on this board is configured
        # active low, and says so in this register; a firmware may write its
        # own mode and is then followed (ADR 0031).
        self.int_mode: int = DEFAULT_INT_MODE
        self.regs[GT911_MODULE_SWITCH1 - GT911_BASE] = DEFAULT_INT_MODE
        # An interrupt reports a change, so a release has to be delivered too:
        # the line stays asserted until the host has taken a report with no
        # finger in it, and only then goes idle.
        self._release_unread = False
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
        if self.point is not None:
            self._release_unread = True
        self.point = None

    @property
    def int_level(self) -> int:
        """The level the INT line carries now (ADR 0031).

        Asserted while the controller has something to say -- a finger, or a
        release the host has not taken yet -- and idle otherwise, so both a
        press and a lift produce an edge. Modes 1 and 2 assert low, which is
        this board's arrangement and the usual one for an interrupt with a
        pull-up on it; modes 0 and 3 assert high.
        """
        asserted = self.point is not None or self._release_unread
        if self.int_mode in (0, 3):
            return 1 if asserted else 0
        return 0 if asserted else 1

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
            # The host is taking the empty report, which is the release.
            if self.ptr == GT911_STATUS:
                self._release_unread = False
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




# [kbd] examples/Keyboard_ESP32C3 (ADR 0127): every key's column and row in
# the keyboard's matrix, its character, and its symbol-layer character, from
# the sketch's two tables. None where the sketch has NULL.
MATRIX = {
    "Q": (0, 0, "q", "#"), "W": (0, 1, "w", "1"), "SYM": (0, 2, None, None),
    "A": (0, 3, "a", "*"), "ALT": (0, 4, None, None), "SPACE": (0, 5, " ", None),
    "MIC": (0, 6, None, "0"),
    "E": (1, 0, "e", "2"), "S": (1, 1, "s", "4"), "D": (1, 2, "d", "5"),
    "P": (1, 3, "p", "@"), "X": (1, 4, "x", "8"), "Z": (1, 5, "z", "7"),
    "LSHIFT": (1, 6, None, None),
    "R": (2, 0, "r", "3"), "G": (2, 1, "g", "/"), "T": (2, 2, "t", "("),
    "RSHIFT": (2, 3, None, None), "V": (2, 4, "v", "?"), "C": (2, 5, "c", "9"),
    "F": (2, 6, "f", "6"),
    "U": (3, 0, "u", "_"), "H": (3, 1, "h", ":"), "Y": (3, 2, "y", ")"),
    "ENTER": (3, 3, None, None), "B": (3, 4, "b", "!"), "N": (3, 5, "n", ","),
    "J": (3, 6, "j", ";"),
    "O": (4, 0, "o", "+"), "L": (4, 1, "l", '"'), "I": (4, 2, "i", "-"),
    "DEL": (4, 3, None, None), "$": (4, 4, "$", None), "M": (4, 5, "m", "."),
    "K": (4, 6, "k", "'"),
}
COLUMNS = 5
MODIFIERS = ("ALT", "SYM", "LSHIFT", "RSHIFT")

# The trackball is not on this bus at all. Its four directions are ordinary
# GPIOs, which the board drives (ADR 0128); this keyboard accepts their names
# and sends nothing for them.
TRACKBALL_KEYS = {"UP", "DOWN", "LEFT", "RIGHT"}

# [kbd] the commands the sketch takes, and its two backlight duties.
CMD_BRIGHTNESS = 0x01
CMD_ALT_B_BRIGHTNESS = 0x02
CMD_MODE_RAW = 0x03
CMD_MODE_KEY = 0x04
BOOT_DUTY = 0
ALT_B_DEFAULT_DUTY = 127


def _chords() -> dict[str, tuple[str, ...]]:
    """Each character the keyboard can type, and the keys that type it: the
    plain layer first, then the symbol layer, then capitals, so a character
    with more than one way gets the simplest (ADR 0127)."""
    chords: dict[str, tuple[str, ...]] = {"\r": ("ENTER",), "\n": ("ENTER",),
                                          "\b": ("DEL",)}
    for name, (_c, _r, char, _s) in MATRIX.items():
        if char:
            chords.setdefault(char, (name,))
    for name, (_c, _r, _ch, symbol) in MATRIX.items():
        if symbol:
            chords.setdefault(symbol, ("SYM", name))
    for name, (_c, _r, char, _s) in MATRIX.items():
        if char and char.isalpha():
            chords.setdefault(char.upper(), ("LSHIFT", name))
    return chords


CHORDS = _chords()


class TDeckKeyboard(I2CDevice):
    """LilyGO's keyboard co-processor, as its sketch has it (ADR 0127).

    Key mode: one read, one byte, zero if nothing was pressed. Raw mode: five
    bytes, one per matrix column. The backlight is the sketch's PWM duty.
    """

    address = 0x55
    name = "T-Deck keyboard"

    def __init__(self, address: int = 0x55):
        self.address = address
        self.held: set[str] = set()
        self.pending: list[int] = []
        self.raw = False
        # Pressed since the last raw read, so a tap between two reads is seen.
        self._tapped: set[str] = set()
        self.duty = BOOT_DUTY               # kb_brightness_duty
        self.alt_b_duty = ALT_B_DEFAULT_DUTY  # kb_brightness_setting_duty
        self.lit = False                    # BL_state
        self.pwm = BOOT_DUTY                # what ledcWrite last set
        self._written = bytearray()
        self._out = b""
        self._pos = 0
        self.reads = 0
        self.delivered = 0

    # -- what the host does -------------------------------------------------

    def key_event(self, name: str, down: bool) -> bool:
        """Press or release a key, and say whether the board has it.

        A press of a key already held counts as a new press: a finger cannot
        press a held key without lifting it, and drivers that send only
        presses rely on that. An unknown name is refused rather than turned
        into an invented keystroke.
        """
        if name in TRACKBALL_KEYS:
            return True
        if name not in MATRIX:
            return False
        if not down:
            self.held.discard(name)
            return True
        self.held.add(name)
        self._tapped.add(name)
        value = self._reply(name)
        if value:
            if self.raw:
                self.pending = [value]
            else:
                self.pending.append(value)
        return True

    def type_text(self, text: str) -> int:
        """Type each character as the chord the device needs, and say how
        many could be typed. One the keyboard has no way to type is skipped."""
        typed = 0
        for char in text:
            chord = CHORDS.get(char)
            if chord is None:
                log.info("the T-Deck keyboard cannot type %r", char)
                continue
            for key in chord:
                self.key_event(key, True)
            for key in reversed(chord):
                self.key_event(key, False)
            typed += 1
        return typed

    def _reply(self, name: str) -> int:
        """The byte the sketch leaves to send after this press, in its order:
        printMatrix(), then loop()'s Enter, backspace, Alt+B and Alt+C."""
        _col, _row, char, symbol = MATRIX[name]
        value = 0
        if char is not None or symbol is not None:
            c = symbol if "SYM" in self.held else char
            value = ord(c) if c else 0
            if "LSHIFT" in self.held or "RSHIFT" in self.held:
                # (char)((int)toPrint - 32), and char is unsigned on the C3.
                value = (value - 32) & 0xFF
        if name == "ENTER":
            value = 0x0D
        if name == "DEL":
            value = 0x08
        if name == "B" and "ALT" in self.held:
            self._alt_b()
            value = 0
        if name == "C" and "ALT" in self.held:
            value = 0x0C
        return value

    def _alt_b(self) -> None:
        if self.lit:
            self.lit = False
            self.pwm = 0
        else:
            self.lit = True
            self.pwm = self.alt_b_duty if self.duty == 0 else self.duty

    def columns(self) -> bytes:
        """The raw-mode reply: per column, bit r set for a key held in row r."""
        out = bytearray(COLUMNS)
        for name in self.held | self._tapped:
            col, row, _c, _s = MATRIX[name]
            out[col] |= 1 << row
        return bytes(out)

    # -- the bus ------------------------------------------------------------

    def start(self, reading: bool) -> None:
        self._take_commands()
        if reading:
            # Latched here, not in read(): a split read must not consume two
            # keystrokes to satisfy one request.
            if self.raw:
                self._out = self.columns()
                self._tapped.clear()
            else:
                value = self.pending.pop(0) if self.pending else 0
                if value:
                    self.delivered += 1
                self._out = bytes([value])
            self._pos = 0
            self.reads += 1

    def write(self, data: bytes) -> bool:
        self._written += data
        return True

    def stop(self) -> None:
        self._take_commands()

    def _take_commands(self) -> None:
        """onReceive(): the bytes of a finished write, as commands. 0x01 has
        no break and runs on into 0x02, which reads one more byte; a read
        past the end is -1, as Wire.read() returns."""
        data, self._written = bytes(self._written), bytearray()
        i = 0

        def next_byte() -> int:
            nonlocal i
            if i >= len(data):
                return -1
            i += 1
            return data[i - 1]

        while i < len(data):
            cmd = next_byte()
            if cmd == CMD_BRIGHTNESS:
                duty = next_byte()
                if duty >= 0:
                    self.duty = duty
                    self.pwm = duty
                    self.lit = duty > 0
            if cmd in (CMD_BRIGHTNESS, CMD_ALT_B_BRIGHTNESS):
                duty = next_byte()
                if duty > 30:
                    self.alt_b_duty = duty
            elif cmd == CMD_MODE_RAW:
                self.raw = True
            elif cmd == CMD_MODE_KEY:
                self.raw = False

    def read(self, count: int) -> bytes:
        out = self._out[self._pos:self._pos + count]
        self._pos += len(out)
        if len(out) < count:
            out += bytes(count - len(out))
        return out

    def __repr__(self) -> str:
        return (f"<{self.name}@0x{self.address:02x} "
                f"reads={self.reads} delivered={self.delivered}>")
