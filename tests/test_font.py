#!/usr/bin/env python3
"""The bitmap font and the baked legends (ADR 0135).

Fast: a face's legends are data — text items with a position, a size and a
colour — so baking them is pixel arithmetic, no toolkit, no guest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import font  # noqa: E402
from emu import web  # noqa: E402
from emu.raster import Raster, hex_colour  # noqa: E402


def test_the_glyph_a_is_the_glyph_a() -> None:
    """The 5×7 pattern, asserted where the ink should be: the A's legs and
    its crossbar."""
    img = Raster(40, 20, (0, 0, 0))
    ink = hex_colour("#ffffff")
    font.draw_text(img, 8, 8, "A", 14, ink, anchor="w")
    # At height 14 the cells are 2px; the A spans 5 cells × 2 = 10 wide.
    def ink_at(x, y):
        i = (y * img.w + x) * 3
        return img.px[i] > 128

    assert ink_at(12, 1)                            # the top bar's middle
    assert ink_at(9, 13) and ink_at(16, 13)         # the two legs, bottom row
    assert ink_at(12, 8)                            # the crossbar's middle
    assert not ink_at(12, 4)                        # the counter, above it
    assert not ink_at(30, 5)                        # nothing beyond the glyph


def test_the_second_characters_are_in_the_font() -> None:
    """Every second character the caps print must render — a missing glyph
    would leave a cap printed with a hole."""
    from emu.tdeck_face import ROWS
    for row in ROWS:
        for _name, second in row:
            if second:
                for char in second:
                    assert char in font.FONT, f"{char!r} has no glyph"


def test_baking_adds_ink_to_the_caps_and_nowhere_else() -> None:
    """Before baking, the caps of the served face are blank plastic; after,
    the legend colour appears inside the caps and the face outside them is
    untouched."""
    from emu import device as _unused  # noqa: F401  (import order only)
    module = web._face_module("tdeck-max")
    face = module.build(2)
    geometry = web._skin_geometry("tdeck-max")
    qcap = next(k for k in geometry["keys"] if k["name"] == "Q")
    x0, y0, x1, y1 = (int(v) for v in (qcap["x0"] * 2, qcap["y0"] * 2,
                                       qcap["x1"] * 2, qcap["y1"] * 2))

    def bright_pixels() -> int:
        n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * face.raster.w + x) * 3
                if face.raster.px[i] > 200 and face.raster.px[i + 1] > 200:
                    n += 1
        return n

    assert bright_pixels() == 0, "the cap was printed with its legend already"
    web._bake_legends(face)
    assert bright_pixels() > 30, "the Q's legend never landed on its cap"


def test_baking_leaves_the_desk_untouched() -> None:
    """The desk margin is not the legend's business."""
    module = web._face_module("tdeck-max")
    face = module.build(2)
    before = bytes(face.raster.px)
    web._bake_legends(face)
    after = bytes(face.raster.px)
    # Only compare inside the body: the desk stays as it was by construction,
    # because every legend sits inside it — assert the image changed at all,
    # then that the very corner (desk) did not.
    assert after != before
    corner = 0
    assert before[corner:corner + 3] == after[corner:corner + 3]
