#!/usr/bin/env python3
"""What a drawn face needs, shared by the boards with faces of their own.

A face is a rendered body plus the legends the window draws as text over it
(ADR 0126). The caps, their printed legends and the icon glyphs are the same
drawings on every board that carries LilyGO's BlackBerry-style keyboard; what
differs is where each board puts them, which is each face module's business.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from emu.raster import Colour, Raster, hex_colour, mix

C_INK = hex_colour("#f3f4f5")          # a legend's ink
C_SECOND = hex_colour("#e4e6e8")       # the smaller, offset second character


@dataclass
class Text:
    x: float
    y: float
    text: str
    size: float                     # pixels
    colour: str
    bold: bool = False
    anchor: str = "center"
    glows: bool = False             # a cap's legend, lit by the keyboard backlight


@dataclass
class Face:
    scale: int
    width: int
    height: int
    raster: Raster
    screen: tuple[int, int]         # the panel's top-left, in canvas pixels
    keys: list[tuple[float, float, float, float, str]] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    trackball: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # The Max's glass touch keys, in canvas pixels with their number (ADR 0131).
    soft_keys: list[tuple[float, float, float, float, int]] = field(default_factory=list)


def shrink(points, d):
    """Pull a convex outline in by about `d`, towards its centroid."""
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        n = math.hypot(dx, dy) or 1.0
        out.append((x - dx / n * d, y - dy / n * d))
    return out


def ring(img: Raster, cx: float, cy: float, r: float, w: float, colour: Colour,
         middle: Colour) -> None:
    img.circle(cx, cy, r, colour)
    img.circle(cx, cy, r - w, middle)


def cap(img: Raster, x0, y0, x1, y1, k, top: Colour = None,
        bottom: Colour = None) -> None:
    """A keycap; a board's caps differ in how smoky their plastic is."""
    top = top or hex_colour("#3b3c3f")
    bottom = bottom or hex_colour("#26272a")
    r = 0.95 * k
    img.rrect(x0, y0 + 0.18 * k, x1, y1 + 0.18 * k, r, hex_colour("#151617"))
    img.rrect(x0, y0, x1, y1, r, lambda y: mix(top, bottom, (y - y0) / (y1 - y0)))
    img.rrect(x0 + 0.35 * k, y0 + 0.12 * k, x1 - 0.35 * k, y0 + 0.30 * k, 0.1 * k,
              hex_colour("#5c5e62"), alpha=0.8)


def legend(face: Face, img: Raster, name: str, second: str, x0, y0, x1, y1, k) -> None:
    """Print a cap: a letter with its second character, or the key's glyph."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w = 0.16 * k
    if len(name) == 1:
        face.texts.append(Text(cx, cy + 0.75 * k, name, 3.0 * k, "#f3f4f5", bold=True,
                               glows=True))
        if second:
            face.texts.append(Text(cx - 0.55 * k, cy - 1.55 * k, second, 1.75 * k,
                                   "#e4e6e8", glows=True))
        if name == "$":                             # a speaker above the $
            speaker(img, cx + 0.2 * k, cy - 1.5 * k, k)
    elif name in ("ALT", "SYM"):
        face.texts.append(Text(cx, cy + 0.2 * k, name.lower(), 2.1 * k, "#f3f4f5",
                               glows=True))
    elif name == "DEL":
        backspace(img, cx, cy, k, w)
    elif name == "ENTER":
        img.segment(cx + 1.1 * k, cy - 1.2 * k, cx + 1.1 * k, cy + 0.6 * k, w * 1.6, C_INK)
        img.segment(cx + 1.1 * k, cy + 0.6 * k, cx - 0.6 * k, cy + 0.6 * k, w * 1.6, C_INK)
        img.polygon([(cx - 1.4 * k, cy + 0.6 * k), (cx - 0.5 * k, cy + 0.05 * k),
                     (cx - 0.5 * k, cy + 1.15 * k)], C_INK)
    elif name in ("LSHIFT", "RSHIFT", "UP", "UP2"):
        # The shift's outline, and the Max's up arrows in the same shape.
        img.polygon([(cx, cy - 1.15 * k), (cx + 1.05 * k, cy + 0.05 * k),
                     (cx - 1.05 * k, cy + 0.05 * k)], C_INK)
        if name in ("UP", "UP2"):
            img.rrect(cx - 0.45 * k, cy + 0.15 * k, cx + 0.45 * k, cy + 1.05 * k,
                      0.05 * k, C_INK)
        else:
            img.rrect(cx - 0.5 * k, cy, cx + 0.5 * k, cy + 0.95 * k, 0.05 * k, C_INK)
    elif name == "MIC":
        img.rrect(cx - 0.45 * k, cy - 0.95 * k, cx + 0.45 * k, cy + 0.55 * k, 0.45 * k, C_INK)
        for a0, a1 in ((200, 250), (250, 290), (290, 340)):
            r = 0.85 * k
            p0 = (cx + r * math.cos(math.radians(a0)), cy - 0.05 * k - r * math.sin(math.radians(a0)))
            p1 = (cx + r * math.cos(math.radians(a1)), cy - 0.05 * k - r * math.sin(math.radians(a1)))
            img.segment(*p0, *p1, w * 1.3, C_INK)
        img.segment(cx, cy + 0.8 * k, cx, cy + 1.25 * k, w * 1.3, C_INK)
        face.texts.append(Text(cx - 1.3 * k, cy - 1.45 * k, "0", 1.7 * k, "#e4e6e8",
                               glows=True))
    elif name == "SPACE":
        img.segment(cx - 2.6 * k, cy + 0.5 * k, cx + 2.6 * k, cy + 0.5 * k, w * 1.8, C_INK)
        for side in (-1, 1):
            img.segment(cx + side * 2.6 * k, cy + 0.6 * k, cx + side * 2.6 * k, cy - 0.15 * k,
                        w * 1.8, C_INK)


def backspace(img: Raster, cx, cy, k, w) -> None:
    left, right, top, bottom = cx - 1.5 * k, cx + 1.3 * k, cy - 0.95 * k, cy + 0.95 * k
    neck = cx - 0.6 * k
    for a, b in (((left, cy), (neck, top)), ((neck, top), (right, top)),
                 ((right, top), (right, bottom)), ((right, bottom), (neck, bottom)),
                 ((neck, bottom), (left, cy))):
        img.segment(*a, *b, w * 1.3, C_INK)
    c = cx + 0.3 * k
    img.segment(c - 0.5 * k, cy - 0.5 * k, c + 0.5 * k, cy + 0.5 * k, w * 1.3, C_INK)
    img.segment(c - 0.5 * k, cy + 0.5 * k, c + 0.5 * k, cy - 0.5 * k, w * 1.3, C_INK)


def speaker(img: Raster, cx, cy, k) -> None:
    s = 0.42 * k
    img.rrect(cx - 1.0 * s, cy - 0.45 * s, cx - 0.45 * s, cy + 0.45 * s, 0.05 * s, C_SECOND)
    img.polygon([(cx - 0.5 * s, cy - 0.45 * s), (cx + 0.15 * s, cy - 1.0 * s),
                 (cx + 0.15 * s, cy + 1.0 * s), (cx - 0.5 * s, cy + 0.45 * s)], C_SECOND)
    for r in (0.65, 1.15):
        img.segment(cx + (0.15 + r) * s, cy - r * 0.8 * s, cx + (0.15 + r) * s, cy + r * 0.8 * s,
                    0.14 * k, C_SECOND)
