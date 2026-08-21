#!/usr/bin/env python3
"""GPIO output-level tracking.

The board wires the e-paper's chip-select and data/command pins to ordinary
GPIOs driven with digitalWrite, not to the SPI peripheral's native CS. Without
knowing their levels there is no way to tell a command byte from a pixel byte,
so the SPI model has to be able to ask.

Only the six output registers are claimed. Input, interrupt, strapping and the
pin matrix stay with the machine's own GPIO model, which already handles them
and which the ROM depends on at boot.
"""

from __future__ import annotations

from emu.bus import Region

GPIO_BASE = 0x60004000
OUT_WINDOW_BASE = GPIO_BASE + 0x04
OUT_WINDOW_SIZE = 0x18

# Offsets within the claimed window.
O_OUT = 0x00
O_OUT_W1TS = 0x04
O_OUT_W1TC = 0x08
O_OUT1 = 0x0C
O_OUT1_W1TS = 0x10
O_OUT1_W1TC = 0x14

MASK32 = 0xFFFFFFFF


class GpioOut(Region):
    def __init__(self, name: str = "gpio_out"):
        super().__init__(name, OUT_WINDOW_BASE, OUT_WINDOW_SIZE)
        self.level = 0  # 64 pins, bit n == pin n
        self.changes = 0

    def pin(self, number: int) -> int:
        return (self.level >> number) & 1

    def read(self, offset: int, size: int) -> int:
        if offset == O_OUT:
            return self.level & MASK32
        if offset == O_OUT1:
            return (self.level >> 32) & MASK32
        # The write-one-to-set/clear registers read back as zero on hardware.
        return 0

    def write(self, offset: int, value: int, size: int) -> None:
        value &= MASK32
        before = self.level
        if offset == O_OUT:
            self.level = (self.level & ~MASK32) | value
        elif offset == O_OUT_W1TS:
            self.level |= value
        elif offset == O_OUT_W1TC:
            self.level &= ~value
        elif offset == O_OUT1:
            self.level = (self.level & MASK32) | (value << 32)
        elif offset == O_OUT1_W1TS:
            self.level |= value << 32
        elif offset == O_OUT1_W1TC:
            self.level &= ~(value << 32)
        if self.level != before:
            self.changes += 1


class GpioIn(Region):
    """Input-level readback for the pins the firmware polls.

    The panel's BUSY line is an input to the SoC; nothing in the machine drives
    it, so it reads low forever and every wait-while-busy loop hangs. Claiming
    just the two input registers is enough to answer them.
    """

    def __init__(self, out: GpioOut, high_pins: set[int] | None = None,
                 name: str = "gpio_in"):
        super().__init__(name, GPIO_BASE + 0x3C, 0x08)
        self.out = out
        self.high = high_pins or set()

    def read(self, offset: int, size: int) -> int:
        level = self.out.level
        for pin in self.high:
            level |= 1 << pin
        return (level >> (32 if offset == 0x04 else 0)) & MASK32
