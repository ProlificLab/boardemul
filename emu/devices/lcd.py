#!/usr/bin/env python3
"""ST7789 TFT controller, as the T-Deck drives it: 320x240, RGB565.

Modelled from the wire (ADR 0006). The command set was read off a real run
before any of this was written: an init block, then CASET/RASET to set a
rectangle and RAMWR to fill it, which is the whole of what has to be right for
a picture to appear.

The important difference from the e-paper panel next door is that an LCD has no
refresh command. It shows whatever is in controller RAM, continuously, so there
is no moment at which the hardware itself declares a frame. This model
snapshots instead: at once when a full-screen rectangle finishes, and otherwise
no more often than `interval` seconds, which stops a driver that flushes in
narrow strips from producing a hundred PNGs per screen.
"""

from __future__ import annotations

import logging
import struct
import time
from pathlib import Path

from emu.png import RGB, write_png
from emu.spi_link import SpiDevice

log = logging.getLogger("lcd")

# The controller's own raster is portrait. MADCTL's MV bit exchanges the
# addressable axes, which is how this board gets a landscape screen.
NATIVE_W, NATIVE_H = 240, 320

CMD_SWRESET = 0x01
CMD_SLPOUT = 0x11
CMD_INVOFF = 0x20
CMD_INVON = 0x21
CMD_DISPOFF = 0x28
CMD_DISPON = 0x29
CMD_CASET = 0x2A
CMD_RASET = 0x2B
CMD_RAMWR = 0x2C
CMD_RAMWRC = 0x3C
CMD_MADCTL = 0x36
CMD_COLMOD = 0x3A

# MADCTL bits.
MADCTL_MY = 0x80        # row address order    -- flips vertically
MADCTL_MX = 0x40        # column address order -- flips horizontally
MADCTL_MV = 0x20        # row/column exchange  -- landscape
MADCTL_BGR = 0x08       # the attached panel has a BGR colour filter

# Parameter bytes each command consumes. Anything absent is part of the init
# block: gamma curves, porches and power rails, none of which change a pixel,
# so the model records that it was sent and moves on.
ARG_COUNT = {CMD_CASET: 4, CMD_RASET: 4, CMD_MADCTL: 1, CMD_COLMOD: 1}

PIXEL_COMMANDS = {CMD_RAMWR, CMD_RAMWRC}


def rgb565_to_888(value: int) -> tuple[int, int, int]:
    """Expand by replicating the high bits, so full scale stays full scale --
    0x1F has to become 0xFF, not 0xF8, or white comes out faintly grey."""
    r = (value >> 11) & 0x1F
    g = (value >> 5) & 0x3F
    b = value & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


