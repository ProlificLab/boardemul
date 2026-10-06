#!/usr/bin/env python3
"""The Max's drawn face against LilyGO's own case (ADR 0130).

The outline, the glass recess and the key openings are the case STL's, the
panel is its datasheet's, and the keys are the TCA8418 matrix's. Each test
holds one of those to the drawing the window shows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import max_face  # noqa: E402
from emu.devices.i2c_devices import TCA8418  # noqa: E402
from emu.skin import TDECK_MAX  # noqa: E402


def test_the_panel_is_its_datasheets_size_at_one_pixel_per_pixel() -> None:
    """47.04 x 62.72 mm at a 0.196 mm pixel: 240 x 320 exactly."""
    assert 47.04 * max_face.PX_PER_MM == pytest.approx(240)
    assert 62.72 * max_face.PX_PER_MM == pytest.approx(320)


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_the_panel_is_pixel_exact_at_every_scale(scale: int) -> None:
    """The panel's origin is a whole pixel, and the skin the window maps
    touches with agrees with where the face put it."""
    face = max_face.build(scale)
    assert face.screen == (TDECK_MAX.screen_x * scale, TDECK_MAX.screen_y * scale)
    assert (face.width, face.height) == (TDECK_MAX.canvas_w * scale,
                                         TDECK_MAX.canvas_h * scale)


def test_the_keys_are_the_cases_35_including_both_up_keys() -> None:
    """The case has five openings in the bottom row, and the matrix calls
    both of the outer pair up keys; the old drawing had only one."""
    face = max_face.build(1)
    names = [k[4] for k in face.keys]
    assert len(names) == 35
    assert names.count("UP") == 1 and names.count("UP2") == 1


def test_every_drawn_key_is_in_the_keypads_matrix() -> None:
    """A cap the TCA8418 does not have is a lie about the board, and every
    key the matrix has is reachable -- with no 'only one UP' exception."""
    face = max_face.build(1)
    assert {k[4] for k in face.keys} == set(TCA8418().keymap)


def test_no_two_caps_overlap_and_all_sit_below_the_glass() -> None:
    face = max_face.build(1)
    keys = face.keys
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            apart = a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
            assert apart, (a[4], b[4])
    k = max_face.PX_PER_MM
    top = face.screen[1] - max_face.PANEL[1] * k
    for x0, y0, x1, _y1, name in keys:
        assert y0 > top + max_face.WINDOW[3] * k, name


def test_the_bottom_rows_five_keys_stand_on_their_own_spacing() -> None:
    """Two keys, the space bar, then two keys: the row the STL opens and the
    matrix wires, not the four-key row the old drawing invented."""
    k = max_face.PX_PER_MM
    face = max_face.build(1)
    row = sorted((x0 / k, x1 / k) for x0, _y0, x1, _y1, name in face.keys
                 if name in ("UP", "0", "SPACE", "SYM", "UP2"))
    assert len(row) == 5
    assert [name for *_box, name in sorted(
        (k for k in face.keys if k[4] in ("UP", "0", "SPACE", "SYM", "UP2")),
        key=lambda k: k[0])] == ["UP", "0", "SPACE", "SYM", "UP2"]
    # The space bar spans the device's centre line.
    assert row[2][0] < max_face.WIDTH / 2 < row[2][1]


def test_the_three_touch_keys_are_printed_below_the_active_area() -> None:
    """A heart, a speech bubble and a paper plane on the glass under the
    panel (ADR 0130) -- drawn, not modelled: they are no key the window
    would send, and they lie outside the panel a touch maps to."""
    assert len(max_face.ICONS) == 3
    for _kind, x in max_face.ICONS:
        assert max_face.WINDOW[0] < x < max_face.WINDOW[2]
        assert max_face.PANEL[3] < max_face.ICON_Y - max_face.ICON_S
        assert max_face.ICON_Y + max_face.ICON_S < max_face.WINDOW[3]
    k = max_face.PX_PER_MM
    face = max_face.build(1)
    top = face.screen[1] - max_face.PANEL[1] * k
    for _kind, x in max_face.ICONS:
        box = ((x - max_face.ICON_S) * k, (max_face.ICON_Y - max_face.ICON_S) * k + top,
               (x + max_face.ICON_S) * k, (max_face.ICON_Y + max_face.ICON_S) * k + top)
        for x0, y0, x1, y1, name in face.keys:
            apart = box[2] <= x0 or x1 <= box[0] or box[3] <= y0 or y1 <= box[1]
            assert apart, name


def test_this_board_has_no_trackball() -> None:
    """The old drawing put the T-Deck's trackball on the Max; the Max has
    none, and its face reports none for the window to roll."""
    assert max_face.build(1).trackball == (0.0, 0.0, 0.0)


def test_the_outline_keeps_the_corner_cut() -> None:
    """The cut through the top-right corner: the body's right edge on the
    cut's rows is the cut's own wall, and whole again below it."""
    k = max_face.PX_PER_MM
    face = max_face.build(1)
    ox = face.screen[0] - max_face.PANEL[0] * k
    oy = face.screen[1] - max_face.PANEL[1] * k
    cut = (ox + max_face.NOTCH[0] * k, oy + max_face.NOTCH[1] * k,
           ox + max_face.NOTCH[2] * k, oy + max_face.NOTCH[3] * k,
           max_face.NOTCH[4] * k)
    span = max_face.body_span(ox, oy, ox + max_face.WIDTH * k,
                              oy + max_face.HEIGHT * k,
                              max_face.CORNER_TOP * k, max_face.CORNER_BOTTOM * k,
                              cut)
    on_the_cut = span(oy + 4.0 * k)
    below_it = span(oy + 20.0 * k)
    assert (on_the_cut[1] - ox) / k == pytest.approx(57.8, abs=0.4)
    assert (below_it[1] - ox) / k == pytest.approx(max_face.WIDTH, abs=0.1)
