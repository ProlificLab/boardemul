#!/usr/bin/env python3
"""The T-Deck Max's face, laid out from LilyGO's own case (ADR 0130).

Millimetres throughout, from the device's top-left corner, x right and y down.
Each number says where it came from:

- [stl]   T-Deck-MAX-Case-Files, `surface-covre.stl`: the front of the case
          (logs/ref/tdeck-max/). The model's x is the device's height and its
          z the width; its +z is the device's right, read from the corner cut
          against the photographs.
- [spec]  the GDEQ031T10 datasheet: a 47.04 x 62.72 mm active area at a
          0.196 mm square pixel.
- [photo] LilyGO's product photographs, read against the case's scale. Good
          to a few tenths of a millimetre.
- [kbd]   the vendor's factory keypad (ADR 0126's peri_keypad.cpp): which
          keys the bottom row's five openings are.

`build(scale)` turns it into canvas pixels: the rendered body, the legends
the window draws as text, and where each key and the panel are. The cap and
legend drawings are `emu/face.py`'s, shared with the T-Deck.
"""

from __future__ import annotations

import math

from emu.face import Face, Text, cap, legend, ring
from emu.raster import Raster, hex_colour, mix, rrect

# 240 px across 47.04 mm [spec]: the panel is exactly 240 x 320 at scale 1.
PX_PER_MM = 250 / 49

WIDTH = 65.81                        # [stl] the front's outline
HEIGHT = 120.12
CORNER_TOP = 6.8                     # [stl]
CORNER_BOTTOM = 28.0                 # [stl]
# [stl] the touch glass recess, from 1 mm below the top edge, and the cut
# through the top-right corner as a rounded rectangle overlapping the edge.
WINDOW = (0.91, 1.02, 64.79, 80.40)
WINDOW_R = 5.8                       # [stl] 5.9 top, 5.7 bottom
NOTCH = (57.5, -1.5, 67.8, 8.1, 3.5)
# [spec] the active area's 47.04 x 62.72; [photo] centred across the window
# and ending 6 mm above its bottom edge, where the e-paper's content stops.
PANEL = (9.33, 11.58, 9.33 + 47.04, 11.58 + 62.72)

# [photo] three touch keys printed on the glass below the active area: a
# heart, a speech bubble and a paper plane, spread wide rather than centred.
ICONS = (("heart", 5.2), ("bubble", 32.9), ("plane", 60.6))
ICON_Y = 77.0
ICON_S = 2.0                         # half size

# [stl] the key openings: ten columns at a 6.24 mm pitch, three rows of
# 6.3 mm-tall caps, and a five-key bottom row staggered half a pitch.
KEY_W, KEY_H = 5.0, 6.3
PITCH_X = 6.235
COL0 = 4.70
ROW_Y = (85.28, 92.96, 100.69, 107.85)
# [kbd] each opening's key, from the matrix's column order.
BOTTOM = (("UP", 8.5, 14.1), ("0", 16.6, 22.2), ("SPACE", 24.2, 41.6),
          ("SYM", 43.0, 48.6), ("UP2", 51.0, 56.6))
BOTTOM_H = 5.6
PLATE = (1.6, 81.3, 64.2, 111.1)     # [photo] the plate the caps sit in
LOGO_Y = 115.6                       # [photo] the bottom band's print

C_DESK = hex_colour("#0f1113")
C_EDGE = hex_colour("#0a0b0c")
C_RIM = hex_colour("#34373b")
C_BODY = hex_colour("#1d1f22")
C_BODY_LOW = hex_colour("#1a1c1f")
C_GLASS = hex_colour("#7c7f81")      # [photo] the light grey touch glass
C_GLASS_LOW = hex_colour("#6e7173")
C_GLASS_WALL = hex_colour("#4a4d4f")
C_ICON = hex_colour("#43464a")
C_PLATE = hex_colour("#4e5154")      # [photo] the smoky plate
C_PLATE_DARK = hex_colour("#43464a")
C_PRINT = hex_colour("#b9bdc0")


def _rrect2(x0, y0, x1, y1, rt, rb):
    """A rounded rectangle whose top and bottom corners differ (the outline)."""
    def span(y: float):
        if y < y0 or y >= y1:
            return None
        inset = 0.0
        if y < y0 + rt:
            d = y0 + rt - y
            inset = rt - math.sqrt(max(0.0, rt * rt - d * d))
        elif y > y1 - rb:
            d = y - (y1 - rb)
            inset = rb - math.sqrt(max(0.0, rb * rb - d * d))
        return x0 + inset, x1 - inset
    return span


