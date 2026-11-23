#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The T-Deck's face, laid out from LilyGO's own sources (ADR 0126).

Millimetres throughout, from the board's top-left corner, x right and y down.
Each number says where it came from:

- [dxf]   shell/T-Deck.dxf in Xinyuan-LilyGO/T-Deck: the main board's drawing,
          whose outline is the uncased device's silhouette (logs/ref/tdeck/).
- [spec]  the product page: a 2.8-inch panel at 320 x 240, so 0.18 mm pixels.
- [photo] LilyGO's product photographs, read against the drawing's 67.06 mm
          width and rescaled to its 98.94 mm height. Good to a few tenths.
- [kbd]   examples/Keyboard_ESP32C3, the keyboard co-processor's sketch: the
          35 keys and each one's second character.

`build(scale)` turns it into canvas pixels: the rendered body, the legends the
window draws as text, and where each key, the trackball and the panel are.
"""

from __future__ import annotations

import math

from emu.face import (  # noqa: F401 -- re-exported for the window and tests
    Face, Text, backspace, cap, legend, ring, shrink, speaker,
)
from emu.raster import Colour, Raster, hex_colour, mix

# 320 px across 57.6 mm [spec]: the panel is exactly 320 x 240 at scale 1.
PX_PER_MM = 50 / 9

WIDTH = 67.06                       # [dxf] dimension 67.059
HEIGHT = 98.94                      # [dxf] dimension 98.941
CORNER = 0.5                        # [dxf] R0.5 at the top corners
SIDES_END = 84.69                   # [dxf] the spline starts 25.80 above the bottom datum
# [dxf] the bottom: a clamped cubic B-spline, in the drawing's own coordinates.
_SPLINE_KNOTS = (0.0, 0.0, 0.0, 0.0, 0.2128769722, 0.5024770381,
                 0.8542979415, 1.0, 1.0, 1.0, 1.0)
_SPLINE_CTRL = ((-1.77396152, 25.7974282801), (-1.7647434984, 19.7584725139),
                (-9.6427328572, 12.7389949312), (-36.4869173294, 10.7311373598),
                (-64.398690346, 12.9247516213), (-68.9682207405, 21.6756967604),
                (-68.96782752, 25.82286428))
_DXF_LEFT, _DXF_TOP = -68.96782752, 110.49    # [dxf] the outline's left edge and top

PANEL = (5.35, 1.30, 5.35 + 57.6, 1.30 + 43.2)     # [spec] size; [photo] place
GLASS = (1.8, 0.0, 66.5, 47.5)                      # [photo] the panel's black glass
STRIP = (0.0, 47.5, WIDTH, 60.6)                    # [photo] the black strip
TRACKBALL = (34.57, 54.36)                          # [dxf] circle centre (-34.40, 56.13)
RING_R, BALL_R = 5.9, 3.7                           # [photo] chrome ring, ball
CHEVRONS_LEFT = (11.3, 23.8)                        # [photo] x span; mirrored right

KEY_W, KEY_H = 5.4, 6.1                             # [photo]
PITCH_X, PITCH_Y = 6.2, 7.87                        # [photo]
ROW_Y = (64.9, 72.6, 80.5, 88.5)                    # [photo] row centres
KEYS_X0 = (WIDTH - (10 * PITCH_X - (PITCH_X - KEY_W))) / 2
PLATE = (1.4, 60.9, WIDTH - 1.4, 84.9)             # [photo] the grey plate, rows 1-3
LOGO_Y = 95.0                                       # [photo] "LILYGO" on the bottom band

# [kbd] each key's name for the emulator, its printed letter or icon, and the
# second character the sketch's symbol layer gives it.
ROWS = (
    (("Q", "#"), ("W", "1"), ("E", "2"), ("R", "3"), ("T", "("),
     ("Y", ")"), ("U", "_"), ("I", "-"), ("O", "+"), ("P", "@")),
    (("A", "*"), ("S", "4"), ("D", "5"), ("F", "6"), ("G", "/"),
     ("H", ":"), ("J", ";"), ("K", "'"), ("L", '"'), ("DEL", "")),
    (("ALT", ""), ("Z", "7"), ("X", "8"), ("C", "9"), ("V", "?"),
     ("B", "!"), ("N", ","), ("M", "."), ("$", ""), ("ENTER", "")),
)
# [photo] the bottom row, symmetric about the middle, in column units.
BOTTOM = (("LSHIFT", 1.00, 1.87), ("MIC", 2.25, 3.12), ("SPACE", 3.45, 6.55),
          ("SYM", 6.88, 7.75), ("RSHIFT", 8.13, 9.00))

C_DESK = hex_colour("#0f1113")
C_BOARD = hex_colour("#131416")
C_RIM = hex_colour("#2c2e31")
C_GLASS = hex_colour("#070809")
C_GLASS_SHEEN = hex_colour("#17191c")
C_STRIP = hex_colour("#0d0e10")
C_CHEVRON = hex_colour("#c9cdd0")
C_TRIM = hex_colour("#8a8f94")
C_PLATE = hex_colour("#8d9296")
C_PLATE_DARK = hex_colour("#6d7276")
C_KEY_TOP = hex_colour("#3b3c3f")
C_KEY_BOTTOM = hex_colour("#26272a")
C_KEY_EDGE = hex_colour("#151617")
C_INK = hex_colour("#f3f4f5")
C_PRINT = hex_colour("#a4a8ac")


def _de_boor(t: float) -> tuple[float, float]:
    k, p = _SPLINE_KNOTS, 3
    span = p
    while span < len(_SPLINE_CTRL) - 1 and t >= k[span + 1]:
        span += 1
    d = [list(_SPLINE_CTRL[span - p + j]) for j in range(p + 1)]
    for r in range(1, p + 1):
        for j in range(p, r - 1, -1):
            i = span - p + j
            denom = k[i + p - r + 1] - k[i]
            a = (t - k[i]) / denom if denom else 0.0
            d[j] = [(1 - a) * d[j - 1][0] + a * d[j][0], (1 - a) * d[j - 1][1] + a * d[j][1]]
    return d[p][0], d[p][1]


def outline(steps: int = 96) -> list[tuple[float, float]]:
    """The board's outline in millimetres, clockwise from the top-left."""
    pts: list[tuple[float, float]] = []
    for cx, cy, a0 in ((CORNER, CORNER, 180.0), (WIDTH - CORNER, CORNER, 270.0)):
        for i in range(5):
            a = math.radians(a0 + 90.0 * i / 4)
            pts.append((cx + CORNER * math.cos(a), cy + CORNER * math.sin(a)))
    for i in range(steps + 1):
        x, y = _de_boor(min(i / steps, 1.0 - 1e-9))
        pts.append((x - _DXF_LEFT, _DXF_TOP - y))
    return pts


