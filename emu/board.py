#!/usr/bin/env python3
"""The T-Deck Max: which windows QEMU handles, which we do, and what is wired up.

The split follows ADR 0011. SoC silicon -- GPIO, the SPI master, the I2C master
-- runs in C inside QEMU and hands over completed transactions. Everything
board-specific -- the panel, the I2C devices -- stays here in Python, where a
wrong assumption is a one-line edit rather than a rebuild.

What is left on the register-level bridge is only the windows nothing models at
all, claimed so that read-modify-write survives (ADR 0008).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from emu.bus import BusServer, Region
from emu.serial_link import SerialServer
from emu.devices.epaper import EPaperPanel
from emu.devices.i2c_devices import CST328, TCA8418, tdeck_max_devices
from emu import sdimage
from emu.devices.air import AirMedium
from emu.devices.gps import L76K
from emu.devices.ledc import Ledc
from emu.devices.sdcard import SdCard
from emu.devices.sx1262 import Sx1262
from emu.i2c_link import I2CServer
from emu.inputs import InputTail
from emu.peripherals.gpio import GpioClient
from emu.peripherals.i2c import I2CDevice
from emu.spi_link import SpiDevice, SpiServer

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
# The three parts on SPI2. The display's select was already named; the other
# two were read off the firmware rather than guessed (ADR 0043): with the GPIO
# trace on, pin 8 is pulled low immediately before the burst of SPI that fails
# to mount a card, and pin 9 before an earlier burst, which is the radio being
# probed. The firmware carries drivers for both -- it has "SD Card Size" and
# "[SX1262] Received packet!" in it -- and 9 is the T-Deck's radio select too.
SD_CS_PIN = 8
RADIO_CS_PIN = 9
EPD_CS_PIN = 34
EPD_DC_PIN = 35
EPD_BUSY_PIN = 37
KEYBOARD_INT_PIN = 15
# The radio's interrupt line. Not yet observed on this board; the T-Deck's
# number is used, and nothing depends on it until the firmware waits on it.
RADIO_DIO1_PIN = 45

# Which device each chip select carries, and which of them drive MISO back.
# The panel says nothing; the card and the radio both answer (ADR 0032).
SPI_DISPLAY, SPI_RADIO, SPI_SDCARD = 0, 1, 2
MISO_MASK = (1 << SPI_RADIO) | (1 << SPI_SDCARD)

# A card of the same size as the other board's, for the same reason: it is
# built here rather than shipped (ADR 0033).
SD_CARD_MB = 32
TOUCH_INT_PIN = 12

# The real panel raises a data-ready interrupt for every frame it captures, so a
# held finger produces a continuous stream of edges.
#
# The rate has to be comfortably above the rate the UI polls at, not merely
# close to it. The driver consumes the pending edge on each read and reports
# "not touched" when there is none, so a poll landing between edges looks like a
# finger lift.
TOUCH_REPORT_HZ = 500

# This board's firmware never executes a float before the scheduler starts, so
# it needs no help from ADR 0013 and does not get any.
SOC_DEVICES: list[str] = []

# Windows nothing models. Claiming them changes no behaviour on its own -- reads
# still return what was last written -- but it stops read-modify-write being
# silently discarded, which is what made the backlight PWM divide by zero.
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

    def reset(self) -> None:
        """These are registers inside the chip, so a reset clears them. A
        second boot then finds the window as the first one did."""
        self.regs.clear()


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
    i2c0: I2CServer
    i2c1: I2CServer
    panel: SpiDevice        # whichever panel this board carries
    # The access point's frame channel, when a run asked for one. The MAC is
    # a C device; what is on the air is this (ADR 0064).
    frames: object | None = None
    devices: list[str] = field(default_factory=list)
    # A part on one of the machine's own UARTs, when the scenario asks for one.
    # It needs no device of ours: the fork's controller already speaks to a
    # character device, so a socket is the whole of it (ADR 0052).
    gps: "SerialServer | None" = None

    def start(self) -> None:
        self.gpio.start()
        self.spi.start()
        self.i2c0.start()
        self.i2c1.start()
        if self.gps:
            self.gps.start()
        if self.frames:
            self.frames.start()
        # BUSY idles high on this panel. Set before QEMU connects; the client
        # replays it on connect, so the line is right from the first fetch.
        self.gpio.drive(EPD_BUSY_PIN, 1)

    def stop(self) -> None:
        for link in (self.spi, self.i2c0, self.i2c1, self.gpio,
                     self.gps, self.frames):
            if link:
                link.stop()

    def region(self, name: str) -> "Region | None":
        """A window's model by name, for a run that wants to report on it."""
        return next((r for r in self.regions if r.name == name), None)

    def qemu_args(self) -> list[str]:
        args: list[str] = []
        for spec in self.devices:
            args += ["-device", spec]
        # The order matters and is the only thing that names the port: the
        # console is given first, in build_argv, so this one is the machine's
        # second UART, which is where the board's satellite receiver sits.
        if self.gps:
            args += ["-serial", f"unix:{self.gps.socket_path}"]
        return args


