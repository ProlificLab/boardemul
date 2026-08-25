#!/usr/bin/env python3
"""The T-Deck Max: which windows QEMU handles, which we do, and what is wired up.

The split follows ADR 0011. SoC silicon -- GPIO and the SPI master -- runs in C
inside QEMU, because chip-select has to be readable on every transfer and
because a register-level bridge costs 232,320 round trips per display frame.
Everything board-specific -- the panel, the I2C devices -- stays here in Python,
where a wrong assumption is a one-line edit rather than a rebuild.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from emu.bus import BusServer, Region
from emu.devices.epaper import EPaperPanel
from emu.devices.i2c_devices import CST328, TCA8418, tdeck_max_devices
from emu.inputs import InputTail
from emu.peripherals.gpio import GpioClient
from emu.peripherals.i2c import I2CController, I2CDevice
from emu.spi_link import SpiServer

log = logging.getLogger("board")

# ESP32-S3 peripheral bases, from components/soc/esp32s3/include/soc/reg_base.h
GPIO_BASE = 0x60004000
I2C0_BASE = 0x60013000
I2C1_BASE = 0x60027000
SPI0_BASE = 0x60003000
SPI2_BASE = 0x60024000
SPI3_BASE = 0x60025000

# Interrupt source numbers, from periph_defs.h for esp32s3.
IRQ_GPIO = 16
IRQ_I2C_EXT0 = 42
IRQ_I2C_EXT1 = 43

# Board pins, from lib/TDeckMaxBoard/src/TDeckMaxBoard.h
EPD_CS_PIN = 34
EPD_DC_PIN = 35
EPD_BUSY_PIN = 37
KEYBOARD_INT_PIN = 15
TOUCH_INT_PIN = 12

# The real panel raises a data-ready interrupt for every frame it captures, so a
# held finger produces a continuous stream of edges.
#
# The rate has to be comfortably above the rate the UI polls at, not merely
# close to it. The driver consumes the pending edge on each read and reports
# "not touched" when there is none, so a poll that lands between edges looks
# like a finger lift. At the panel's own 100 Hz against a 33 Hz poll, four in
# ten polls came back released and the click was never seen as a single press.
TOUCH_REPORT_HZ = 500

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
    "spi0": SPI0_BASE,
    "spi3": SPI3_BASE,
}


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


@dataclass
class Board:
    regions: list[Region]
    gpio: GpioClient
    spi: SpiServer
    panel: EPaperPanel
    devices: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.gpio.start()
        self.spi.start()
        # BUSY idles high on this panel. Set before QEMU connects; the client
        # replays it on connect, so the line is right from the first fetch.
        self.gpio.drive(EPD_BUSY_PIN, 1)

    def stop(self) -> None:
        self.spi.stop()
        self.gpio.stop()

    def qemu_args(self) -> list[str]:
        args: list[str] = []
        for spec in self.devices:
            args += ["-device", spec]
        return args


def build(outdir: Path) -> Board:
    outdir = Path(outdir)
    panel = EPaperPanel(outdir / "frames",
                        quarter_turns=int(os.environ.get("BOARDEMUL_ROTATE", "0")))
    gpio = GpioClient(outdir / "gpio.sock")
    spi = SpiServer(outdir / "spi2.sock", panel)

    regions: list[Region] = [
        I2CController("i2c0", I2C0_BASE, i2c_devices(), irq=IRQ_I2C_EXT0),
        I2CController("i2c1", I2C1_BASE, [], irq=IRQ_I2C_EXT1),
    ] + [TraceRegion(n, b) for n, b in UNMODELLED.items()]

    # GPIO is declared first: the SPI master registers a chip-select watch when
    # it is realized, and -device instances are realized in command-line order.
    devices = [
        f"esp-gpio-block,base=0x{GPIO_BASE:x},irq={IRQ_GPIO},"
        f"socket={gpio.socket_path}",
        f"esp-spi-master,name=spi2,base=0x{SPI2_BASE:x},"
        # The panel drives nothing back, so duplex transfers need no round trip.
        f"cs_gpio={EPD_CS_PIN},dc_gpio={EPD_DC_PIN},miso=off,"
        f"socket={spi.socket_path}",
    ]
    return Board(regions=regions, gpio=gpio, spi=spi, panel=panel, devices=devices)


def find(regions: list[Region], kind: type) -> Region | None:
    return next((r for r in regions if isinstance(r, kind)), None)


def attach_input(server: BusServer, board: Board, outdir: Path) -> InputTail:
    """Route host input events to the devices that report them.

    The touch controller waits on a GPIO interrupt rather than polling, so
    delivering a touch means both giving it something to report and producing an
    edge. The keyboard is the opposite: it is polled, and nothing listens on its
    interrupt line at all (ADR 0010).
    """
    tail = InputTail(Path(outdir) / "input.jsonl")
    i2c0 = find(board.regions, I2CController)
    keypad = i2c0.devices.get(0x34) if isinstance(i2c0, I2CController) else None
    touch = i2c0.devices.get(0x1A) if isinstance(i2c0, I2CController) else None

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
                # The edge has to follow the coordinate, not precede it: the
                # driver reads the report only because an edge fired.
                board.gpio.pulse_low(TOUCH_INT_PIN)
            else:
                log.warning("unknown input event %r", event)

    server.pumps.append(pump)

    def repeat() -> None:
        """Keep re-arming the touch interrupt while a finger is down.

        This cannot ride the bus pump: the pump only runs when the guest touches
        an intercepted window, and now that SPI and GPIO are serviced in C an
        idle UI generates almost no bus traffic at all.
        """
        period = 1.0 / TOUCH_REPORT_HZ
        while not tail._stop.is_set():
            if isinstance(touch, CST328) and touch.touch is not None:
                board.gpio.pulse_low(TOUCH_INT_PIN)
            time.sleep(period)

    threading.Thread(target=repeat, name="touch", daemon=True).start()
    tail.start()
    return tail
