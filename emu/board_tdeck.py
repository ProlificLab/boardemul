#!/usr/bin/env python3
"""The original LilyGO T-Deck: ESP32-S3, ST7789 LCD, GT911 touch.

Second board. The display is modelled; touch, the keyboard and the radio are
not, and the I2C bus deliberately carries no devices yet, so the controller
records every address the firmware probes -- which is how the first board's
device list was established.

Measuring first also settled the question ADR 0011 left open: this display is
driven through the SPI data path, so its pixels do cross our boundary.

Pins are the vendor's own, from examples/UnitTest/utilities.h.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from emu.board import TraceRegion, Board  # shared with the first board
from emu.bus import Region
from emu.i2c_link import I2CServer
from emu.devices.lcd import St7789Panel
from emu.devices.tdeck_i2c import GT911, TDeckKeyboard
from emu.peripherals.gpio import GpioClient
from emu.inputs import InputTail
from emu.spi_link import SpiServer

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

# The touch controller reports on an interrupt rather than being polled.
TOUCH_REPORT_HZ = 500
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
    # I2S moves its samples with GDMA on this chip, and an unclaimed window
    # discards writes and reads back zero (ADR 0008). Claiming it makes the
    # traffic visible, which is the first thing needed to find out what the
    # audio test is waiting for.
    "gdma": 0x6003F000,
}


def build(outdir: Path) -> Board:
    outdir = Path(outdir)
    panel = St7789Panel(outdir / "frames")
    gpio = GpioClient(outdir / "gpio.sock")
    spi = SpiServer(outdir / "spi2.sock", panel)
    # Discovered by watching the firmware probe: it looks for 0x14 and 0x5d
    # (touch), 0x55 (the keyboard) and 0x40 (an audio ADC we do not model).
    touch = GT911()
    keypad = TDeckKeyboard()
    i2c0 = I2CServer(outdir / "i2c0.sock", [touch, keypad], name="i2c0")
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
                  panel=panel, devices=devices)
    board.touch = touch
    board.keypad = keypad
    return board


def attach_input(server, board: Board, outdir: Path) -> InputTail:
    """Route host input to the touch controller and the keyboard.

    Same shape as the first board, and the same reason for the ticker: with
    GPIO, SPI and I2C all serviced in C, an idle UI produces almost no
    register-level traffic, so there is no bus activity for a pump to ride on.

    The touch controller raises an interrupt when it has something to report,
    which is why a held finger has to keep producing edges rather than being
    delivered once.
    """
    tail = InputTail(Path(outdir) / "input.jsonl")
    touch = board.i2c0.devices.get(GT911().address)
    keypad = board.i2c0.devices.get(TDeckKeyboard().address)

    def pump() -> None:
        for event in tail.drain():
            kind = event.get("type")
            if kind == "key" and isinstance(keypad, TDeckKeyboard):
                if not keypad.key_event(str(event.get("key", "")),
                                        bool(event.get("down", True))):
                    log.debug("no character for key %r", event.get("key"))
                elif bool(event.get("down", True)):
                    # This keyboard has its own interrupt line, unlike the
                    # Max's, so a firmware that has stopped polling still has
                    # to be told a key is waiting.
                    board.gpio.pulse_low(KEYBOARD_INT_PIN)
            elif kind == "touch" and isinstance(touch, GT911):
                if bool(event.get("down", True)):
                    touch.touch(int(event.get("x", 0)), int(event.get("y", 0)))
                else:
                    touch.release()
                board.gpio.pulse_low(TOUCH_INT_PIN)
            else:
                log.warning("unknown input event %r", event)

    def ticker() -> None:
        period = 1.0 / TOUCH_REPORT_HZ
        while not tail._stop.is_set():
            pump()
            if isinstance(touch, GT911) and touch.point is not None:
                board.gpio.pulse_low(TOUCH_INT_PIN)
            time.sleep(period)

    threading.Thread(target=ticker, name="input", daemon=True).start()
    tail.start()
    return tail