def build(outdir: Path, air: str | None = None, ble_air: str | None = None,
          position: tuple[float, float] = (0.0, 0.0),
          antenna_dbi: float = 0.0, exponent: float = 2.0,
          obstruction_db: float = 0.0,
          gps: tuple[float, float, float] | None = None,
          wifi_ap: tuple[str, int, int, bool, str] | None = None) -> Board:
    """`ble_air` and `wifi_ap` are accepted and do nothing here: this board has
    a radio but no Bluetooth controller and no WiFi MAC modelled, so there is no
    part for either to reach. Every board takes the same arguments on purpose --
    when they did not, the caller's `except TypeError` quietly dropped this
    board back to a build with no air at all, and the radio options did nothing
    for a release (ADR 0052). Being ignored is said out loud below for the same
    reason.
    """
    outdir = Path(outdir)
    if wifi_ap:
        logging.getLogger("board").warning(
            "this board has no WiFi MAC modelled, so the access point %r is "
            "not put on the air", wifi_ap[0])
    panel = EPaperPanel(outdir / "frames",
                        quarter_turns=int(os.environ.get("BOARDEMUL_ROTATE", "0")))
    gpio = GpioClient(outdir / "gpio.sock")
    # The same three-part bus as the other board (ADR 0032), and for the same
    # reason: with one chip select forwarded the card and the radio were not
    # unmodelled but unreachable.
    medium = AirMedium(air, outdir.name) if air else None
    radio = Sx1262(air=medium, position=position, antenna_dbi=antenna_dbi,
                   exponent=exponent, obstruction_db=obstruction_db,
                   signal=lambda level: gpio.drive(RADIO_DIO1_PIN, level))
    sdcard = SdCard(sdimage.build(size_mb=SD_CARD_MB))
    spi = SpiServer(outdir / "spi2.sock",
                    {SPI_DISPLAY: panel, SPI_RADIO: radio, SPI_SDCARD: sdcard})
    i2c0 = I2CServer(outdir / "i2c0.sock", i2c_devices(), name="i2c0")
    i2c1 = I2CServer(outdir / "i2c1.sock", [], name="i2c1")
    # The same receiver on the same UART as the other board. No firmware to
    # hand exercises it here -- this board's own GPS example gives up before it
    # touches a UART at all -- so it is plumbed and unproven (ADR 0052).
    receiver = SerialServer(outdir / "gps.sock",
                            L76K(latitude=gps[0], longitude=gps[1],
                                 altitude_m=gps[2]), name="gps") if gps else None

    # Most of these windows are claimed only so that an access to them is
    # visible rather than swallowed. The backlight controller is the one
    # with a model behind it (ADR 0053).
    regions: list[Region] = [Ledc(n, b) if n == "ledc" else TraceRegion(n, b)
                             for n, b in UNMODELLED.items()]

    # GPIO is declared first: the SPI master registers a chip-select watch when
    # it is realized, and -device instances are realized in command-line order.
    devices = [
        f"esp-gpio-block,base=0x{GPIO_BASE:x},irq={IRQ_GPIO},"
        f"socket={gpio.socket_path}",
        f"esp-spi-master,name=spi2,base=0x{SPI2_BASE:x},"
        # The panel drives nothing back; the card and radio do, so MISO is
        # per device rather than per bus.
        f"cs_gpio={EPD_CS_PIN},cs1_gpio={RADIO_CS_PIN},cs2_gpio={SD_CS_PIN},"
        f"dc_gpio={EPD_DC_PIN},miso=off,miso_mask={MISO_MASK},"
        f"socket={spi.socket_path}",
        f"esp-i2c-master,name=i2c0,base=0x{I2C0_BASE:x},irq={IRQ_I2C_EXT0},"
        f"socket={i2c0.socket_path}",
        f"esp-i2c-master,name=i2c1,base=0x{I2C1_BASE:x},irq={IRQ_I2C_EXT1},"
        f"socket={i2c1.socket_path}",
    ]
    return Board(regions=regions, gpio=gpio, spi=spi, i2c0=i2c0, i2c1=i2c1,
                 panel=panel, gps=receiver, devices=devices)


def attach_input(server: BusServer, board: Board, outdir: Path) -> InputTail:
    """Route host input events to the devices that report them.

    The touch controller waits on a GPIO interrupt rather than polling, so
    delivering a touch means both giving it something to report and producing an
    edge. The keyboard is the opposite: it is polled, and nothing listens on its
    interrupt line at all (ADR 0010).
    """
    tail = InputTail(Path(outdir) / "input.jsonl")
    keypad = board.i2c0.devices.get(0x34)
    touch = board.i2c0.devices.get(0x1A)

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
                board.gpio.pulse_low(TOUCH_INT_PIN)
            else:
                log.warning("unknown input event %r", event)

    # The pump no longer has a bus to ride: with GPIO, SPI and I2C all serviced
    # in C, an idle UI produces almost no register-level traffic at all. Both
    # the event drain and the touch repeat run on their own clock.
    def ticker() -> None:
        period = 1.0 / TOUCH_REPORT_HZ
        while not tail._stop.is_set():
            pump()
            if isinstance(touch, CST328) and touch.touch is not None:
                board.gpio.pulse_low(TOUCH_INT_PIN)
            time.sleep(period)

    threading.Thread(target=ticker, name="input", daemon=True).start()
    tail.start()
    return tail