def body_span(ox, oy, x1, y1, rt, rb, cut, inset=0.0):
    """The outline as one interval per row: a two-radius rounded rectangle
    with the corner cut bitten out of its right edge (ADR 0130). The cut
    crosses the edge, so what it leaves is still a single interval."""
    d = inset
    base = _rrect2(ox + d, oy + d, x1 - d, y1 - d, rt - d, rb - d)
    cx0, cy0, cx1, cy1, cr = cut
    bite = rrect(cx0 + d, cy0 + d, cx1 - d, cy1 - d, cr - d)

    def span(y: float):
        s = base(y)
        if s is None:
            return None
        right = s[1]
        if y < cy1 - d:
            b = bite(y)
            if b is not None:
                right = min(right, b[0])
        return (s[0], right) if right > s[0] else None
    return span


DESK_MM = 4.0                        # room around the body for its shadow


def panel_origin(desk_mm: float = DESK_MM) -> tuple[int, int]:
    """The panel's top-left in canvas pixels at scale 1, a whole pixel."""
    return (round((desk_mm + PANEL[0]) * PX_PER_MM),
            round((desk_mm + PANEL[1]) * PX_PER_MM))


def canvas_size(desk_mm: float = DESK_MM) -> tuple[int, int]:
    return (round((WIDTH + 2 * desk_mm) * PX_PER_MM),
            round((HEIGHT + 2 * desk_mm) * PX_PER_MM))


def _key_boxes():
    """(x0, y0, x1, y1, name, printed) in millimetres; `printed` is the cap
    the legend shows, which differs from the key's name on the microphone."""
    boxes = []
    from emu.tdeck_face import ROWS          # the legends print the same [photo]
    for r, row in enumerate(ROWS):
        y0 = ROW_Y[r] - KEY_H / 2
        for c, (name, _second) in enumerate(row):
            x0 = COL0 + c * PITCH_X - KEY_W / 2
            boxes.append((x0, y0, x0 + KEY_W, y0 + KEY_H, name, name))
    y0 = ROW_Y[3] - BOTTOM_H / 2
    for name, bx0, bx1 in BOTTOM:
        printed = "MIC" if name == "0" else name
        boxes.append((bx0, y0, bx1, y0 + BOTTOM_H, name, printed))
    return boxes


