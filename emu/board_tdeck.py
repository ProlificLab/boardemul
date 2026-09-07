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
import os
import threading
import time
from pathlib import Path

from emu.board import TraceRegion, Board  # shared with the first board
from emu.bus import Region
from emu import sdimage
from emu.i2c_link import I2CServer
from emu.devices.lcd import St7789Panel
from emu.devices.air import AirMedium
from emu.devices.es7210 import Es7210
from emu.devices.sdcard import SdCard
from emu.devices.spi_trace import SpiTrace
from emu.devices.sx1262 import Sx1262
from emu.devices.tdeck_i2c import GT911, TDeckKeyboard
from emu.peripherals.adc import SarAdc
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
# The interface's own size; the parts behind it are mounted the other way up.
DISPLAY_W, DISPLAY_H = 320, 240

TOUCH_INT_PIN = 16
# The trackball: five active-low push contacts on plain GPIOs, pulled up on
# the board, so an idle one reads high. Our GPIO block reports an input the
# host never drove as low, which is a button held down from power-on; the
# device-ui firmware took that as the boot logo being held, switched on
# Bluetooth and rebooted into its programming mode (ADR 0026). The pull-up
# lives in IO_MUX, which the fork leaves unimplemented, so the board says it.
TRACKBALL_PINS = {"press": 0, "left": 1, "right": 2, "up": 3, "down": 15}
KEYBOARD_INT_PIN = 46

# The touch controller reports on an interrupt rather than being polled.
TOUCH_REPORT_HZ = 500
SD_CS_PIN = 39
RADIO_CS_PIN = 9
# The radio nudges the processor on this line when something it was told
# to watch for happens: a packet sent, or one received (ADR 0036).
RADIO_DIO1_PIN = 45

# Which device each chip select is, in the order the controller is told them
# (ADR 0032), and which of them drive the shared MISO line. The display does
# not: the vendor firmware's "Init display id" comes from reading back the
# transmit FIFO, and answering it properly would take a model of the panel's
# read commands that nothing has needed yet.
SPI_DISPLAY, SPI_RADIO, SPI_SDCARD = 0, 1, 2
SD_CARD_MB = 32
MISO_MASK = (1 << SPI_RADIO) | (1 << SPI_SDCARD)

# ADR 0013. This board's vendor firmware runs a float in a C++ global
# constructor, before FreeRTOS exists to own the FPU state, and IDF panics.
# Leaving CP0 enabled out of reset is a divergence from the architectural reset
# value that we could not confirm against silicon, so it is stated here per
# board rather than applied to every board quietly.
SOC_DEVICES = ["esp-coproc-enable,mask=0x1"]

# Audio both ways, in C because completing a transfer means walking DMA
# descriptors in guest memory: the microphone captures on I2S1 and the speaker
# plays on I2S0. The firmware waits for each in turn during startup, so leaving
# either unmodelled stops it dead.
I2S0_BASE, GDMA_PERIPH_I2S0 = 0x6000F000, 3
I2S1_BASE, GDMA_PERIPH_I2S1 = 0x6002D000, 4

# Claimed with a model behind them, sized exactly. SENS is 0x400: the first cut
# took 0x1000 and ran into IO_MUX at 0x60009000, which the fork models -- the
# third bite from ADR 0008's amendment, this time by size rather than by base.
BLE_CORE_BASE = 0x60031000
IRQ_RWBLE = 8          # ETS_RWBLE_INTR_SOURCE: the dispatcher's line (ADR 0024)

