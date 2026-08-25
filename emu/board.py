#!/usr/bin/env python3
"""The T-Deck Max as a set of intercepted MMIO windows.

Only windows the fork does not already model belong here. Anything QEMU handles
correctly -- UART, timers, eFuse, SHA -- is deliberately left alone (ADR 0005
decision 4).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from emu.bus import BusServer, Region
from emu.devices.epaper import EPaperPanel
from emu.devices.i2c_devices import CST328, TCA8418, tdeck_max_devices
from emu.inputs import InputTail
from emu.peripherals.gpio import GpioInputs, GpioOut
from emu.peripherals.i2c import I2CController, I2CDevice
from emu.peripherals.spi import SPIController

log = logging.getLogger("board")

# ESP32-S3 peripheral bases, from components/soc/esp32s3/include/soc/reg_base.h
I2C0_BASE = 0x60013000
I2C1_BASE = 0x60027000
SPI0_BASE = 0x60003000
SPI2_BASE = 0x60024000
SPI3_BASE = 0x60025000

# Interrupt source numbers, from periph_defs.h for esp32s3.
IRQ_GPIO = 16
IRQ_SPI2 = 21
IRQ_I2C_EXT0 = 42
IRQ_I2C_EXT1 = 43

# Board pins, from lib/TDeckMaxBoard/src/TDeckMaxBoard.h
EPD_BUSY_PIN = 37
KEYBOARD_INT_PIN = 15
TOUCH_INT_PIN = 12

# Roughly the report rate of the real panel while a finger is down.
TOUCH_REPORT_INTERVAL = 0.01

# Peripheral windows the fork leaves to its catch-all region. Claiming them
# changes no behaviour on its own -- reads still return what was last written --
# but it stops read-modify-write being silently discarded, which is what made
# the backlight PWM divide by zero (ADR 0008).
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

    Reads return what was last written, which is strictly more truthful than the
    catch-all's constant zero.
    """

    def __init__(self, name: str, base: int, size: int = 0x1000):
        super().__init__(name, base, size)
        self.regs: dict[int, int] = {}

    def read(self, offset: int, size: int) -> int:
        return self.regs.get(offset, 0)

    def write(self, offset: int, value: int, size: int) -> None:
        self.regs[offset] = value


def build_regions(outdir: Path) -> list[Region]:
    gpio_out = GpioOut()
    # BUSY idles high on this panel; left undriven it reads low and every
    # wait-while-busy loop in the driver spins forever.
    gpio_in = GpioInputs(gpio_out, static_high={EPD_BUSY_PIN}, irq=IRQ_GPIO)
    panel = EPaperPanel(gpio_out, Path(outdir) / "frames",
                        quarter_turns=int(os.environ.get("BOARDEMUL_ROTATE", "0")))
    return [
        I2CController("i2c0", I2C0_BASE, i2c_devices(), irq=IRQ_I2C_EXT0),
        I2CController("i2c1", I2C1_BASE, [], irq=IRQ_I2C_EXT1),
        gpio_out,
        gpio_in,
        # SPI2 carries both the panel and the LoRa radio, distinguished by
        # chip-select. Only the panel is modelled so far, and it checks its own
        # CS, so radio traffic is simply ignored rather than misread.
        SPIController("spi2", SPI2_BASE, [panel]),
        TraceRegion("spi0", SPI0_BASE),
        TraceRegion("spi3", SPI3_BASE),
    ] + [TraceRegion(n, b) for n, b in UNMODELLED.items()]


def find(regions: list[Region], kind: type) -> Region | None:
    return next((r for r in regions if isinstance(r, kind)), None)


def attach_input(server: BusServer, regions: list[Region], outdir: Path) -> InputTail:
    """Route host input events to the devices that report them.

    The firmware waits on GPIO interrupts rather than polling, so delivering an
    event means both giving the device something to report and raising its
    interrupt line (ADR 0010).
    """
    tail = InputTail(Path(outdir) / "input.jsonl")
    gpio: GpioInputs | None = find(regions, GpioInputs)
    i2c0 = find(regions, I2CController)
    keypad = i2c0.devices.get(0x34) if isinstance(i2c0, I2CController) else None
    touch = i2c0.devices.get(0x1A) if isinstance(i2c0, I2CController) else None

    # A real panel raises a new data-ready interrupt for every frame it
    # captures, so a held finger produces a continuous stream of edges. Firing
    # once per event instead makes the press visible to LVGL for a single read,
    # which is too brief to become a click.
    held = {"since": 0.0}

    def pump() -> None:
        for event in tail.drain():
            kind = event.get("type")
            if kind == "key" and isinstance(keypad, TCA8418):
                if not keypad.key_event(str(event.get("key", "")),
                                        bool(event.get("down", True))):
                    log.warning("no key mapping for %r", event.get("key"))
            elif kind == "touch" and isinstance(touch, CST328):
                down = bool(event.get("down", True))
                touch.touch = ((int(event.get("x", 0)), int(event.get("y", 0)))
                               if down else None)
                # The report is only read if an edge fired, so the edge has to
                # come after the coordinate is in place, not before.
                if gpio is not None:
                    gpio.pulse_low(TOUCH_INT_PIN)
            else:
                log.warning("unknown input event %r", event)

        if isinstance(touch, CST328) and touch.touch is not None and gpio is not None:
            now = time.monotonic()
            if now - held["since"] >= TOUCH_REPORT_INTERVAL:
                held["since"] = now
                gpio.pulse_low(TOUCH_INT_PIN)

        # The INT line follows the FIFO rather than being pulsed: the driver
        # drains events until the controller stops asserting, so releasing the
        # line is what ends its read loop. Both controllers signal active-low.
        if keypad is not None and gpio is not None:
            gpio.drive(KEYBOARD_INT_PIN, 0 if keypad.pending else 1)

    server.pumps.append(pump)
    tail.start()
    return tail