def _key_boxes() -> list[tuple[float, float, float, float, str, str]]:
    boxes = []
    for r, row in enumerate(ROWS):
        y0 = ROW_Y[r] - KEY_H / 2
        for c, (name, second) in enumerate(row):
            x0 = KEYS_X0 + c * PITCH_X
            boxes.append((x0, y0, x0 + KEY_W, y0 + KEY_H, name, second))
    y0 = ROW_Y[3] - KEY_H / 2
    for name, c0, c1 in BOTTOM:
        x0 = KEYS_X0 + c0 * PITCH_X
        x1 = KEYS_X0 + c1 * PITCH_X + (KEY_W - 0.87 * PITCH_X)
        boxes.append((x0, y0, x1, y0 + KEY_H, name, "0" if name == "MIC" else ""))
    return boxes


DESK_MM = 4.0                       # room around the body for its shadow


def panel_origin(desk_mm: float = DESK_MM) -> tuple[int, int]:
    """The panel's top-left in canvas pixels at scale 1, a whole pixel."""
    return (round((desk_mm + PANEL[0]) * PX_PER_MM),
            round((desk_mm + PANEL[1]) * PX_PER_MM))


def canvas_size(desk_mm: float = DESK_MM) -> tuple[int, int]:
    return (round((WIDTH + 2 * desk_mm) * PX_PER_MM),
            round((HEIGHT + 2 * desk_mm) * PX_PER_MM))