EXTRA_REGIONS = {
    "radio_unknown": (0x6000E000, 0x1000),   # ADR 0019: the radio state page
    "sens": (0x60008800, 0x400),             # ADR 0020: the SAR ADC, battery read
    # Bluetooth (ADR 0023, open items). Once the controller has its clock, ROM
    # link-layer code copies tables into the exchange memory it shares with the
    # baseband, at 0x3FC00100 and up: below the internal SRAM the machine maps
    # from 0x3FC80000, and backed by nothing, so the guest took a bus error
    # there. Claimed up to where SRAM starts, as memory that keeps what is
    # written, until what the controller expects of it is understood.
    "bt_em": (0x3FC00000, 0x80000),
    # 0x60031000, the link-layer core's page, is a C device now (ADR 0025).
    # The page after LCD_CAM, named by nothing in the fork or IDF, that the
    # controller code touches next to the exchange memory (literals 0x60042000,
    # +4, +8 in the function after btdm_lpclk_set_div).
    "bt_unknown": (0x60042000, 0x1000),
    # The exchange-memory base registers (ADR 0023, open items). The ROM link
    # layer maps exchange-memory pages through registers it addresses from a
    # literal (0x4000a354) plus the page index from its own table (0x4000a314),
    # writes them at init and later asserts (emi.c:164) unless each reads
    # back with the page offset in its top fourteen bits. Nothing claimed the
    # page, so the readback was the machine default, zero.
}

# The class a region in EXTRA_REGIONS is served by; a plain trace window unless
# something here knows better.
EXTRA_CLASSES = {"radio_unknown": "PhyGates", "sens": "SarAdc"}

UNMODELLED = {
    # The radio front end. Claimed not because anything models it, but because
    # this is where the vendor firmware stops: with these invisible, the last
    # bus access looked like PSRAM timing calibration on SPI0, which sent a
    # whole investigation the wrong way.
    # Where the vendor firmware actually stops. It writes a little config here
    # and then polls +0x050 about a thousand times a second, forever, inside
    # esp_wifi_start(). Undocumented in both the fork's register map and IDF's,
    # and claimed only so the stall is visible rather than swallowed by the
    # machine's catch-all.
    # The MAC stage of esp_wifi_start(), once the PHY is up. IDF's own base list
    # names nothing at 0x60033000..0x60037FFF, but WDEV_RND_REG at 0x6003507C
    # places the MAC/WDEV block there. 0x60035000 itself is deliberately NOT
    # claimed: the fork's catch-all serves the hardware RNG at 0x6003507C, and
    # shadowing it with a trace window starves the bootloader of entropy and
    # stops the boot at "entry" (ADR 0008, amended).
    "wifi_mac0": 0x60033000,
    "wifi_mac1": 0x60034000,
    "wifi_mac3": 0x60036000,
    "wifi_mac4": 0x60037000,
    "bt": 0x60011000,
    "fe": 0x60006000,
    "nrx": 0x6001C000,
    "bb": 0x6001D000,
    "ledc": 0x60019000,
    "lcd_cam": 0x60041000,
    "uhci": 0x60014000,
    "pcnt": 0x60017000,
    "spi0": 0x60003000,
    "spi3": 0x60025000,
}


class PhyGates(TraceRegion):
    """A radio register page whose completion flags can be answered.

    On by default: every wait in GATES is answered, and that is what lets the
    vendor firmware get through esp_wifi_start() and build its interface
    (ADR 0019). BOARDEMUL_PHY_GATES=0 turns it back into a plain trace window,
    which is how each gate was found -- the firmware then stalls at the first
    unanswered wait, and the PC-watch hatch in the bridge says which.

    Every entry was read off a disassembled polling loop, never guessed, and
    each is one register and the exact bits it waits to see. The loops come
    in two shapes -- `bnei extui==N` waits for a state field, `bnone reg,mask`
    waits for a done bit -- and both are satisfied by forcing those bits high
    on read. The rule is one address at a time: these registers are shared
    with values early boot reads back, and forcing bits across a whole page
    hangs the guest at reset.

    None of this calibrates anything. There is no RF, so the numbers real
    calibration derives from analog readbacks are meaningless here; the app's
    successive-approximation loop still terminates because it steps a fixed
    number of times and only steers on the readback.
    """

    # absolute address -> bits forced high on read
    GATES = {
        0x6000e050: 0x07000000,   # ROM  : bnei extui(bits 26..24) == 7, state done
        0x6000e04c: 0x01000000,   # app  : bnone bit 24, SAR step complete
        0x60006174: 0x00010000,   # ROM  : bnone bit 16, FE calibration done
        0x60033d14: 0x00000001,   # app  : bbci bit 0, MAC reset acknowledged
    }

    @classmethod
    def covers(cls, base: int, size: int = 0x1000) -> bool:
        """Whether any gate lives in this page, so a page is wired through
        this class by content rather than by name."""
        return any(base <= a < base + size for a in cls.GATES)

    def __init__(self, name: str, base: int, size: int = 0x1000):
        super().__init__(name, base, size)
        self._gated = os.environ.get("BOARDEMUL_PHY_GATES", "1") != "0"

    def read(self, offset: int, size: int) -> int:
        v = super().read(offset, size)
        if self._gated:
            v |= self.GATES.get(self.base + offset, 0)
        return v