class St7789Panel(SpiDevice):
    name = "ST7789"

    def __init__(self, outdir: Path, interval: float = 0.5):
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.interval = interval

        self.madctl = 0
        self.colmod = 0x55          # 16 bits per pixel until told otherwise
        self.display_on = False
        self.inverted = False

        # Two bytes per pixel, held in the addressable space rather than the
        # panel's -- both orientations hold the same number of pixels.
        self.fb = bytearray(NATIVE_W * NATIVE_H * 2)
        self.xs = self.ys = 0
        self.xe, self.ye = NATIVE_W - 1, NATIVE_H - 1
        self.x = self.y = 0
        self.window_left = 0
        self.window_done = True

        self.command: int | None = None
        self.args = bytearray()
        self.commands: list[int] = []
        self.pixel_bytes = 0
        self.frames = 0
        self.dirty = False
        # Not zero: the first pixel byte would otherwise look overdue and
        # snapshot a blank framebuffer.
        self.last_render = time.monotonic()
        self.latest: Path | None = None
        self._new_window = False
        self._odd: int | None = None
        self._lut_cache: list[bytes] | None = None

    @property
    def size(self) -> tuple[int, int]:
        if self.madctl & MADCTL_MV:
            return NATIVE_H, NATIVE_W
        return NATIVE_W, NATIVE_H

    # -- the wire -----------------------------------------------------------

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        if dc == 0:
            for b in data:
                self._on_command(b)
        else:
            self._on_data(data)
        # Nothing here is readable back, so a duplex read gets zeros.
        return bytes(len(data)) if duplex else None

    def _on_command(self, cmd: int) -> None:
        self.command = cmd
        self.commands.append(cmd)
        self.args.clear()
        if cmd == CMD_RAMWR:
            self.x, self.y = self.xs, self.ys
            self.window_left = self._window_area()
            self.window_done = False
            self._odd = None
            self._new_window = True
            log.debug("RAMWR x %d..%d, y %d..%d (%d px)",
                      self.xs, self.xe, self.ys, self.ye, self.window_left)
        elif cmd == CMD_INVON:
            self.inverted = True
        elif cmd == CMD_INVOFF:
            self.inverted = False
        elif cmd == CMD_DISPON:
            self.display_on = True
        elif cmd == CMD_DISPOFF:
            self.display_on = False
        elif cmd == CMD_SWRESET:
            self.madctl = 0

    def _on_data(self, data: bytes) -> None:
        if self.command in PIXEL_COMMANDS:
            self._on_pixels(data)
            return
        want = ARG_COUNT.get(self.command)
        if want is None:
            return
        self.args += data
        if len(self.args) >= want:
            self._apply(self.command, bytes(self.args[:want]))
            self.args.clear()

    def _apply(self, cmd: int, args: bytes) -> None:
        w, h = self.size
        if cmd == CMD_CASET:
            self.xs = (args[0] << 8) | args[1]
            self.xe = min((args[2] << 8) | args[3], w - 1)
        elif cmd == CMD_RASET:
            self.ys = (args[0] << 8) | args[1]
            self.ye = min((args[2] << 8) | args[3], h - 1)
        elif cmd == CMD_MADCTL:
            if args[0] != self.madctl and self.pixel_bytes:
                log.info("MADCTL changed to 0x%02x after %d pixel byte(s)",
                         args[0], self.pixel_bytes)
            self.madctl = args[0]
            log.debug("MADCTL 0x%02x -> %dx%d%s%s%s", args[0], *self.size,
                      " MX" if args[0] & MADCTL_MX else "",
                      " MY" if args[0] & MADCTL_MY else "",
                      " BGR" if args[0] & MADCTL_BGR else "")
        elif cmd == CMD_COLMOD:
            self.colmod = args[0]
            if args[0] & 0x07 != 0x05:
                log.warning("COLMOD 0x%02x is not 16bpp; pixels will be wrong",
                            args[0])

    def _window_area(self) -> int:
        return max(0, self.xe - self.xs + 1) * max(0, self.ye - self.ys + 1)

    def _on_pixels(self, data: bytes) -> None:
        if self._new_window:
            # The first bytes of a fill say more than any amount of staring at
            # the resulting image: they are the pixel format, byte order and
            # colour all at once.
            log.debug("first pixels %s", data[:8].hex(" ", 2))
            self._new_window = False
        self.pixel_bytes += len(data)
        # A pixel is two bytes and a segment boundary can fall between them.
        # Dropping the odd byte would put every later pixel in this window one
        # byte out, which looks like corrupted colour rather than a framing bug.
        if self._odd is not None:
            data = bytes([self._odd]) + data
            self._odd = None
        if len(data) % 2:
            self._odd = data[-1]
            data = data[:-1]

        w, h = self.size
        fb = self.fb
        x, y, xs, xe, ys, ye = self.x, self.y, self.xs, self.xe, self.ys, self.ye
        if xe < xs or ye < ys:
            return

        for i in range(0, len(data), 2):
            off = (y * w + x) * 2
            if 0 <= off < len(fb) - 1:
                fb[off] = data[i]
                fb[off + 1] = data[i + 1]
            x += 1
            if x > xe:
                x = xs
                y += 1
                # The controller wraps to the top of the window rather than
                # running on into the rest of RAM.
                if y > ye:
                    y = ys
        self.x, self.y = x, y

        self.dirty = True
        self.window_left -= len(data) // 2
        # A full-screen rectangle is a frame, and it is worth waiting for: a
        # snapshot taken part-way through one shows a half-drawn screen that
        # nobody would have seen. The interval rule exists for the other kind
        # of driver, which flushes in strips and so never completes a window
        # that covers the screen.
        full_screen = self._window_area() >= w * h
        if self.window_left <= 0 and not self.window_done:
            # Only the transition matters. A driver may keep pushing into a
            # filled window, since the controller wraps.
            self.window_done = True
            if full_screen:
                self.render()
                return
        if not full_screen and time.monotonic() - self.last_render >= self.interval:
            self.render()

    # -- output -------------------------------------------------------------

    def _lut(self) -> list[bytes]:
        """Every possible RGB565 value expanded once.

        Rendering runs on the SPI thread, so converting 76,800 pixels one at a
        time would stall the guest for as long as it took.
        """
        if self._lut_cache is None:
            self._lut_cache = [bytes(rgb565_to_888(v)) for v in range(1 << 16)]
        return self._lut_cache

    def render(self) -> Path:
        """Two bits that look like they should transform the image, and do not.

        MADCTL's BGR bit tells the controller which colour filter the glass in
        front of it uses, and the controller then reorders so that ordinary
        RGB-ordered host data comes out right. Swapping here as well would
        double-compensate. INVON is the same shape of thing: these IPS panels
        are natively inverted, so the driver turns inversion on to arrive back
        at normal. Both are recorded in the summary and neither is applied.
        """
        w, h = self.size
        table = self._lut()
        fb = self.fb
        row_format = f">{w}H"
        mx = bool(self.madctl & MADCTL_MX)

        rows: list[bytes] = []
        for y in range(h):
            base = y * w * 2
            values = struct.unpack(row_format, bytes(fb[base:base + w * 2]))
            pixels = [table[v] for v in values]
            if mx:
                pixels.reverse()
            rows.append(b"".join(pixels))

        # MX and MY describe the direction the controller scans RAM out to the
        # glass, so they belong to how the picture is presented rather than to
        # how it was stored.
        if self.madctl & MADCTL_MY:
            rows.reverse()

        image = b"".join(rows)
        self.frames += 1
        path = self.outdir / f"frame-{self.frames:03d}.png"
        write_png(path, w, h, image, RGB)
        write_png(self.outdir / "latest.png", w, h, image, RGB)
        self.latest = path
        self.dirty = False
        self.last_render = time.monotonic()
        log.info("rendered %s (%dx%d, madctl 0x%02x)", path.name, w, h, self.madctl)
        return path

    def flush(self) -> None:
        """Emit whatever is pending, so a run that ends part-way through an
        update still shows its last screen rather than losing it."""
        if self.dirty:
            self.render()

    def summary(self) -> str:
        seen = ", ".join(f"0x{c:02x}" for c in dict.fromkeys(self.commands))
        w, h = self.size
        return (f"{self.name}: {self.frames} frame(s) at {w}x{h}, "
                f"{self.pixel_bytes} pixel byte(s), madctl 0x{self.madctl:02x}, "
                f"colmod 0x{self.colmod:02x}, "
                f"display {'on' if self.display_on else 'off'}"
                f"{', inverted' if self.inverted else ''}\n"
                f"    commands seen: [{seen}]")