def build(scale: int, desk_mm: float = DESK_MM) -> Face:
    k = PX_PER_MM * scale
    # The panel's origin lands on a whole pixel at every scale; the rest of the
    # device follows it.
    sx, sy = (v * scale for v in panel_origin(desk_mm))
    ox, oy = sx - PANEL[0] * k, sy - PANEL[1] * k
    width, height = (v * scale for v in canvas_size(desk_mm))

    def X(mm: float) -> float:
        return ox + mm * k

    def Y(mm: float) -> float:
        return oy + mm * k

    def P(points):
        return [(X(x), Y(y)) for x, y in points]

    img = Raster(width, height, C_DESK)
    body = P(outline())

    # A soft contact shadow, then the board with a lit rim.
    for d, a in ((3.0, 0.10), (2.0, 0.14), (1.2, 0.18)):
        img.polygon([(x + d * scale * 0.8, y + d * scale) for x, y in body], (0, 0, 0), a)
    img.polygon(body, C_RIM)
    img.polygon(shrink(body, 0.35 * k), C_BOARD)

    # The panel's black glass, with a faint sheen falling off downwards.
    gx0, gy0, gx1, gy1 = X(GLASS[0]), Y(GLASS[1]), X(GLASS[2]), Y(GLASS[3])
    img.rrect(gx0, gy0, gx1, gy1, 0.6 * k,
              lambda y: mix(C_GLASS_SHEEN, C_GLASS, (y - gy0) / (gy1 - gy0) * 2.2))

    # The strip: chevrons and trim either side of the trackball.
    img.rrect(X(STRIP[0] + 0.4), Y(STRIP[1]), X(STRIP[2] - 0.4), Y(STRIP[3]), 0.2 * k, C_STRIP)
    cy = TRACKBALL[1]
    _chevrons(img, X, Y, k, CHEVRONS_LEFT, cy, +1)
    mirrored = (2 * TRACKBALL[0] - CHEVRONS_LEFT[1], 2 * TRACKBALL[0] - CHEVRONS_LEFT[0])
    _chevrons(img, X, Y, k, mirrored, cy, -1)
    for side in (+1, -1):
        edge = 2.2 if side > 0 else WIDTH - 2.2
        near = CHEVRONS_LEFT[0] - 0.8 if side > 0 else mirrored[1] + 0.8
        img.segment(X(edge), Y(cy - 2.4), X(near - side * 1.6), Y(cy - 2.4), 0.16 * k, C_TRIM)
        img.segment(X(near - side * 1.6), Y(cy - 2.4), X(near), Y(cy - 0.8), 0.16 * k, C_TRIM)
    for i in range(3):                              # [photo] hatch at the right end
        x = WIDTH - 9.2 + i * 0.9
        img.segment(X(x), Y(cy - 3.4), X(x + 1.4), Y(cy - 1.9), 0.14 * k, C_TRIM)

    tx, ty = X(TRACKBALL[0]), Y(TRACKBALL[1])
    _trackball(img, tx, ty, RING_R * k, BALL_R * k)

    # The keyboard: plate, then caps.
    img.rrect(X(PLATE[0]), Y(PLATE[1]), X(PLATE[2]), Y(PLATE[3]), 1.0 * k,
              lambda y: mix(C_PLATE, C_PLATE_DARK, (y - Y(PLATE[1])) / ((PLATE[3] - PLATE[1]) * k)))
    first, last = KEYS_X0 + PITCH_X - 0.5, KEYS_X0 + 9 * PITCH_X - 0.3
    img.rrect(X(first), Y(PLATE[3] - 1.5), X(last), Y(ROW_Y[3] + KEY_H / 2 + 1.0), 1.0 * k,
              lambda y: mix(C_PLATE, C_PLATE_DARK, 0.6 + (y - Y(PLATE[3])) / (8 * k)))
    face = Face(scale, width, height, img, (sx, sy), trackball=(tx, ty, RING_R * k))
    for x0, y0, x1, y1, name, second in _key_boxes():
        cap(img, X(x0), Y(y0), X(x1), Y(y1), k)
        face.keys.append((X(x0), Y(y0), X(x1), Y(y1), name))
        legend(face, img, name, second, X(x0), Y(y0), X(x1), Y(y1), k)

    # Printed marks in the black corners of the bottom row, and the logo.
    for col in (0, 9):
        cx = X(KEYS_X0 + col * PITCH_X + KEY_W / 2)
        ring(img, cx, Y(ROW_Y[3]), 1.25 * k, 0.22 * k, C_PRINT, C_BOARD)
        img.segment(cx - 0.8 * k, Y(ROW_Y[3]) + 0.8 * k, cx + 0.8 * k, Y(ROW_Y[3]) - 0.8 * k,
                    0.22 * k, C_PRINT)
    ly = Y(LOGO_Y)
    face.texts.append(Text(X(WIDTH / 2), ly, "LILYGO®", 2.5 * k, "#d8dbde", bold=True))
    for side in (+1, -1):
        near = WIDTH / 2 - side * 9.6
        far = WIDTH / 2 - side * 21.0
        img.segment(X(far), ly, X(near - side * 0.4), ly, 0.16 * k, C_TRIM)
        tip = near + side * 0.6
        img.polygon([(X(near - side * 0.4), ly - 0.55 * k), (X(tip), ly),
                     (X(near - side * 0.4), ly + 0.55 * k)], C_TRIM)
        ring(img, X(far - side * 0.5), ly, 0.5 * k, 0.15 * k, C_TRIM, C_BOARD)
    return face