def build(outdir: Path, air: str | None = None,
          position: tuple[float, float] = (0.0, 0.0)) -> Board:
    outdir = Path(outdir)
    panel = St7789Panel(outdir / "frames")
    gpio = GpioClient(outdir / "gpio.sock")
    for pin in TRACKBALL_PINS.values():
        gpio.drive(pin, 1)          # nobody is pressing the trackball
    # Three parts share SPI2 (ADR 0032). The display drives nothing back; the
    # radio and the card both do, so they are on the MISO mask below. Neither
    # is modelled yet: what sits on their selects records what the firmware
    # asks for, which is where a model starts (ADR 0006).
    # On the air only when a directory is given to share with other boards
    # (ADR 0036); alone otherwise, which is a node with nobody in range.
    medium = AirMedium(air, outdir.name) if air else None
    radio = Sx1262(air=medium, position=position,
                   signal=lambda level: gpio.drive(RADIO_DIO1_PIN, level))
    # A card is in the slot, formatted and empty (ADR 0033). Its contents are
    # built here rather than shipped, and go no further than this run.
    sdcard = SdCard(sdimage.build(size_mb=SD_CARD_MB))
    spi = SpiServer(outdir / "spi2.sock",
                    {SPI_DISPLAY: panel, SPI_RADIO: radio, SPI_SDCARD: sdcard})
    # Discovered by watching the firmware probe: it looks for 0x14 and 0x5d
    # (touch), 0x55 (the keyboard) and 0x40 (the microphone codec).
    touch = GT911()          # its own portrait raster; see touch_point()
    keypad = TDeckKeyboard()
    codec = Es7210()         # the microphone codec the firmware probes for
    i2c0 = I2CServer(outdir / "i2c0.sock", [touch, keypad, codec], name="i2c0")
    i2c1 = I2CServer(outdir / "i2c1.sock", [], name="i2c1")

    regions: list[Region] = [PhyGates(n, b) if PhyGates.covers(b) else TraceRegion(n, b)
                             for n, b in UNMODELLED.items()]
    for name, (base, size) in EXTRA_REGIONS.items():
        cls = {"PhyGates": PhyGates, "SarAdc": SarAdc}.get(EXTRA_CLASSES.get(name, ""), TraceRegion)
        regions.append(cls(name, base, size))

    devices = [
        f"esp-gpio-block,base=0x{GPIO_BASE:x},irq={IRQ_GPIO},"
        f"socket={gpio.socket_path}",
        f"esp-spi-master,name=spi2,base=0x{SPI2_BASE:x},"
        f"cs_gpio={TFT_CS_PIN},cs1_gpio={RADIO_CS_PIN},cs2_gpio={SD_CS_PIN},"
        f"dc_gpio={TFT_DC_PIN},miso=off,miso_mask={MISO_MASK},"
        f"socket={spi.socket_path}",
        f"esp-i2c-master,name=i2c0,base=0x{I2C0_BASE:x},irq={IRQ_I2C_EXT0},"
        f"socket={i2c0.socket_path}",
        f"esp-i2c-master,name=i2c1,base=0x{I2C1_BASE:x},irq={IRQ_I2C_EXT1},"
        f"socket={i2c1.socket_path}",
        # No socket: it says nothing to Python. Its job is to let the DMA
        # engine finish the capture the firmware waits on at startup.
        f"esp-i2s-master,name=i2s0,base=0x{I2S0_BASE:x},"
        f"periph={GDMA_PERIPH_I2S0}",
        f"esp-i2s-master,name=i2s1,base=0x{I2S1_BASE:x},"
        f"periph={GDMA_PERIPH_I2S1}",
        # The Bluetooth link-layer core's page, on QEMU's own clock: its timers
        # must interrupt within the half-slot they were set for (ADR 0025).
        f"esp-ble-core,base=0x{BLE_CORE_BASE:x},irq={IRQ_RWBLE}",
    ]
    board = Board(regions=regions, gpio=gpio, spi=spi, i2c0=i2c0, i2c1=i2c1,
                  panel=panel, devices=devices)
    board.touch = touch
    board.keypad = keypad
    return board


