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
    """Keyboard matrix controller. The key event FIFO at KEY_EVENT_A reads 0,
    meaning 'no event', until something is pushed into it."""

    CFG = 0x01
    INT_STAT = 0x02
    KEY_LCK_EC = 0x03
    KEY_EVENT_A = 0x04

    def __init__(self, address: int = 0x34):
        super().__init__(address, "TCA8418", size=64)
        self.pending: list[int] = []

    def press(self, keycode: int, down: bool = True) -> None:
        self.pending.append((keycode & 0x7F) | (0x80 if down else 0))

    def read(self, count: int) -> bytes:
        if self.ptr == self.KEY_EVENT_A and self.pending:
            self.regs[self.KEY_EVENT_A] = self.pending.pop(0)
        elif self.ptr == self.KEY_LCK_EC:
            self.regs[self.KEY_LCK_EC] = len(self.pending)
        return super().read(count)


class CST328(RegisterFile):
    """Capacitive touch controller. Reports no touch; a coordinate can be
    injected by setting `self.touch`."""

    def __init__(self, address: int = 0x1A):
        super().__init__(address, "CST328", size=0x100)
        self.touch: tuple[int, int] | None = None


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
