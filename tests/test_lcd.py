#!/usr/bin/env python3
"""The ST7789 panel, driven by synthetic wire traffic.

The T-Deck's firmware only ever fills the screen with one colour before it
blocks on I2S, so booting it proves almost nothing about this model. These
tests send the command sequences a driver would send and check the picture that
comes out, which is the only way to know the window arithmetic is right before
a firmware exists that exercises it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.lcd import (  # noqa: E402
    CMD_CASET, CMD_COLMOD, CMD_MADCTL, CMD_RAMWR, CMD_RASET,
    MADCTL_MV, MADCTL_MX, MADCTL_MY, NATIVE_H, NATIVE_W,
    St7789Panel, rgb565_to_888,
)
from tests.support import png_colour_type, read_png  # noqa: E402

RED = 0xF800
GREEN = 0x07E0
BLUE = 0x001F
WHITE = 0xFFFF
BLACK = 0x0000

LANDSCAPE = MADCTL_MV           # 320x240


def command(p: St7789Panel, cmd: int, *args: int) -> None:
    p.on_segment(0, bytes([cmd]), False)
    if args:
        p.on_segment(1, bytes(args), False)


def make_panel(tmp_path: Path, madctl: int = LANDSCAPE) -> St7789Panel:
    # A long interval keeps the time-based snapshot out of these tests; every
    # frame they see is one a rule they are testing asked for.
    p = St7789Panel(tmp_path / "frames", interval=3600)
    command(p, CMD_COLMOD, 0x55)
    command(p, CMD_MADCTL, madctl)
    return p


def window(p: St7789Panel, xs: int, xe: int, ys: int, ye: int) -> None:
    command(p, CMD_CASET, xs >> 8, xs & 0xFF, xe >> 8, xe & 0xFF)
    command(p, CMD_RASET, ys >> 8, ys & 0xFF, ye >> 8, ye & 0xFF)


def fill(p: St7789Panel, colour: int, count: int, chunk: int | None = None) -> None:
    """Send `count` pixels, optionally broken into segments of `chunk` bytes."""
    command(p, CMD_RAMWR)
    data = bytes([colour >> 8, colour & 0xFF]) * count
    if chunk is None:
        p.on_segment(1, data, False)
        return
    for i in range(0, len(data), chunk):
        p.on_segment(1, data[i:i + chunk], False)


def pixel(pixels: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    off = (y * width + x) * 3
    return pixels[off], pixels[off + 1], pixels[off + 2]


def fill_screen(p: St7789Panel, colour: int, chunk: int | None = None) -> None:
    w, h = p.size
    window(p, 0, w - 1, 0, h - 1)
    fill(p, colour, w * h, chunk)


# -- colour -----------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (WHITE, (255, 255, 255)),
    (BLACK, (0, 0, 0)),
    (RED, (255, 0, 0)),
    (GREEN, (0, 255, 0)),
    (BLUE, (0, 0, 255)),
])
def test_colour_conversion_reaches_full_scale(value: int, expected) -> None:
    """Shifting alone would make white 0xF8F8F8, a visibly dirty white."""
    assert rgb565_to_888(value) == expected


def test_frames_are_written_as_colour(tmp_path: Path) -> None:
    p = make_panel(tmp_path)
    fill_screen(p, RED)
    assert png_colour_type(p.latest) == 2


# -- geometry ---------------------------------------------------------------


def test_a_full_screen_fill_renders_that_colour(tmp_path: Path) -> None:
    p = make_panel(tmp_path)
    fill_screen(p, RED)
    width, height, pixels = read_png(p.latest)
    assert (width, height) == (320, 240)
    assert set(zip(pixels[0::3], pixels[1::3], pixels[2::3])) == {(255, 0, 0)}


def test_madctl_mv_swaps_the_addressable_axes(tmp_path: Path) -> None:
    """Without MV the controller's own portrait raster is what is addressed."""
    portrait = make_panel(tmp_path / "p", madctl=0)
    assert portrait.size == (NATIVE_W, NATIVE_H) == (240, 320)
    landscape = make_panel(tmp_path / "l", madctl=MADCTL_MV)
    assert landscape.size == (NATIVE_H, NATIVE_W) == (320, 240)

    fill_screen(portrait, BLUE)
    assert read_png(portrait.latest)[:2] == (240, 320)


def test_only_the_addressed_rectangle_changes(tmp_path: Path) -> None:
    p = make_panel(tmp_path)
    fill_screen(p, BLACK)
    window(p, 10, 19, 20, 29)
    fill(p, WHITE, 10 * 10)
    p.flush()

    width, _, pixels = read_png(p.latest)
    assert pixel(pixels, width, 10, 20) == (255, 255, 255)
    assert pixel(pixels, width, 19, 29) == (255, 255, 255)
    assert pixel(pixels, width, 9, 20) == (0, 0, 0)
    assert pixel(pixels, width, 20, 29) == (0, 0, 0)
    assert pixel(pixels, width, 10, 30) == (0, 0, 0)


