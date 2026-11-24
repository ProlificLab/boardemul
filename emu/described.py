# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Boards described by their users: an ESP32-S3 and catalogue parts on its pins.

A board description is a TOML file (ADR 0184). `load()` reads one strictly and
answers a `Description`, or raises `DescriptionError` naming what is wrong;
`module()` turns a description into what `emu/run.py` builds a board from, the
same three names a hand-written board module has: `SOC_DEVICES`, `build()` and
`attach_input()`.

The chip is the T-Deck's (decision 3): its windows, WiFi, Bluetooth, I2S and
USB, from `emu/board_tdeck.py`. What a description adds is which parts are on
SPI2's three selects, on the two I2C buses, on the second UART and on plain
pins.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from emu import board_tdeck as chip
from emu import sdimage
from emu.board import Board
from emu.bus import Region
from emu.devices.air import HOST as AIR_LOOPBACK, AirMedium, air_host, keep_time
from emu.devices.backlight import Backlight, GpioReports
from emu.devices.es7210 import Es7210
from emu.devices.gps import L76K
from emu.devices.lcd import St7789Panel
from emu.devices.ledc import Ledc
from emu.devices.sdcard import SdCard
from emu.devices.sx1262 import Sx1262
from emu.devices.tdeck_i2c import GT911, TDeckKeyboard
from emu.devices.usb_otg import OtgCable
from emu.devices.wifi_ap import FrameChannel, build_access_point
from emu.guest_clock import GuestClock
from emu.i2c_link import I2CServer
from emu.peripherals.gpio import GpioClient
from emu.png import FrameWriter
from emu.serial_link import SerialServer
from emu.spi_link import SpiServer

CHIPS = ("esp32s3",)
PIN_MAX = 48                # GPIO0..GPIO48 on the ESP32-S3
SPI_SELECTS = 3             # what QEMU's SPI master offers (ADR 0184)
SUFFIX = ("", "1", "2")     # its per-select property names: cs_gpio, cs1_gpio, ...
SD_CARD_MB = chip.SD_CARD_MB    # a card's size when the description says none


class DescriptionError(ValueError):
    """What is wrong with a board description, said so its author can fix it."""


@dataclass(frozen=True)
class Kind:
    """A catalogue part: where it sits and what it takes."""
    bus: str                                # spi2, i2c, uart1 or gpio
    needs: tuple[str, ...] = ()             # pins it must be given
    may: tuple[str, ...] = ()               # pins it may be given
    options: dict[str, type] = field(default_factory=dict)
    address: int | None = None              # an I2C part's own address
    screen: bool = False
    drives_miso: bool = False               # an SPI part that answers


CATALOGUE: dict[str, Kind] = {
    "st7789": Kind("spi2", needs=("cs",), may=("backlight",), screen=True),
    "sx1262": Kind("spi2", needs=("cs",), may=("busy", "reset", "irq"),
                   drives_miso=True),
    "sdcard": Kind("spi2", needs=("cs",), options={"size_mb": int},
                   drives_miso=True),
    "gt911": Kind("i2c", may=("irq",), address=GT911.address),
    "tdeck-keyboard": Kind("i2c", address=TDeckKeyboard.address),
    "es7210": Kind("i2c", address=Es7210.address),
    "l76k": Kind("uart1"),
    "tdeck-trackball": Kind("gpio", needs=("press", "left", "right", "up", "down")),
}


@dataclass
class Placed:
    """One part as the description wires it."""
    part: str
    pins: dict[str, int]
    options: dict[str, object] = field(default_factory=dict)
    address: int | None = None


