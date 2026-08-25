#!/usr/bin/env python3
"""I2C devices on the T-Deck Max.

Addresses come from the vendor's own lib/TDeckMaxBoard/src/TDeckMaxBoard.h, not
from datasheet guesswork.

Most of these follow the same shape -- address a register, then read or write
from it, auto-incrementing -- so `RegisterFile` covers them and only the parts
the firmware actually inspects get specialised.
"""

from __future__ import annotations

import logging

from emu.peripherals.i2c import I2CDevice

log = logging.getLogger("i2c.dev")

# The real part's key-event FIFO is ten deep.
FIFO_DEPTH = 10


class RegisterFile(I2CDevice):
    """The common register-pointer device.

    A write sets the pointer from its first byte and stores the rest; a read
    returns bytes from the pointer onward. Devices that need more than this
    override `on_write`.
    """

    def __init__(self, address: int, name: str, size: int = 256,
                 defaults: dict[int, int] | None = None):
        self.address = address
        self.name = name
        self.regs = bytearray(size)
        self.defaults = defaults or {}
        for reg, val in self.defaults.items():
            self.regs[reg] = val
        self.ptr = 0
        self._expect_pointer = False
        self.writes = 0
        self.reads = 0

    def start(self, reading: bool) -> None:
        # A repeated START for the read half of a register read must not
        # disturb the pointer the write half just set.
        if not reading:
            self._expect_pointer = True

    def write(self, data: bytes) -> bool:
        if not data:
            return True
        if self._expect_pointer:
            self.ptr = data[0] % len(self.regs)
            data = data[1:]
            self._expect_pointer = False
        for b in data:
            self.regs[self.ptr] = b
            self.on_write(self.ptr, b)
            self.ptr = (self.ptr + 1) % len(self.regs)
        self.writes += 1
        return True

    def read(self, count: int) -> bytes:
        out = bytes(self.regs[(self.ptr + i) % len(self.regs)] for i in range(count))
        self.ptr = (self.ptr + count) % len(self.regs)
        self.reads += 1
        return out

    def on_write(self, reg: int, value: int) -> None:
        """Hook for devices with side effects."""

    def __repr__(self) -> str:
        return f"<{self.name}@0x{self.address:02x} r={self.reads} w={self.writes}>"


class XL9555(RegisterFile):
    """16-bit I/O expander, TCA9555 register layout.

    Inputs read back as whatever the outputs were last driven to, which is what
    a board with no external stimulus behaves like and keeps firmware that
    reads back its own writes happy.
    """

    IN0, IN1, OUT0, OUT1, POL0, POL1, CFG0, CFG1 = range(8)

    def __init__(self, address: int = 0x20):
        super().__init__(address, "XL9555", size=8,
                         defaults={self.OUT0: 0xFF, self.OUT1: 0xFF,
                                   self.CFG0: 0xFF, self.CFG1: 0xFF})

    def on_write(self, reg: int, value: int) -> None:
        if reg == self.OUT0:
            self.regs[self.IN0] = value
        elif reg == self.OUT1:
            self.regs[self.IN1] = value


class TCA8418(RegisterFile):
    """Keyboard matrix controller.

    The firmware never polls this: it configures the part and then waits on a
    GPIO interrupt (ADR 0010). So a key press is two things -- an entry in the
    event FIFO here, and a falling edge on the controller's INT line, which the
    board wiring raises separately.
    """

    CFG = 0x01
    INT_STAT = 0x02
    KEY_LCK_EC = 0x03
    KEY_EVENT_A = 0x04

    COLS = 10

    # Physical layout of the T-Deck Max keyboard, row by row. A key's position
    # in this grid is what the controller reports; the character is only how the
    # host names it.
    # Verbatim from examples/factory/peri_keypad.cpp. Note row 2 starts with
    # ALT and ends with ENTER, and row 3 has only five wired keys -- the
    # first five positions are not populated and the firmware drops them.
    LAYOUT = (
        ("Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"),
        ("A", "S", "D", "F", "G", "H", "J", "K", "L", "DEL"),
        ("ALT", "Z", "X", "C", "V", "B", "N", "M", "$", "ENTER"),
        (None, None, None, None, None, "UP", "0", "SPACE", "SYM", "UP2"),
    )

    def __init__(self, address: int = 0x34):
        super().__init__(address, "TCA8418", size=64)
        self.pending: list[int] = []
        self.keymap = self._build_keymap()

    @classmethod
    def _build_keymap(cls) -> dict[str, tuple[int, int]]:
        table: dict[str, tuple[int, int]] = {}
        for row, keys in enumerate(cls.LAYOUT):
            for col, name in enumerate(keys):
                if name is not None:
                    table.setdefault(name.upper(), (row, col))
        return table

    def key_code(self, row: int, col: int) -> int:
        """The controller numbers keys from 1 across the matrix. The firmware
        recovers the column as (COLS-1) - k % COLS, so the numbering runs right
        to left within a row."""
        return row * self.COLS + (self.COLS - 1 - col) + 1

    def key_event(self, key: str, down: bool = True) -> bool:
        position = self.keymap.get(key.upper())
        if position is None:
            return False
        code = self.key_code(*position)
        self.pending.append(code | (0x80 if down else 0x00))
        return True

    def read(self, count: int) -> bytes:
        # These two registers are computed at read time rather than stored: the
        # count has to fall as the driver drains the FIFO, or it loops forever.
        if self.ptr == self.KEY_EVENT_A:
            self.regs[self.KEY_EVENT_A] = self.pending.pop(0) if self.pending else 0
        elif self.ptr == self.KEY_LCK_EC:
            self.regs[self.KEY_LCK_EC] = min(len(self.pending), FIFO_DEPTH)
        elif self.ptr == self.INT_STAT:
            self.regs[self.INT_STAT] = 0x01 if self.pending else 0x00
        return super().read(count)