def test_pixels_wrap_within_the_window(tmp_path: Path) -> None:
    """Running off the end of a window returns to its top-left, not into the
    rest of RAM -- otherwise an over-long write would corrupt the screen."""
    p = make_panel(tmp_path)
    fill_screen(p, BLACK)
    window(p, 0, 9, 0, 9)
    command(p, CMD_RAMWR)
    p.on_segment(1, bytes([RED >> 8, RED & 0xFF]) * 100, False)
    p.on_segment(1, bytes([GREEN >> 8, GREEN & 0xFF]) * 100, False)
    p.flush()

    width, _, pixels = read_png(p.latest)
    assert pixel(pixels, width, 0, 0) == (0, 255, 0), "the second pass should win"
    assert pixel(pixels, width, 9, 9) == (0, 255, 0)
    assert pixel(pixels, width, 10, 0) == (0, 0, 0), "it must not spill sideways"


@pytest.mark.parametrize("mirror", [MADCTL_MX, MADCTL_MY, MADCTL_MX | MADCTL_MY])
def test_the_mirror_bits_do_not_transform_the_picture(tmp_path: Path,
                                                      mirror: int) -> None:
    """The driver chooses MADCTL so that its own coordinate system lands the
    right way up on the glass it is wired to. Applying the mirror here as well
    double-counts it, which is how Meshtastic's boot screen first rendered with
    its logo and version text written backwards."""
    plain = make_panel(tmp_path / f"plain{mirror}", madctl=MADCTL_MV)
    flagged = make_panel(tmp_path / f"flag{mirror}", madctl=MADCTL_MV | mirror)
    for p in (plain, flagged):
        fill_screen(p, BLACK)
        window(p, 0, 9, 0, 9)
        fill(p, RED, 100)
        p.flush()

    assert read_png(flagged.latest) == read_png(plain.latest)
    width, _, pixels = read_png(flagged.latest)
    assert pixel(pixels, width, 0, 0) == (255, 0, 0)
    assert pixel(pixels, width, 319, 239) == (0, 0, 0)


# -- segmentation, which is where this project's bugs have lived -------------


@pytest.mark.parametrize("chunk", [2, 6, 512, 4096, 32768])
def test_an_even_split_gives_the_same_picture(tmp_path: Path, chunk: int) -> None:
    whole = make_panel(tmp_path / f"whole{chunk}")
    split = make_panel(tmp_path / f"split{chunk}")
    fill_screen(whole, GREEN)
    fill_screen(split, GREEN, chunk=chunk)
    assert read_png(split.latest) == read_png(whole.latest)


@pytest.mark.parametrize("chunk", [1, 3, 7, 4095])
def test_an_odd_split_gives_the_same_picture(tmp_path: Path, chunk: int) -> None:
    """A pixel is two bytes and a segment boundary can fall between them. Every
    earlier bug of this shape in this project looked like healthy traffic and
    wrong output, so the odd sizes are the point of this test, not an edge
    case bolted on to the even one."""
    whole = make_panel(tmp_path / f"whole{chunk}")
    split = make_panel(tmp_path / f"split{chunk}")
    fill_screen(whole, BLUE)
    fill_screen(split, BLUE, chunk=chunk)
    assert read_png(split.latest) == read_png(whole.latest)


def test_a_split_across_two_windows_does_not_carry_a_stale_byte(tmp_path: Path) -> None:
    """An odd tail belongs to the window it arrived in. Carrying it into the
    next one would shift that whole rectangle by a byte."""
    p = make_panel(tmp_path)
    fill_screen(p, BLACK)
    window(p, 0, 9, 0, 9)
    command(p, CMD_RAMWR)
    p.on_segment(1, bytes([RED >> 8]), False)      # half a pixel, then abandoned
    window(p, 20, 29, 0, 9)
    fill(p, WHITE, 100)
    p.flush()

    width, _, pixels = read_png(p.latest)
    assert pixel(pixels, width, 20, 0) == (255, 255, 255)
    assert pixel(pixels, width, 29, 9) == (255, 255, 255)


# -- when a frame is declared ------------------------------------------------


def test_one_frame_per_full_screen_fill(tmp_path: Path) -> None:
    """The window completing is the frame. Rendering per segment would emit the
    same picture five times for one screen."""
    p = make_panel(tmp_path)
    fill_screen(p, RED, chunk=4096)
    assert p.frames == 1
    fill_screen(p, BLUE, chunk=4096)
    assert p.frames == 2


def test_a_partial_update_waits(tmp_path: Path) -> None:
    """A strip is not a frame, and with the interval long there is no reason to
    snapshot yet."""
    p = make_panel(tmp_path)
    fill_screen(p, BLACK)
    window(p, 0, 9, 0, 9)
    fill(p, RED, 100)
    assert p.frames == 1
    assert p.dirty


def test_flush_emits_what_is_pending(tmp_path: Path) -> None:
    """A run that ends part-way through an update should still show its last
    screen rather than losing it."""
    p = make_panel(tmp_path)
    fill_screen(p, BLACK)
    window(p, 0, 9, 0, 9)
    fill(p, RED, 100)
    p.flush()
    assert p.frames == 2
    assert not p.dirty
    p.flush()
    assert p.frames == 2, "nothing pending, so nothing to write"


def test_the_panel_answers_a_duplex_read_with_zeros(tmp_path: Path) -> None:
    p = make_panel(tmp_path)
    assert p.on_segment(1, bytes(4), duplex=True) == bytes(4)
