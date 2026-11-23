#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A small anti-aliased rasteriser, standard library only (ADR 0126).

Tk's canvas draws shapes without anti-aliasing, gradients or alpha, so a
rounded device outline stair-steps at the sizes the skin is drawn at. This
paints into an RGB buffer instead, and the window shows the result as one
photo image.

Every shape is convex, described by the interval it covers on any horizontal
line. Each pixel row is sampled on four sub-rows. Columns covered on all four
are filled directly; the few at the edges get their exact horizontal overlap,
averaged over the sub-rows. That is exact coverage at every edge for the cost
of a handful of pixels per row. Anything concave is painted as convex pieces,
and a ring as a disc with another disc over it.

Paint is a colour, a function of the row (a vertical gradient, one colour per
row), or a function of the pixel (a shader, for small shapes only).
"""

from __future__ import annotations

import base64
import math
from typing import Callable, Sequence, Union

Colour = tuple[int, int, int]
Interval = Callable[[float], "tuple[float, float] | None"]
Paint = Union[Colour, Callable[[float], Colour], Callable[[float, float], Colour]]

SUBROWS = 4


def mix(a: Colour, b: Colour, t: float) -> Colour:
    """a at t = 0, b at t = 1."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


_TABLES: dict = {}


def _tables(colour: Colour, alpha: float) -> tuple:
    """For each channel, every old value blended towards the colour."""
    key = (colour, round(alpha, 4))
    found = _TABLES.get(key)
    if found is None:
        found = tuple(bytes(round(v + (c - v) * alpha) for v in range(256)) for c in colour)
        if len(_TABLES) > 4096:
            _TABLES.clear()
        _TABLES[key] = found
    return found


def hex_colour(text: str) -> Colour:
    text = text.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


# -- shapes, as the interval they cover at height y ---------------------------

def rrect(x0: float, y0: float, x1: float, y1: float, r: float) -> Interval:
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))

    def span(y: float):
        if y < y0 or y >= y1:
            return None
        d = 0.0
        if y < y0 + r:
            d = y0 + r - y
        elif y > y1 - r:
            d = y - (y1 - r)
        inset = r - math.sqrt(max(0.0, r * r - d * d)) if d else 0.0
        return x0 + inset, x1 - inset
    return span


def circle(cx: float, cy: float, r: float) -> Interval:
    def span(y: float):
        d = y - cy
        if d <= -r or d >= r:
            return None
        h = math.sqrt(r * r - d * d)
        return cx - h, cx + h
    return span


def polygon(points: Sequence[tuple[float, float]]) -> Interval:
    """A convex polygon, in either winding.

    Rows are asked for top to bottom, so the edges a row crosses are kept as
    the rows go rather than looked for among all of them each time: a device
    outline has a hundred edges and a row crosses two."""
    edges = []
    for i in range(len(points)):
        (ax, ay), (bx, by) = points[i], points[(i + 1) % len(points)]
        if ay != by:
            if ay > by:
                ax, ay, bx, by = bx, by, ax, ay
            edges.append((ay, by, ax, (bx - ax) / (by - ay)))
    edges.sort()
    top = min(p[1] for p in points)
    bottom = max(p[1] for p in points)
    state = {"y": -math.inf, "next": 0, "active": []}

    def span(y: float):
        if y < top or y >= bottom:
            return None
        if y < state["y"]:                          # asked out of order: start over
            state.update(next=0, active=[])
        state["y"] = y
        while state["next"] < len(edges) and edges[state["next"]][0] <= y:
            state["active"].append(edges[state["next"]])
            state["next"] += 1
        active = [e for e in state["active"] if e[1] > y]
        state["active"] = active
        if len(active) < 2:
            return None
        xs = [x0 + (y - y0) * slope for y0, _y1, x0, slope in active]
        return min(xs), max(xs)
    return span


def segment(x0: float, y0: float, x1: float, y1: float, width: float) -> Interval:
    """A straight stroke with square ends."""
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length * width / 2, dx / length * width / 2
    return polygon([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny),
                    (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)])


