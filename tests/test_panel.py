#!/usr/bin/env python3
"""Panel tests: what the firmware sends versus what ends up in the PNG."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.epaper import (  # noqa: E402
    FRAME_BYTES, HEIGHT, WIDTH, EPaperPanel,
)
from emu.png import write_png  # noqa: E402
from tests.support import read_png  # noqa: E402

CMD_OLD_PLANE = 0x10
CMD_NEW_PLANE = 0x13
CMD_REFRESH = 0x12


def panel(tmp_path: Path, quarter_turns: int = 0) -> EPaperPanel:
    return EPaperPanel(tmp_path / "frames", quarter_turns=quarter_turns)


def send_frame(p: EPaperPanel, fill: int, command: int = CMD_NEW_PLANE) -> None:
    p.on_segment(0, bytes([command]), False)
    p.on_segment(1, bytes([fill]) * FRAME_BYTES, False)


def test_all_ones_is_a_blank_screen(tmp_path: Path) -> None:
    p = panel(tmp_path)
    send_frame(p, 0xFF)
    assert p.frames == 1
    width, height, pixels = read_png(p.latest)
    assert (width, height) == (WIDTH, HEIGHT)
    assert set(pixels) == {0xFF}, "a set bit is white"


def test_all_zeros_is_a_full_black_screen(tmp_path: Path) -> None:
    p = panel(tmp_path)
    send_frame(p, 0x00)
    _, _, pixels = read_png(p.latest)
    assert set(pixels) == {0x00}


def test_bit_order_is_most_significant_first(tmp_path: Path) -> None:
    """0x80 sets only the leftmost pixel of each byte, so every eighth column
    is white and the rest black. Getting this backwards mirrors the image in
    eight-pixel blocks, which is subtle enough to miss by eye."""
    p = panel(tmp_path)
    send_frame(p, 0x80)
    width, _, pixels = read_png(p.latest)
    row = pixels[:width]
    assert row[0] == 0xFF
    assert set(row[1:8]) == {0x00}
    assert row[8] == 0xFF


def test_previous_image_plane_is_not_displayed(tmp_path: Path) -> None:
    """The controller takes two planes; only the new one reaches the screen.
    Rendering the old one shows the previous frame instead of this one."""
    p = panel(tmp_path)
    p.on_segment(0, bytes([CMD_OLD_PLANE]), False)
    p.on_segment(1, bytes([0x00]) * FRAME_BYTES, False)
    assert p.frames == 0


def test_refresh_command_renders_a_partial_frame(tmp_path: Path) -> None:
    p = panel(tmp_path)
    p.on_segment(0, bytes([CMD_NEW_PLANE]), False)
    p.on_segment(1, bytes([0x00]) * (FRAME_BYTES // 2), False)
    assert p.frames == 0, "half a frame is not yet a picture"
    p.on_segment(0, bytes([CMD_REFRESH]), False)
    assert p.frames == 1


def test_command_and_data_are_told_apart_by_dc(tmp_path: Path) -> None:
    """dc low is a command; the same byte on dc high is payload."""
    p = panel(tmp_path)
    p.on_segment(0, bytes([CMD_NEW_PLANE]), False)
    assert p.commands == [CMD_NEW_PLANE]
    p.on_segment(1, bytes([CMD_NEW_PLANE]) * 4, False)
    assert p.commands == [CMD_NEW_PLANE], "payload must not be read as commands"


@pytest.mark.parametrize("turns,shape", [(0, (WIDTH, HEIGHT)),
                                         (1, (HEIGHT, WIDTH)),
                                         (2, (WIDTH, HEIGHT)),
                                         (3, (HEIGHT, WIDTH))])
def test_rotation_changes_the_output_shape(tmp_path: Path, turns: int,
                                           shape: tuple[int, int]) -> None:
    p = panel(tmp_path, quarter_turns=turns)
    send_frame(p, 0xFF)
    width, height, _ = read_png(p.latest)
    assert (width, height) == shape


def test_panel_answers_a_duplex_read_with_zeros(tmp_path: Path) -> None:
    """It has no MISO line, so a full-duplex transfer reads back nothing."""
    p = panel(tmp_path)
    assert p.on_segment(1, bytes(4), duplex=True) == bytes(4)


def test_png_round_trips(tmp_path: Path) -> None:
    """The writer is ours and the reader is only used by tests, so this is the
    one place they are checked against each other."""
    pixels = bytes(range(256)) * 4
    path = tmp_path / "probe.png"
    write_png(path, 64, 16, pixels)
    assert read_png(path) == (64, 16, pixels)