# What the touch controller reports at the edges of the visible glass, in its
# own raster. Not the full 240x320: the active area sits inside it, and these
# are the bounds Meshtastic's driver carries as corner points measured on real
# hardware ({0,2, 0,314, 223,5, 224,314}). Averaged per edge (ADR 0030).
TOUCH_X_EDGES = (0.0, 223.5)        # bottom of the screen .. top
TOUCH_Y_EDGES = (3.5, 314.0)        # left of the screen .. right


def touch_point(x: int, y: int) -> tuple[int, int]:
    """Where a finger at display (x, y) lands in the touch controller's raster.

    The panel is 320x240 to the firmware and 240x320 to both the parts behind
    it, so the controller's x axis runs down the screen and its y axis across
    it, counting up the screen rather than down. Those measured corners give the
    spans: the pair sharing a raw x is a screen row, the pair sharing a raw y is
    a screen column, at a scale within a few per cent of one to one, which is
    the raster relation itself. Which way up the x axis runs was settled on the
    device, not read off the constants (ADR 0030).
    """
    x0, x1 = TOUCH_X_EDGES
    y0, y1 = TOUCH_Y_EDGES
    raw_x = x0 + (DISPLAY_H - 1 - y) * (x1 - x0) / (DISPLAY_H - 1)
    raw_y = y0 + x * (y1 - y0) / (DISPLAY_W - 1)
    return round(raw_x), round(raw_y)


def attach_input(server, board: Board, outdir: Path) -> InputTail:
    """Route host input to the touch controller and the keyboard.

    Same shape as the first board, and the same reason for the ticker: with
    GPIO, SPI and I2C all serviced in C, an idle UI produces almost no
    register-level traffic, so there is no bus activity for a pump to ride on.

    The touch controller raises an interrupt when it has something to report,
    which is why a held finger has to keep producing edges rather than being
    delivered once.
    """
    radio = board.spi.devices.get(SPI_RADIO)
    on_air = radio if getattr(radio, "air", None) is not None else None

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
                down = bool(event.get("down", True))
                if down:
                    touch.touch(*touch_point(int(event.get("x", 0)),
                                             int(event.get("y", 0))))
                else:
                    touch.release()
                # Recorded so a verbose bus log shows where the host's finger
                # sat among the controller's polls, which is what decides
                # whether the firmware could have seen it.
                log.debug("touch %s at %s", "down" if down else "up", touch.point)
                # A level, and the model's to state: it follows the trigger
                # mode the firmware wrote, or rests low until one is written
                # (ADR 0029). The Max's CST328 next door pulses instead.
                board.gpio.drive(TOUCH_INT_PIN, touch.int_level)
            else:
                log.warning("unknown input event %r", event)

    def ticker() -> None:
        period = 1.0 / TOUCH_REPORT_HZ
        while not tail._stop.is_set():
            pump()
            if on_air is not None:
                # On the ticker rather than the bus pumps: an idle guest makes
                # almost no register accesses, which is the same reason the
                # touch controller is driven from here.
                on_air.pump()
            if isinstance(touch, GT911):
                # Idempotent: the client sends nothing for an unchanged level.
                # This is where a mode write with no finger down moves the
                # line's rest level (ADR 0029).
                board.gpio.drive(TOUCH_INT_PIN, touch.int_level)
            time.sleep(period)

    threading.Thread(target=ticker, name="input", daemon=True).start()
    tail.start()
    return tail