def bounds(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    return min(p[1] for p in points), max(p[1] for p in points)


# -- the image ----------------------------------------------------------------

class Raster:
    """An RGB image that shapes are painted into."""

    def __init__(self, width: int, height: int, background: Colour = (0, 0, 0)):
        self.w, self.h = width, height
        self.px = bytearray(bytes(background) * (width * height))

    def fill(self, span: Interval, y0: float, y1: float, paint: Paint,
             alpha: float = 1.0, shader: bool = False) -> None:
        """Paint the shape `span` describes between heights y0 and y1.

        `paint` is a colour, or a function of the row's centre height, or --
        with `shader` -- a function of a pixel's centre."""
        row_paint = callable(paint) and not shader
        for py in range(max(0, math.floor(y0)), min(self.h, math.ceil(y1))):
            spans = [span(py + (k + 0.5) / SUBROWS) for k in range(SUBROWS)]
            present = [s for s in spans if s is not None and s[1] > s[0]]
            if not present:
                continue
            colour = paint(py + 0.5) if row_paint else paint
            lo = max(0, math.floor(min(s[0] for s in present)))
            hi = min(self.w, math.ceil(max(s[1] for s in present)))
            core_lo, core_hi = hi, hi
            if len(present) == SUBROWS:
                core_lo = max(lo, math.ceil(max(s[0] for s in present)))
                core_hi = min(hi, math.floor(min(s[1] for s in present)))
                if core_hi <= core_lo:
                    core_lo, core_hi = hi, hi
            base = py * self.w
            # The fully covered middle of the row.
            if core_hi > core_lo:
                if shader:
                    for x in range(core_lo, core_hi):
                        self._blend(base + x, paint(x + 0.5, py + 0.5), alpha)
                elif alpha >= 1.0:
                    self.px[(base + core_lo) * 3:(base + core_hi) * 3] = \
                        bytes(colour) * (core_hi - core_lo)
                else:
                    # A translucent span, a channel at a time through a lookup
                    # table: bytes.translate runs at C speed where a loop over
                    # pixels does not.
                    for c, table in enumerate(_tables(colour, alpha)):
                        s = slice((base + core_lo) * 3 + c, (base + core_hi) * 3, 3)
                        self.px[s] = self.px[s].translate(table)
            # The edges, with their exact coverage.
            for x in list(range(lo, core_lo)) + list(range(core_hi, hi)):
                cover = 0.0
                for s in present:
                    cover += max(0.0, min(s[1], x + 1) - max(s[0], x))
                cover /= SUBROWS
                if cover > 0.0:
                    c = paint(x + 0.5, py + 0.5) if shader else colour
                    self._blend(base + x, c, cover * alpha)

    def _blend(self, i: int, colour: Colour, a: float) -> None:
        j = i * 3
        px = self.px
        if a >= 1.0:
            px[j], px[j + 1], px[j + 2] = colour
            return
        px[j] = round(px[j] + (colour[0] - px[j]) * a)
        px[j + 1] = round(px[j + 1] + (colour[1] - px[j + 1]) * a)
        px[j + 2] = round(px[j + 2] + (colour[2] - px[j + 2]) * a)

    # Conveniences: each shape with the heights it spans.

    def rrect(self, x0, y0, x1, y1, r, paint: Paint, alpha: float = 1.0,
              shader: bool = False) -> None:
        self.fill(rrect(x0, y0, x1, y1, r), y0, y1, paint, alpha, shader)

    def circle(self, cx, cy, r, paint: Paint, alpha: float = 1.0,
               shader: bool = False) -> None:
        self.fill(circle(cx, cy, r), cy - r, cy + r, paint, alpha, shader)

    def polygon(self, points, paint: Paint, alpha: float = 1.0) -> None:
        y0, y1 = bounds(points)
        self.fill(polygon(points), y0, y1, paint, alpha)

    def segment(self, x0, y0, x1, y1, width, paint: Paint, alpha: float = 1.0) -> None:
        pad = width / 2 + 1
        self.fill(segment(x0, y0, x1, y1, width), min(y0, y1) - pad,
                  max(y0, y1) + pad, paint, alpha)

    def ppm(self) -> bytes:
        return b"P6 %d %d 255\n" % (self.w, self.h) + bytes(self.px)

    def tk_data(self) -> str:
        """What a Tk photo image takes as -data: a PNG, base64-encoded. Tk 8.6
        reads PNG data on every platform; its PPM reader does not take -data
        reliably, and refused this one on Windows."""
        from emu.png import RGB, encode_png
        png = encode_png(self.w, self.h, bytes(self.px), RGB, level=1)
        return base64.b64encode(png).decode("ascii")
