#!/usr/bin/env python3
"""GDEQ031T10 e-paper panel, 320x240, 1 bit per pixel.

The panel is driven over SPI2 with chip-select and data/command on ordinary
GPIOs, so this needs the GPIO tracker to tell a command byte from a pixel byte.

Rather than hard-coding one controller's command set, the model watches for a
full frame's worth of data to accumulate and renders on the next refresh
command. That way an unexpected controller variant still produces an image, and
the command log says what it actually sent.
"""

from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path

from emu.peripherals.gpio import GpioOut
from emu.peripherals.spi import SPIDevice

log = logging.getLogger("epd")

# The panel is sold as 320x240, but its controller RAM is laid out in the
# native portrait orientation: 240 pixels per row, 320 rows. Rendering it as
# 320 wide transposes the image and the text comes out running sideways.
WIDTH = 240
HEIGHT = 320
ROW_BYTES = WIDTH // 8
FRAME_BYTES = ROW_BYTES * HEIGHT

# Pins, from lib/TDeckMaxBoard/src/TDeckMaxBoard.h
PIN_CS = 34
PIN_DC = 35
PIN_RST = 9

# Observed on the wire: this panel is a UC825x-family controller, not the
# SSD16xx the 0x24/0x26 command pair would imply. 0x10 carries the previous
# image and 0x13 the new one; only 0x13 is what ends up on screen.
CMD_DTM1_OLD = 0x10
CMD_DTM2_NEW = 0x13
CMD_DISPLAY_REFRESH = 0x12
CMD_POWER_ON = 0x04

# Kept so an SSD16xx-based panel variant still renders rather than silently
# producing nothing.
CMD_WRITE_RAM_BW = 0x24
CMD_WRITE_RAM_RED = 0x26
CMD_MASTER_ACTIVATION = 0x20

RAM_WRITE_COMMANDS = {CMD_DTM1_OLD, CMD_DTM2_NEW, CMD_WRITE_RAM_BW, CMD_WRITE_RAM_RED}
IGNORED_PLANE_COMMANDS = {CMD_DTM1_OLD, CMD_WRITE_RAM_RED}
REFRESH_COMMANDS = {CMD_DISPLAY_REFRESH, CMD_MASTER_ACTIVATION}


def write_png(path: Path, width: int, height: int, pixels: bytes) -> None:
    """Minimal 8-bit greyscale PNG. zlib is in the allowed dependency set and
    is all a PNG actually needs."""
    raw = b"".join(b"\x00" + pixels[y * width:(y + 1) * width] for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def _rotate(pixels: bytearray, w: int, h: int, quarter_turns: int) -> tuple[int, int, bytearray]:
    """Rotate the raster by whole quarter turns, clockwise.

    The device is portrait and so is the panel's raster, so the shipping
    firmware needs no turn at all -- its launcher comes out upright as stored.
    Examples that appear sideways are drawing through a GxEPD2 rotation of their
    own; that is their choice, not a property of the hardware, which is why this
    defaults to zero rather than to whatever makes one demo look right.
    """
    turns = quarter_turns % 4
    if turns == 0:
        return w, h, pixels
    if turns == 2:
        return w, h, bytearray(reversed(pixels))
    out = bytearray(w * h)
    if turns == 1:              # clockwise
        for y in range(w):
            base = y * h
            for x in range(h):
                out[base + x] = pixels[(h - 1 - x) * w + y]
    else:                       # anticlockwise
        for y in range(w):
            base = y * h
            src_col = w - 1 - y
            for x in range(h):
                out[base + x] = pixels[x * w + src_col]
    return h, w, out


class EPaperPanel(SPIDevice):
    name = "GDEQ031T10"

    def __init__(self, gpio: GpioOut, outdir: Path, quarter_turns: int = 1):
        self.gpio = gpio
        self.quarter_turns = quarter_turns
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.ram = bytearray(b"\xff" * FRAME_BYTES)   # 1 == white
        self.ram_ptr = 0
        self.command: int | None = None
        self.commands: list[int] = []
        self.frames = 0
        self.data_since_command = 0
        self.dirty = False
        self.latest: Path | None = None

    def selected(self) -> bool:
        return self.gpio.pin(PIN_CS) == 0   # active low

    def _is_command(self) -> bool:
        return self.gpio.pin(PIN_DC) == 0

    def transfer(self, data: bytes) -> bytes:
        if self._is_command():
            for b in data:
                self._on_command(b)
        else:
            self._on_data(data)
        return b"\x00" * len(data)

    def _on_command(self, cmd: int) -> None:
        self.command = cmd
        self.commands.append(cmd)
        self.data_since_command = 0
        if cmd in RAM_WRITE_COMMANDS:
            self.ram_ptr = 0
        elif cmd in REFRESH_COMMANDS and self.dirty:
            self.render()

    def _on_data(self, data: bytes) -> None:
        self.data_since_command += len(data)
        if self.command not in RAM_WRITE_COMMANDS:
            return
        if self.command in IGNORED_PLANE_COMMANDS:
            return   # previous-image and red planes never reach the screen
        for b in data:
            if self.ram_ptr < FRAME_BYTES:
                self.ram[self.ram_ptr] = b
                self.ram_ptr += 1
        self.dirty = True
        # Some drivers push a frame and refresh with a command this model does
        # not recognise; a full frame is reason enough to render.
        if self.ram_ptr >= FRAME_BYTES:
            self.render()

    def render(self) -> Path:
        pixels = bytearray(WIDTH * HEIGHT)
        for y in range(HEIGHT):
            row = y * ROW_BYTES
            base = y * WIDTH
            for xb in range(ROW_BYTES):
                byte = self.ram[row + xb]
                for bit in range(8):
                    # MSB is the leftmost pixel; a set bit is white.
                    pixels[base + xb * 8 + bit] = 0xFF if byte & (0x80 >> bit) else 0x00
        # The firmware draws in landscape on a portrait raster, so the stored
        # image is a quarter turn away from how the device is actually held.
        out_w, out_h, pixels = _rotate(pixels, WIDTH, HEIGHT, self.quarter_turns)
        self.frames += 1
        path = self.outdir / f"frame-{self.frames:03d}.png"
        write_png(path, out_w, out_h, bytes(pixels))
        write_png(self.outdir / "latest.png", out_w, out_h, bytes(pixels))
        self.latest = path
        self.dirty = False
        ink = sum(1 for p in pixels if p == 0)
        log.info("rendered %s (%d of %d pixels inked)", path.name, ink, len(pixels))
        return path

    def summary(self) -> str:
        seen = ", ".join(f"0x{c:02x}" for c in dict.fromkeys(self.commands))
        return (f"{self.name}: {self.frames} frame(s), "
                f"{len(self.commands)} command(s) [{seen}]")