def _chevrons(img: Raster, X, Y, k, span, cy, direction) -> None:
    """Three thick chevrons pointing at the trackball, each as two quads."""
    x0, x1 = span
    w = (x1 - x0) / 3
    h, t = 1.75, 1.3                # [photo] half height, stroke along x
    for i in range(3):
        a = x0 + i * w + 0.25
        if direction < 0:
            a = x1 - i * w - 0.25
        tip = a + direction * (w - 0.5)
        back = tip - direction * t
        top = [(X(a), Y(cy - h)), (X(a + direction * t), Y(cy - h)), (X(tip), Y(cy)), (X(back), Y(cy))]
        bottom = [(X(back), Y(cy)), (X(tip), Y(cy)), (X(a + direction * t), Y(cy + h)), (X(a), Y(cy + h))]
        img.polygon(top, C_CHEVRON)
        img.polygon(bottom, C_CHEVRON)


def _trackball(img: Raster, cx: float, cy: float, ring: float, ball: float) -> None:
    img.circle(cx, cy + ring * 0.06, ring * 1.06, (4, 4, 5))
    dark, light, edge = hex_colour("#55595d"), hex_colour("#e8eaec"), hex_colour("#2a2d30")

    def chrome(x: float, y: float) -> Colour:
        # Lit from the top left, as the photographs are, darkening at the rim.
        dx, dy = (x - cx) / ring, (y - cy) / ring
        base = mix(dark, light, 0.5 - 0.5 * (dx + dy) / 1.414)
        return mix(base, edge, max(0.0, math.hypot(dx, dy) - 0.86) * 5)
    img.circle(cx, cy, ring, chrome, shader=True)
    img.circle(cx, cy, ball * 1.12, hex_colour("#1d1f21"))

    def sphere(x: float, y: float) -> Colour:
        dx, dy = (x - cx) / ball, (y - cy) / ball
        spot = math.hypot(dx + 0.38, dy + 0.42)
        return mix(hex_colour("#6a6f74"), hex_colour("#141516"), min(1.0, spot * 1.25))
    img.circle(cx, cy, ball, sphere, shader=True)


def cap(img: Raster, x0, y0, x1, y1, k) -> None:
    r = 0.95 * k
    img.rrect(x0, y0 + 0.18 * k, x1, y1 + 0.18 * k, r, C_KEY_EDGE)
    img.rrect(x0, y0, x1, y1, r, lambda y: mix(C_KEY_TOP, C_KEY_BOTTOM, (y - y0) / (y1 - y0)))
    img.rrect(x0 + 0.35 * k, y0 + 0.12 * k, x1 - 0.35 * k, y0 + 0.30 * k, 0.1 * k,
              hex_colour("#4c4e51"), alpha=0.8)
