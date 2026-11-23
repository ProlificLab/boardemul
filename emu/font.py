#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A tiny bitmap font, so a face's legends can be baked into its own image.

The Tk window draws legends as canvas text over the rendered face (ADR 0126,
decision 3) — a browser cannot do that, so the server bakes them in before
serving the PNG. The font is a classic 5×7, rendered at four times the
target size and box-downsampled, which gives it the soft edges a bitmap
usually lacks.

Only the characters faces actually print are here: capitals, digits, the
second characters printed on the caps, the lowercase of `alt` and `sym`,
and the mark on the logo.
"""

from emu.raster import Colour, Raster

# Each glyph: seven rows, five bits per row, most significant bit left.
FONT = {
    "A": (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b11110, 0b10001, 0b10001, 0b10001, 0b11110),
    "C": (0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110),
    "D": (0b11100, 0b10010, 0b10001, 0b10001, 0b10001, 0b10010, 0b11100),
    "E": (0b11111, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000, 0b11111),
    "F": (0b11111, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000, 0b10000),
    "G": (0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111),
    "H": (0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001, 0b10001),
    "I": (0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "J": (0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100),
    "K": (0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001),
    "L": (0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    "M": (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    "N": (0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001),
    "O": (0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "P": (0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000),
    "Q": (0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101),
    "R": (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    "S": (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "V": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "W": (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001),
    "X": (0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001),
    "Y": (0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100),
    "Z": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111),
    "0": (0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "2": (0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111),
    "3": (0b11111, 0b00010, 0b00100, 0b00010, 0b00001, 0b10001, 0b01110),
    "4": (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    "6": (0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110),
    "7": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    "8": (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100),
    "#": (0b01010, 0b01010, 0b11111, 0b01010, 0b11111, 0b01010, 0b01010),
    "@": (0b01110, 0b10001, 0b10111, 0b10101, 0b10111, 0b10000, 0b01110),
    "(": (0b00010, 0b00100, 0b01000, 0b01000, 0b01000, 0b00100, 0b00010),
    ")": (0b01000, 0b00100, 0b00010, 0b00010, 0b00010, 0b00100, 0b01000),
    "_": (0, 0, 0, 0, 0, 0, 0b11111),
    "-": (0, 0, 0, 0b11111, 0, 0, 0),
    "+": (0b00100, 0b00100, 0b00100, 0b11111, 0b00100, 0b00100, 0b00100),
    "/": (0b00001, 0b00010, 0b00010, 0b00100, 0b01000, 0b01000, 0b10000),
    ":": (0, 0b00100, 0, 0, 0b00100, 0, 0),
    ";": (0, 0b00100, 0, 0, 0b00100, 0b00100, 0b01000),
    "'": (0b00100, 0b00100, 0, 0, 0, 0, 0),
    '"': (0b01010, 0b01010, 0, 0, 0, 0, 0),
    "!": (0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0, 0b00100),
    "?": (0b01110, 0b10001, 0b00001, 0b00110, 0b00100, 0, 0b00100),
    ",": (0, 0, 0, 0, 0b00110, 0b00100, 0b01000),
    ".": (0, 0, 0, 0, 0, 0b01100, 0b01100),
    "$": (0b00100, 0b01111, 0b10100, 0b01110, 0b00101, 0b11110, 0b00100),
    "*": (0, 0b10101, 0b01110, 0b11111, 0b01110, 0b10101, 0),
    "®": (0b11110, 0b10001, 0b10110, 0b10101, 0b10110, 0b10001, 0b11110),
    "a": (0, 0, 0b01110, 0b00001, 0b01111, 0b10001, 0b01111),
    "l": (0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "t": (0b01000, 0b01000, 0b11100, 0b01000, 0b01000, 0b01000, 0b01100),
    "s": (0, 0, 0b01111, 0b10000, 0b01110, 0b00001, 0b11110),
    "y": (0b10001, 0b10001, 0b10001, 0b10001, 0b01111, 0b00001, 0b01110),
    "m": (0, 0, 0b11011, 0b10101, 0b10101, 0b10101, 0b10101),
    " ": (0, 0, 0, 0, 0, 0, 0),
}

SS = 4  # supersampling factor


def text_size(text: str, height: float) -> tuple[float, float]:
    """Width and height of `text` rendered at a glyph height of `height` px."""
    cells = height * SS / 7.0
    return (len(text) * 6 - 1) * cells / SS, height


def _draw_glyph_layer(text: str, height: float) -> Raster:
    """The ink, supersampled: white where a glyph bit sits."""
    cell = height * SS / 7.0
    width = int((len(text) * 6 - 1) * cell) + 1
    img = Raster(width, int(height * SS) + 1, (0, 0, 0))
    white = (255, 255, 255)
    for index, char in enumerate(text):
        glyph = FONT.get(char, FONT.get(char.upper()))
        if glyph is None:
            continue
        gx = index * 6 * cell
        for row, bits in enumerate(glyph):
            for col in range(5):
                if bits & (1 << (4 - col)):
                    x0 = gx + col * cell
                    y0 = row * cell
                    img.rrect(x0, y0, x0 + cell, y0 + cell, 0, white)
    return img


def draw_text(img: Raster, x: float, y: float, text: str, height: float,
              colour: Colour, anchor: str = "center", bold: bool = False) -> None:
    """Blend `text` onto the image, centred (or anchored west) on x, y."""
    layer = _draw_glyph_layer(text, height)
    if bold:
        layer = _double(layer)
    ratio = 1.0 / SS
    w = max(1, int(layer.w * ratio))
    h = max(1, int(layer.h * ratio))
    # Box-average the supersampled ink into a coverage mask.
    coverage = bytearray(w * h)
    for ty in range(h):
        sy0 = int(ty / ratio)
        sy1 = max(sy0 + 1, int((ty + 1) / ratio))
        for tx in range(w):
            sx0 = int(tx / ratio)
            sx1 = max(sx0 + 1, int((tx + 1) / ratio))
            total = n = 0
            for sy in range(sy0, min(sy1, layer.h)):
                base = sy * layer.w
                for sx in range(sx0, min(sx1, layer.w)):
                    total += layer.px[(base + sx) * 3]
                    n += 1
            coverage[ty * w + tx] = total // max(1, n)
    if anchor == "center":
        x -= w / 2
        y -= h / 2
    elif anchor == "w":
        y -= h / 2
    for ty in range(h):
        dy = int(y) + ty
        if not 0 <= dy < img.h:
            continue
        for tx in range(w):
            dx = int(x) + tx
            if not 0 <= dx < img.w:
                continue
            a = coverage[ty * w + tx] / 255.0
            if a <= 0:
                continue
            i = (dy * img.w + dx) * 3
            for c in range(3):
                img.px[i + c] = round(img.px[i + c] + (colour[c] - img.px[i + c]) * a)


def _double(layer: Raster) -> Raster:
    """A poor man's bold: the ink struck twice, one pixel apart."""
    out = Raster(layer.w + 1, layer.h, (0, 0, 0))
    for y in range(layer.h):
        for x in range(layer.w):
            i = (y * layer.w + x) * 3
            if layer.px[i]:
                j = (y * out.w + x) * 3
                out.px[j:j + 3] = layer.px[i:i + 3]
                k = (y * out.w + x + 1) * 3
                out.px[k:k + 3] = layer.px[i:i + 3]
    return out