@dataclass
class Description:
    name: str
    chip: str = "esp32s3"
    spi_dc: int | None = None
    spi: list[Placed] = field(default_factory=list)
    i2c: tuple[list[Placed], list[Placed]] = field(default_factory=lambda: ([], []))
    uart1: Placed | None = None
    gpio: list[Placed] = field(default_factory=list)
    source: str = ""                        # the file's text, kept with a device

    def parts(self) -> list[Placed]:
        return (self.spi + self.i2c[0] + self.i2c[1] + self.gpio
                + ([self.uart1] if self.uart1 else []))

    def screen(self) -> Placed | None:
        return next((p for p in self.spi if CATALOGUE[p.part].screen), None)


def load(path: str | Path) -> Description:
    """Read a board description file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DescriptionError(f"{path}: {exc.strerror or exc}") from None
    return parse(text, where=str(path))


def parse(text: str, where: str = "board description") -> Description:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DescriptionError(f"{where}: not TOML: {exc}") from None
    reader = _Reader(where)
    desc = reader.read(data)
    desc.source = text
    return desc


class _Reader:
    def __init__(self, where: str):
        self.where = where
        self.used: dict[int, str] = {}      # pin -> what took it

    def fail(self, message: str) -> None:
        raise DescriptionError(f"{self.where}: {message}")

    def read(self, data: dict) -> Description:
        self.only(data, "the description", ("name", "chip", "spi2", "i2c0", "i2c1",
                                           "uart1", "gpio"))
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            self.fail("`name` is required: what the board is called")
        chip_name = data.get("chip", "esp32s3")
        if chip_name not in CHIPS:
            self.fail(f"chip {chip_name!r}: BoardEmul emulates {', '.join(CHIPS)} only")
        desc = Description(name=name.strip(), chip=chip_name)

        spi = self.table(data, "spi2")
        self.only(spi, "[spi2]", ("dc", "parts"))
        if "dc" in spi:
            desc.spi_dc = self.pin(spi["dc"], "spi2's dc")
        desc.spi = [self.part(entry, "spi2", f"spi2 part {n + 1}")
                    for n, entry in enumerate(self.parts_list(spi, "spi2"))]
        if len(desc.spi) > SPI_SELECTS:
            self.fail(f"spi2 has {len(desc.spi)} parts; QEMU's SPI master "
                      f"has {SPI_SELECTS} selects")
        if sum(CATALOGUE[p.part].screen for p in desc.spi) > 1:
            self.fail("spi2 carries more than one screen")
        screen = desc.screen()
        if screen is not None and desc.spi_dc is None:
            self.fail(f"the {screen.part} screen needs spi2's `dc` line")

        for bus in (0, 1):
            table = self.table(data, f"i2c{bus}")
            self.only(table, f"[i2c{bus}]", ("parts",))
            placed = [self.part(entry, "i2c", f"i2c{bus} part {n + 1}")
                      for n, entry in enumerate(self.parts_list(table, f"i2c{bus}"))]
            seen: dict[int, str] = {}
            for p in placed:
                if p.address in seen:
                    self.fail(f"i2c{bus}: {p.part} and {seen[p.address]} are "
                              f"both at 0x{p.address:02x}")
                seen[p.address] = p.part
            desc.i2c[bus].extend(placed)

        uart = self.table(data, "uart1")
        if uart:
            desc.uart1 = self.part(uart, "uart1", "[uart1]")

        gpio = self.table(data, "gpio")
        self.only(gpio, "[gpio]", ("parts",))
        desc.gpio = [self.part(entry, "gpio", f"gpio part {n + 1}")
                     for n, entry in enumerate(self.parts_list(gpio, "gpio"))]
        return desc

    def table(self, data: dict, key: str) -> dict:
        value = data.get(key, {})
        if not isinstance(value, dict):
            self.fail(f"`{key}` is a table, [{key}]")
        return value

    def parts_list(self, table: dict, bus: str) -> list:
        value = table.get("parts", [])
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            self.fail(f"{bus}'s parts are a list of tables, [[{bus}.parts]]")
        return value

    def only(self, table: dict, what: str, allowed) -> None:
        extra = sorted(set(table) - set(allowed))
        if extra:
            self.fail(f"{what} has {', '.join(map(repr, extra))}, which it does "
                      f"not take; it takes {', '.join(allowed)}")

    def pin(self, value, what: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 0 <= value <= PIN_MAX:
            self.fail(f"{what} is a GPIO number from 0 to {PIN_MAX}, not {value!r}")
        if value in self.used:
            self.fail(f"GPIO {value} is both {self.used[value]} and {what}")
        self.used[value] = what
        return value

    def part(self, entry: dict, bus: str, what: str) -> Placed:
        name = entry.get("part")
        if name not in CATALOGUE:
            known = ", ".join(sorted(CATALOGUE))
            self.fail(f"{what}: part {name!r} is not in the catalogue ({known})")
        kind = CATALOGUE[name]
        if kind.bus != bus:
            self.fail(f"{what}: {name} goes on {kind.bus}, not {bus}")
        allowed = ("part",) + kind.needs + kind.may + tuple(kind.options) \
            + (("address",) if kind.address is not None else ())
        self.only(entry, f"{what} ({name})", allowed)
        for pin in kind.needs:
            if pin not in entry:
                self.fail(f"{what}: {name} needs `{pin}`, the GPIO it is wired to")
        pins = {p: self.pin(entry[p], f"{name}'s {p}")
                for p in kind.needs + kind.may if p in entry}
        options = {}
        for key, typ in kind.options.items():
            if key in entry:
                if not isinstance(entry[key], typ) or isinstance(entry[key], bool) \
                        or entry[key] <= 0:
                    self.fail(f"{what}: {name}'s {key} is a positive {typ.__name__}")
                options[key] = entry[key]
        address = None
        if kind.address is not None:
            address = entry.get("address", kind.address)
            if isinstance(address, bool) or not isinstance(address, int) \
                    or not 0x08 <= address <= 0x77:
                self.fail(f"{what}: {name}'s address is a 7-bit I2C address "
                          f"from 0x08 to 0x77, not {address!r}")
        return Placed(name, pins, options, address)


class _NoScreen:
    """Where a board without a screen keeps what run.py asks of a panel."""
    latest = None

    def flush(self) -> None:
        pass

    def summary(self) -> str:
        return "no screen on this board"


def build(desc: Description, outdir: Path, air: str | None = None,
          ble_air: str | None = None,
          position: tuple[float, float] = (0.0, 0.0),
          antenna_dbi: float = 0.0, exponent: float = 2.0,
          obstruction_db: float = 0.0,
          gps: tuple[float, float, float] | None = None,
          wifi_ap: tuple | None = None,
          air_boards: int = 0, air_quantum_ns: int = 1_000_000,
          audio: bool = False, mic: str | None = None,
          air_name: str | None = None) -> Board:
    """The board a description wires, as `emu/board_tdeck.py`'s build makes
    the T-Deck: the same parts, made the same way, on the description's pins."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)   # a board may have no screen to make it
    sound = ""
    if audio:
        (outdir / "audio").mkdir(parents=True, exist_ok=True)
        sound += ",speaker={}".format((outdir / "audio").as_posix() + "/{name}")
    if mic:
        sound += f",mic={Path(mic).resolve().as_posix()}"
    gpio = GpioClient(outdir / "gpio.sock")
    reports = GpioReports(outdir / "gpio-report.sock")
    for placed in desc.gpio:
        if placed.part == "tdeck-trackball":
            for pin in placed.pins.values():
                gpio.drive(pin, 1)      # pulled up on the board; nobody pressing

    clock = GuestClock(outdir / "clock.sock",
                       quantum_ns=air_quantum_ns if air_boards else 0)
    medium = AirMedium(air, air_name or outdir.name,
                       timed=bool(air_boards)) if air else None

    panel = None
    spi_parts: dict[int, object] = {}
    spec = f"esp-spi-master,name=spi2,base=0x{chip.SPI2_BASE:x},"
    lines: list[str] = []
    miso_mask = 0
    backlight_pin = None
    for slot, placed in enumerate(desc.spi):
        kind = CATALOGUE[placed.part]
        lines.append(f"cs{SUFFIX[slot]}_gpio={placed.pins['cs']}")
        if placed.part == "st7789":
            panel = St7789Panel(outdir / "frames")
            panel.writer = FrameWriter()
            backlight_pin = placed.pins.get("backlight")
            spi_parts[slot] = panel
        elif placed.part == "sx1262":
            irq = placed.pins.get("irq")
            radio = Sx1262(air=medium, position=position, antenna_dbi=antenna_dbi,
                           exponent=exponent, obstruction_db=obstruction_db,
                           signal=(lambda level, pin=irq: gpio.drive(pin, level, force=True))
                           if irq is not None else None,
                           clock=clock.seconds)
            if medium is not None and air_boards and irq is not None:
                clock.on_barrier = keep_time(radio, medium, gpio, irq, air_boards)
            spi_parts[slot] = radio
        elif placed.part == "sdcard":
            size = int(placed.options.get("size_mb", SD_CARD_MB))
            spi_parts[slot] = SdCard(sdimage.build(size_mb=size))
        for line in ("busy", "reset", "irq"):
            if line in placed.pins:
                lines.append(f"{line}{SUFFIX[slot]}_gpio={placed.pins[line]}")
        if kind.drives_miso:
            miso_mask |= 1 << slot
    if desc.spi_dc is not None:
        lines.append(f"dc_gpio={desc.spi_dc}")
    spi = SpiServer(outdir / "spi2.sock", spi_parts)
    spec += ",".join(lines + ["miso=off", f"miso_mask={miso_mask}",
                              f"socket={spi.socket_path}"])

    buses = []
    for bus, placed_parts in enumerate(desc.i2c):
        made = []
        for placed in placed_parts:
            if placed.part == "gt911":
                touch = GT911(address=placed.address)
                if "irq" in placed.pins:
                    # Its line at rest from the start (ADR 0113).
                    gpio.drive(placed.pins["irq"], touch.int_level)
                made.append(touch)
            elif placed.part == "tdeck-keyboard":
                made.append(TDeckKeyboard(address=placed.address))
            elif placed.part == "es7210":
                made.append(Es7210(address=placed.address))
        buses.append(I2CServer(outdir / f"i2c{bus}.sock", made, name=f"i2c{bus}"))

    receiver = None
    if desc.uart1 is not None and desc.uart1.part == "l76k" and gps:
        # Only when the run says where the board is, as on the T-Deck (ADR 0052).
        receiver = SerialServer(outdir / "gps.sock",
                                L76K(latitude=gps[0], longitude=gps[1],
                                     altitude_m=gps[2], clock=clock.seconds),
                                name="gps")

    regions: list[Region] = [chip._claim(n, b) for n, b in chip.UNMODELLED.items()]
    for name, (base, size) in chip.EXTRA_REGIONS.items():
        cls = {"PhyGates": chip.PhyGates, "SarAdc": chip.SarAdc}.get(
            chip.EXTRA_CLASSES.get(name, ""), chip.TraceRegion)
        regions.append(cls(name, base, size))

    report_pins = 1 << backlight_pin if backlight_pin is not None else 0
    devices = [
        f"esp-gpio-block,base=0x{chip.GPIO_BASE:x},irq={chip.IRQ_GPIO},"
        f"socket={gpio.socket_path},"
        f"report={reports.path},report_pins=0x{report_pins:x}",
        spec,
        f"esp-i2c-master,name=i2c0,base=0x{chip.I2C0_BASE:x},irq={chip.IRQ_I2C_EXT0},"
        f"socket={buses[0].socket_path}",
        f"esp-i2c-master,name=i2c1,base=0x{chip.I2C1_BASE:x},irq={chip.IRQ_I2C_EXT1},"
        f"socket={buses[1].socket_path}",
        f"esp-i2s-master,name=i2s0,base=0x{chip.I2S0_BASE:x},"
        f"periph={chip.GDMA_PERIPH_I2S0}" + sound.format(name="i2s0"),
        f"esp-i2s-master,name=i2s1,base=0x{chip.I2S1_BASE:x},"
        f"periph={chip.GDMA_PERIPH_I2S1}" + sound.format(name="i2s1"),
        f"esp-wifi-mac,base=0x{chip.WIFI_MAC_BASE:x},irq={chip.IRQ_WIFI_MAC}"
        + (f",beacon-ms={min(wifi_ap[1], 5)},ap-channel={wifi_ap[2] or 1}"
           f",frames={outdir / 'wifi.sock'}"
           if wifi_ap else ""),
        f"esp-ble-core,base=0x{chip.BLE_CORE_BASE:x},irq={chip.IRQ_RWBLE}"
        + (f",air={ble_air},airname={air_name or outdir.name}" if ble_air else "")
        + (f",airhost={air_host()}" if ble_air and air_host() != AIR_LOOPBACK
           else ""),
    ]
    frames = FrameChannel(outdir / "wifi.sock", build_access_point(wifi_ap)) \
        if wifi_ap else None
    usb = OtgCable.logging_to(outdir / "otg.sock",
                              outdir / f"{air_name or outdir.name}.otg.log")
    board = DescribedBoard(regions=regions, gpio=gpio, spi=spi, i2c0=buses[0],
                           i2c1=buses[1], panel=panel or _NoScreen(), gps=receiver,
                           devices=devices, frames=frames, clock=clock, usb_otg=usb)
    board.description = desc
    parts = [d for b in buses for d in b.devices.values()]
    board.touch = next((d for d in parts if isinstance(d, GT911)), None)
    board.keypad = next((d for d in parts if isinstance(d, TDeckKeyboard)), None)
    board.reports = reports
    board.backlight = Backlight(next((r for r in regions if isinstance(r, Ledc)), None)) \
        if backlight_pin is not None else None
    return board