def build(scale: int, desk_mm: float = DESK_MM) -> Face:
    k = PX_PER_MM * scale
    sx, sy = (v * scale for v in panel_origin(desk_mm))
    ox, oy = sx - PANEL[0] * k, sy - PANEL[1] * k
    width, height = (v * scale for v in canvas_size(desk_mm))

    def X(mm: float) -> float:
        return ox + mm * k

    def Y(mm: float) -> float:
        return oy + mm * k

    img = Raster(width, height, C_DESK)
    bx0, by0, bx1, by1 = X(0), Y(0), X(WIDTH), Y(HEIGHT)
    rt, rb = CORNER_TOP * k, CORNER_BOTTOM * k
    cut = (X(NOTCH[0]), Y(NOTCH[1]), X(NOTCH[2]), Y(NOTCH[3]), NOTCH[4] * k)

    # A soft contact shadow, then the body with a lit rim. Both follow the
    # outline's span, so the corner cut is missing from the shadow too.
    for d, a in ((3.0, 0.10), (2.0, 0.14), (1.2, 0.18)):
        span = body_span(bx0 + d * k * 0.8, by0 + d * k, bx1 + d * k * 0.8,
                         by1 + d * k, rt, rb, cut)
        img.fill(span, by0 + d * k, by1 + d * k, (0, 0, 0), alpha=a)
    outline = body_span(bx0, by0, bx1, by1, rt, rb, cut)
    img.fill(outline, by0, by1, C_RIM)
    inner = body_span(bx0, by0, bx1, by1, rt, rb, cut, inset=0.35 * k)
    img.fill(inner, by0 + 0.35 * k, by1 - 0.35 * k,
             lambda y: mix(C_BODY, C_BODY_LOW, (y - by0) / (by1 - by0)))

    # The touch glass fills the recess: a wall, then the light grey pane.
    img.rrect(X(WINDOW[0]), Y(WINDOW[1]), X(WINDOW[2]), Y(WINDOW[3]),
              WINDOW_R * k, C_GLASS_WALL)
    img.rrect(X(WINDOW[0] + 0.35), Y(WINDOW[1] + 0.35), X(WINDOW[2] - 0.35),
              Y(WINDOW[3] - 0.35), (WINDOW_R - 0.3) * k,
              lambda y: mix(C_GLASS, C_GLASS_LOW,
                            (y - Y(WINDOW[1])) / ((WINDOW[3] - WINDOW[1]) * k)))
    # The e-paper sits behind the glass; its edge reads as a frame.
    img.rrect(X(PANEL[0] - 0.3), Y(PANEL[1] - 0.3), X(PANEL[2] + 0.3),
              Y(PANEL[3] + 0.3), 0.2 * k, C_GLASS_WALL)
    img.rrect(X(PANEL[0]), Y(PANEL[1]), X(PANEL[2]), Y(PANEL[3]), 0.1 * k,
              C_GLASS_LOW)
    # The glass cannot cross the cut; the hole is repainted over its corner.
    img.rrect(X(NOTCH[0] + 0.6), Y(0.2), bx1, Y(NOTCH[3] - 0.2), 3.0 * k, C_EDGE)
    ring(img, X(63.2), Y(3.6), 1.5 * k, 0.3 * k, hex_colour("#3a3e42"), C_EDGE)
    img.circle(X(63.2), Y(3.6), 0.6 * k, hex_colour("#7d8489"))

    face = Face(scale, width, height, img, (sx, sy))

    # The three touch keys, printed on the glass [photo]. They are drawn,
    # not modelled: a click here is neither a touch nor a key (ADR 0130).
    for kind, ix in ICONS:
        cx, cy = X(ix), Y(ICON_Y)
        if kind == "heart":
            img.circle(cx - 0.9 * k, cy - 0.5 * k, 1.0 * k, C_ICON)
            img.circle(cx + 0.9 * k, cy - 0.5 * k, 1.0 * k, C_ICON)
            img.polygon([(cx - 1.8 * k, cy - 0.1 * k), (cx + 1.8 * k, cy - 0.1 * k),
                         (cx, cy + 1.85 * k)], C_ICON)
        elif kind == "bubble":
            img.rrect(cx - 1.9 * k, cy - 1.5 * k, cx + 1.9 * k, cy + 0.9 * k,
                      0.9 * k, C_ICON)
            img.polygon([(cx - 1.1 * k, cy + 0.7 * k), (cx - 0.1 * k, cy + 0.7 * k),
                         (cx - 1.1 * k, cy + 1.9 * k)], C_ICON)
            for dx in (-0.95, 0.0, 0.95):
                img.circle(cx + dx * k, cy - 0.3 * k, 0.28 * k, C_GLASS)
        else:
            img.polygon([(cx - 2.0 * k, cy - 1.1 * k), (cx + 2.0 * k, cy + 0.1 * k),
                         (cx - 2.0 * k, cy + 0.9 * k)], C_ICON)
            img.polygon([(cx - 2.0 * k, cy + 0.9 * k), (cx + 2.0 * k, cy + 0.1 * k),
                         (cx + 0.4 * k, cy + 1.5 * k)], C_ICON)

    # The keyboard: plate, then caps with their legends.
    img.rrect(X(PLATE[0]), Y(PLATE[1]), X(PLATE[2]), Y(PLATE[3]), 2.5 * k,
              lambda y: mix(C_PLATE, C_PLATE_DARK,
                            (y - Y(PLATE[1])) / ((PLATE[3] - PLATE[1]) * k)))
    for x0, y0, x1, y1, name, printed in _key_boxes():
        cap(img, X(x0), Y(y0), X(x1), Y(y1), k)
        face.keys.append((X(x0), Y(y0), X(x1), Y(y1), name))
        legend(face, img, printed, _second_char(printed),
               X(x0), Y(y0), X(x1), Y(y1), k)

    # The bottom band: the logo between arrowed lines, with a trace either
    # side and hatching towards the ends, as the photographs print it.
    ly = Y(LOGO_Y)
    face.texts.append(Text(X(WIDTH / 2), ly, "LILYGO®", 2.6 * k, "#c9cdd0", bold=True))
    for side in (+1, -1):
        near = WIDTH / 2 - side * 11.5
        far = WIDTH / 2 - side * 23.0
        img.segment(X(far), ly, X(near - side * 0.4), ly, 0.16 * k, C_PRINT)
        tip = near + side * 0.6
        img.polygon([(X(near - side * 0.4), ly - 0.55 * k), (X(tip), ly),
                     (X(near - side * 0.4), ly + 0.55 * k)], C_PRINT)
        pad = far - side * 2.2
        img.rrect(X(pad) - 0.4 * k, ly - 0.4 * k, X(pad) + 0.4 * k, ly + 0.4 * k,
                  0.1 * k, C_PRINT)
        img.segment(X(pad - side * 0.4), ly, X(pad - side * 1.6), ly - 0.8 * k,
                    0.14 * k, C_PRINT)
        img.segment(X(pad - side * 0.4), ly, X(pad - side * 1.6), ly + 0.8 * k,
                    0.14 * k, C_PRINT)
    for side in (+1, -1):
        for i in range(3):
            hx = WIDTH / 2 + side * (25.5 + i * 1.5)
            img.segment(X(hx), ly + 1.6 * k, X(hx + side * 1.0), ly + 2.5 * k,
                        0.13 * k, C_PRINT)
    return face


def _second_char(printed: str) -> str:
    """The cap's second character, from the T-Deck's sketch by way of the
    photographs' reading that the legends print the same (ADR 0130)."""
    from emu.tdeck_face import ROWS
    for row in ROWS:
        for name, second in row:
            if name == printed:
                return second
    return ""
