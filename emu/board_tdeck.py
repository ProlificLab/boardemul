#!/usr/bin/env python3
"""The original LilyGO T-Deck: ESP32-S3, ST7789 LCD, GT911 touch.

Second board, and deliberately minimal. Nothing here is modelled yet: the point
is to measure what the firmware does before deciding what to build, the way the
first board went. In particular it answers the question ADR 0011 left open --
whether a DMA-driven display puts its pixels anywhere we can see.

Pins are the vendor's own, from examples/UnitTest/utilities.h.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from emu.board import TraceRegion, Board  # shared with the first board
from emu.bus import Region
from emu.i2c_link import I2CServer
from emu.peripherals.gpio import GpioClient
from emu.spi_link import SpiDevice, SpiServer

log = logging.getLogger("tdeck")

GPIO_BASE = 0x60004000
I2C0_BASE = 0x60013000
I2C1_BASE = 0x60027000
SPI2_BASE = 0x60024000

IRQ_GPIO = 16
IRQ_I2C_EXT0 = 42
IRQ_I2C_EXT1 = 43

# examples/UnitTest/utilities.h
POWERON_PIN = 10
I2C_SDA, I2C_SCL = 18, 8
TFT_CS_PIN = 12
TFT_DC_PIN = 11
TFT_BACKLIGHT = 42
TOUCH_INT_PIN = 16
KEYBOARD_INT_PIN = 46
SD_CS_PIN = 39
RADIO_CS_PIN = 9

# ADR 0013. This board's vendor firmware runs a float in a C++ global
# constructor, before FreeRTOS exists to own the FPU state, and IDF panics.
# Leaving CP0 enabled out of reset is a divergence from the architectural reset
# value that we could not confirm against silicon, so it is stated here per
# board rather than applied to every board quietly.
SOC_DEVICES = ["esp-coproc-enable,mask=0x1"]

UNMODELLED = {
    "ledc": 0x60019000,
    "i2s0": 0x6002D000,
    "lcd_cam": 0x60041000,
    "uhci": 0x60014000,
    "pcnt": 0x60017000,
    "spi0": 0x60003000,
    "spi3": 0x60025000,
}


class SpiProbe(SpiDevice):
    """Counts what arrives without pretending to be anything.

    A display driven by the CPU sends every pixel through the SPI data
    register, so the byte count here is the whole framebuffer. One driven by
    DMA sends almost nothing -- the engine reads guest RAM directly and the
    pixels never cross this boundary at all. Which of those is true decides
    whether the architecture needs a path to guest memory.
    """

    name = "spi-probe"

    def __init__(self) -> None:
        self.commands: list[int] = []
        self.command_bytes = 0
        self.data_bytes = 0
        self.segments = 0
        self.largest = 0

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        self.segments += 1
        self.largest = max(self.largest, len(data))
        if dc == 0:
            self.command_bytes += len(data)
            for b in data:
                if len(self.commands) < 400:
                    self.commands.append(b)
        else:
            self.data_bytes += len(data)
        return bytes(len(data)) if duplex else None

    def summary(self) -> str:
        seen = ", ".join(f"0x{c:02x}" for c in dict.fromkeys(self.commands))
        return (f"{self.segments} segment(s), {self.command_bytes} command byte(s), "
                f"{self.data_bytes} data byte(s), largest {self.largest}\n"
                f"    commands seen: [{seen}]")


def build(outdir: Path) -> Board:
    outdir = Path(outdir)
    probe = SpiProbe()
    gpio = GpioClient(outdir / "gpio.sock")
    spi = SpiServer(outdir / "spi2.sock", probe)
    # No devices: the controller records every address the firmware probes,
    # which is how the first board's device list was established.
    i2c0 = I2CServer(outdir / "i2c0.sock", [], name="i2c0")
    i2c1 = I2CServer(outdir / "i2c1.sock", [], name="i2c1")

    regions: list[Region] = [TraceRegion(n, b) for n, b in UNMODELLED.items()]

    devices = [
        f"esp-gpio-block,base=0x{GPIO_BASE:x},irq={IRQ_GPIO},"
        f"socket={gpio.socket_path}",
        f"esp-spi-master,name=spi2,base=0x{SPI2_BASE:x},"
        f"cs_gpio={TFT_CS_PIN},dc_gpio={TFT_DC_PIN},miso=off,"
        f"socket={spi.socket_path}",
        f"esp-i2c-master,name=i2c0,base=0x{I2C0_BASE:x},irq={IRQ_I2C_EXT0},"
        f"socket={i2c0.socket_path}",
        f"esp-i2c-master,name=i2c1,base=0x{I2C1_BASE:x},irq={IRQ_I2C_EXT1},"
        f"socket={i2c1.socket_path}",
    ]
    board = Board(regions=regions, gpio=gpio, spi=spi, i2c0=i2c0, i2c1=i2c1,
                  panel=probe, devices=devices)
    return board


def attach_input(server, board: Board, outdir: Path):
    """No input routing yet: nothing on this board is modelled to receive it."""
    return None