class DescribedBoard(Board):
    """A board from a description. It drives no line it was not told of:
    the hand-written boards' start() sets the Max's e-paper BUSY high."""

    def start(self) -> None:
        if self.clock:
            self.clock.start()
        self.gpio.start()
        self.spi.start()
        self.i2c0.start()
        self.i2c1.start()
        if self.gps:
            self.gps.start()
        if self.frames:
            self.frames.start()
        if self.usb_otg is not None:
            self.usb_otg.start()

    def not_wired(self) -> list[str]:
        """What the firmware talked to that the description does not wire
        (ADR 0180, decision 3)."""
        found = [f"i2c{n} 0x{a:02x}" for n, bus in enumerate((self.i2c0, self.i2c1))
                 for a in sorted(bus.nacks)]
        found += [f"spi2 select {cs}" for cs in sorted(self.spi.unmodelled)]
        return found


def module(desc: Description) -> SimpleNamespace:
    """What run.py builds a board from: a hand-written board module's names."""
    screen = desc.screen()
    touch = next((p for bus in desc.i2c for p in bus if p.part == "gt911"), None)
    trackball = next((p for p in desc.gpio if p.part == "tdeck-trackball"), None)

    def attach(server, board, outdir):
        return chip.attach_input(
            server, board, outdir,
            touch_int=touch.pins.get("irq") if touch else None,
            backlight_pin=screen.pins.get("backlight") if screen else None,
            trackball_pins=dict(trackball.pins) if trackball else None)

    return SimpleNamespace(
        SOC_DEVICES=list(chip.SOC_DEVICES),
        build=lambda outdir, **kw: build(desc, outdir, **kw),
        attach_input=attach,
        description=desc)