class CST328(I2CDevice):
    """Capacitive touch panel, 240x320.

    Unlike the other devices here this one is addressed with a 16-bit register
    number and validates its own reports, so it cannot reuse RegisterFile.

    Two details are load-bearing. The last byte of a report must be 0xAB or the
    driver discards it, and the driver probes for a cst66xx at the same address
    first -- that probe is rejected by returning anything other than 0xCA at
    bytes 2 and 3, which is what lets it fall through to the CST328 path.
    """

    REG_REPORT = 0xD000
    REG_CHIP_INFO = 0xD1F4
    REPORT_MAGIC = 0xAB
    CONTACT = 0x06

    def __init__(self, address: int = 0x1A):
        self.address = address
        self.name = "CST328"
        self.reg = 0
        self._written = b""
        self._out = b""
        self._out_pos = 0
        self.touch: tuple[int, int] | None = None
        self.reports = 0
        self.acks = 0

    def start(self, reading: bool) -> None:
        if not reading:
            self._written = b""
            return
        # Snapshot the answer once per addressing, and serve it sequentially.
        #
        # The controller splits one logical read into two commands -- n-1 bytes
        # acknowledged, the last one not -- so a reply regenerated per call
        # hands out byte 0 where byte 6 was expected. For this device that is
        # fatal and silent: the driver checks the last byte for 0xAB, discards
        # the whole report when it does not match, and still writes its
        # acknowledgement, so the traffic looks perfectly healthy.
        self._out = self._build()
        self._out_pos = 0

    def _build(self) -> bytes:
        if self.reg == self.REG_REPORT:
            return self._report_bytes()
        if self.reg == self.REG_CHIP_INFO:
            return self._chip_info_bytes()
        return bytes(64)

    def write(self, data: bytes) -> bool:
        self._written += data
        if len(self._written) >= 2:
            self.reg = (self._written[0] << 8) | self._written[1]
        # A three-byte write of 0xAB to the report register is the driver
        # acknowledging what it just read; on real hardware that releases INT.
        if (len(self._written) == 3 and self.reg == self.REG_REPORT
                and self._written[2] == self.REPORT_MAGIC):
            self.acks += 1
        return True

    def read(self, count: int) -> bytes:
        out = self._out[self._out_pos:self._out_pos + count]
        self._out_pos += count
        return out + bytes(count - len(out))

    def _report_bytes(self) -> bytes:
        buf = bytearray(16)
        if self.touch is not None:
            x, y = self.touch
            buf[0] = self.CONTACT            # finger 0, contact event
            buf[1] = (x >> 4) & 0xFF
            buf[2] = (y >> 4) & 0xFF
            buf[3] = ((x & 0x0F) << 4) | (y & 0x0F)
            buf[4] = 0x3F                    # pressure
            buf[5] = 0x01                    # one finger, not a key event
        buf[6] = self.REPORT_MAGIC
        self.reports += 1
        return bytes(buf)

    def _chip_info_bytes(self) -> bytes:
        info = bytearray(32)
        info[4:6] = (240).to_bytes(2, "little")   # reported x resolution
        info[6:8] = (320).to_bytes(2, "little")   # reported y resolution
        info[18:20] = (0x0328).to_bytes(2, "little")
        return bytes(info)

    def __repr__(self) -> str:
        return f"<CST328@0x{self.address:02x} reports={self.reports} acks={self.acks}>"


class DRV2605(RegisterFile):
    """Haptic driver. Only the status register matters at init: bits 7:5 carry
    the device id, and the driver refuses to continue if they read back as 0."""

    def __init__(self, address: int = 0x5A):
        super().__init__(address, "DRV2605", size=0x40, defaults={0x00: 0xE0})


class SY6970(RegisterFile):
    """Charger/PMIC. Register 0x14 bit 7 is the reset bit and bits 5:3 are the
    part number; leaving them zero makes probing code think the part is absent,
    so a plausible id is supplied."""

    def __init__(self, address: int = 0x6A):
        super().__init__(address, "SY6970", size=0x15,
                         defaults={0x0B: 0x40, 0x14: 0x30})


class BQ27220(RegisterFile):
    """Fuel gauge. Enough of a battery to look charged and healthy."""

    def __init__(self, address: int = 0x55):
        super().__init__(address, "BQ27220", size=0x80, defaults={
            0x08: 0x10, 0x09: 0x10,   # voltage  ~4112 mV
            0x2C: 0x55, 0x2D: 0x00,   # state of charge ~85 %
        })


class ES8311(RegisterFile):
    """Audio codec. Register 0xFD/0xFE hold the chip id (0x83, 0x11)."""

    def __init__(self, address: int = 0x18):
        super().__init__(address, "ES8311", size=0x100,
                         defaults={0xFD: 0x83, 0xFE: 0x11})


class BHI260AP(RegisterFile):
    """Bosch smart sensor. It expects a large firmware upload before it does
    anything useful; this models only enough to be discoverable."""

    def __init__(self, address: int = 0x28):
        super().__init__(address, "BHI260AP", size=0x100, defaults={0x2B: 0x89})


def tdeck_max_devices() -> list[I2CDevice]:
    return [
        ES8311(0x18),
        CST328(0x1A),
        XL9555(0x20),
        BHI260AP(0x28),
        TCA8418(0x34),
        BQ27220(0x55),
        DRV2605(0x5A),
        SY6970(0x6A),
    ]
