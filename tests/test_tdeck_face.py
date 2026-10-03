#!/usr/bin/env python3
"""The T-Deck's drawn face against LilyGO's own sources (ADR 0126).

The outline is the board drawing's, the panel is its specification's, and the
keys are the keyboard sketch's. Each test holds one of those to the drawing the
window shows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import tdeck_face  # noqa: E402
from emu.devices.tdeck_i2c import MATRIX  # noqa: E402
from emu.skin import TDECK  # noqa: E402

# examples/Keyboard_ESP32C3: every key with a character, and its symbol layer.
VENDOR_KEYS = set("QWERTYUIOPASDFGHJKLZXCVBNM$") | {
    "SPACE", "ENTER", "DEL", "ALT", "SYM", "LSHIFT", "RSHIFT", "MIC"}
VENDOR_SYMBOLS = {
    "Q": "#", "W": "1", "A": "*", "MIC": "0", "E": "2", "S": "4", "D": "5",
    "P": "@", "X": "8", "Z": "7", "R": "3", "G": "/", "T": "(", "V": "?",
    "C": "9", "F": "6", "U": "_", "H": ":", "Y": ")", "B": "!", "N": ",",
    "J": ";", "O": "+", "L": '"', "I": "-", "M": ".", "K": "'"}


def test_the_outline_is_the_drawings_size() -> None:
    """67.06 x 98.94 mm, the drawing's two overall dimensions.

    The drawing's bottom spline starts 0.13 mm right of the edge its width
    dimension is taken from (x -1.774 against -1.909), and the outline keeps
    the drawing's spline as drawn."""
    xs = [x for x, _y in tdeck_face.outline()]
    ys = [y for _x, y in tdeck_face.outline()]
    assert min(xs) == pytest.approx(0.0, abs=0.02)
    assert max(xs) == pytest.approx(67.06, abs=0.15)
    assert min(ys) == pytest.approx(0.0, abs=0.02)
    assert max(ys) == pytest.approx(98.94, abs=0.05)


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_the_panel_is_pixel_exact_at_every_scale(scale: int) -> None:
    """The panel's origin is a whole pixel, and the skin the window maps
    touches with agrees with where the face put it."""
    face = tdeck_face.build(scale)
    assert face.screen == (TDECK.screen_x * scale, TDECK.screen_y * scale)
    assert (face.width, face.height) == (TDECK.canvas_w * scale, TDECK.canvas_h * scale)
    assert 57.6 * tdeck_face.PX_PER_MM == pytest.approx(320)
    assert 43.2 * tdeck_face.PX_PER_MM == pytest.approx(240)


def test_the_keys_are_the_vendors_35() -> None:
    face = tdeck_face.build(1)
    names = [k[4] for k in face.keys]
    assert len(names) == 35 and set(names) == VENDOR_KEYS


def test_every_key_is_in_the_keyboards_matrix() -> None:
    """A cap that the model does not have is a lie about the board."""
    assert VENDOR_KEYS == set(MATRIX)


def test_each_cap_prints_the_sketchs_second_character() -> None:
    printed = {}
    for row in tdeck_face.ROWS:
        for name, second in row:
            if second:
                printed[name] = second
    printed["MIC"] = "0"
    assert printed == VENDOR_SYMBOLS
    assert printed == {n: s for n, (_c, _r, _ch, s) in MATRIX.items() if s}


def test_no_two_caps_overlap_and_all_sit_on_the_body() -> None:
    face = tdeck_face.build(1)
    keys = face.keys
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            apart = a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
            assert apart, (a[4], b[4])
    k = tdeck_face.PX_PER_MM
    left = face.screen[0] - tdeck_face.PANEL[0] * k
    right = left + tdeck_face.WIDTH * k
    for x0, _y0, x1, _y1, name in keys:
        assert left < x0 and x1 < right, name


def test_the_trackball_is_where_the_drawing_puts_it() -> None:
    """Centred on the drawing's Ø15.39 and Ø20.26 circles."""
    face = tdeck_face.build(1)
    k = tdeck_face.PX_PER_MM
    left = face.screen[0] - tdeck_face.PANEL[0] * k
    top = face.screen[1] - tdeck_face.PANEL[1] * k
    cx, cy, _r = face.trackball
    assert (cx - left) / k == pytest.approx(34.57, abs=0.01)
    assert (cy - top) / k == pytest.approx(54.36, abs=0.01)
