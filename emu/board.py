#!/usr/bin/env python3
"""The T-Deck Max as a set of intercepted MMIO windows.

Only windows the fork does not already model belong here. Anything QEMU handles
correctly -- UART, timers, eFuse, SHA -- is deliberately left alone (ADR 0005
decision 4).
"""

from __future__ import annotations

import os
from pathlib import Path

from emu.bus import Region
from emu.devices.epaper import EPaperPanel
from emu.peripherals.gpio import GpioIn, GpioOut
from emu.peripherals.spi import SPIController
from emu.devices.i2c_devices import tdeck_max_devices
from emu.peripherals.i2c import I2CController, I2CDevice

# ESP32-S3 peripheral bases, from components/soc/esp32s3/include/soc/reg_base.h
I2C0_BASE = 0x60013000
I2C1_BASE = 0x60027000
SPI0_BASE = 0x60003000
SPI2_BASE = 0x60024000
SPI3_BASE = 0x60025000

# Interrupt source numbers, from periph_defs.h for esp32s3.
IRQ_I2C_EXT0 = 42
IRQ_I2C_EXT1 = 43
IRQ_SPI2 = 21

EPD_BUSY_PIN = 37

# Peripheral windows the fork leaves to its catch-all region. Claiming them
# changes no behaviour on its own -- reads still return what was last written --
# but it makes the traffic visible, which is how you find out what a stalled or
# crashing firmware was reaching for.
UNMODELLED = {
    "ledc": 0x60019000,
    "i2s0": 0x6002D000,
    "lcd_cam": 0x60041000,
    "uhci": 0x60014000,
    "pcnt": 0x60017000,
}


def i2c_devices() -> list[I2CDevice]:
    """Addresses are the vendor's own, from lib/TDeckMaxBoard/src/TDeckMaxBoard.h.

    BOARDEMUL_I2C restricts the set to a comma-separated list of hex addresses,
    which is how you find out which device's answers a driver actually chokes on.
    """
    devices = tdeck_max_devices()
    only = os.environ.get("BOARDEMUL_I2C")
    if only:
        wanted = {int(a, 16) for a in only.replace(" ", "").split(",") if a}
        devices = [d for d in devices if d.address in wanted]
    return devices


class TraceRegion(Region):
    """Claims a window purely to find out whether the firmware touches it.

    Reads return zero, which is what the machine's catch-all already did, so
    adding one of these changes nothing except visibility.
    """

    def __init__(self, name: str, base: int, size: int = 0x1000):
        super().__init__(name, base, size)
        self.regs: dict[int, int] = {}

    def read(self, offset: int, size: int) -> int:
        return self.regs.get(offset, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        self.regs[offset] = value


def build_regions(outdir: Path) -> list[Region]:
    gpio = GpioOut()
    panel = EPaperPanel(gpio, Path(outdir) / "frames",
                        quarter_turns=int(os.environ.get("BOARDEMUL_ROTATE", "0")))
    # BUSY idles high on this controller; left undriven it reads low and every
    # wait-while-busy loop in the driver spins forever.
    gpio_in = GpioIn(gpio, high_pins={EPD_BUSY_PIN})
    return [
        I2CController("i2c0", I2C0_BASE, i2c_devices(), irq=IRQ_I2C_EXT0),
        I2CController("i2c1", I2C1_BASE, [], irq=IRQ_I2C_EXT1),
        gpio,
        gpio_in,
        # SPI2 carries both the panel and the LoRa radio, distinguished by
        # chip-select. Only the panel is modelled so far, and it checks its own
        # CS, so radio traffic is simply ignored rather than misread.
        SPIController("spi2", SPI2_BASE, [panel]),
        TraceRegion("spi0", SPI0_BASE),
        TraceRegion("spi3", SPI3_BASE),
    ] + [TraceRegion(n, b) for n, b in UNMODELLED.items()]
