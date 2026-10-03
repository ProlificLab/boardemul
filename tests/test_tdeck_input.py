#!/usr/bin/env python3
"""The T-Deck's touch controller and keyboard.

Both are read in ways that split across calls, which is the failure this
project keeps meeting: the traffic looks healthy, the driver discards the
report, and nothing says so. So most of what is here is the same question asked
at every boundary a read can be cut at.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.tdeck_i2c import (  # noqa: E402
    GT911, GT911_MODULE_SWITCH1, GT911_POINT1, GT911_PRODUCT_ID, GT911_STATUS,
    STATUS_READY,
    TDeckKeyboard,
)
from emu.board_tdeck import touch_point  # noqa: E402
from emu.skin import SKINS, TDECK  # noqa: E402


def select(dev: GT911, reg: int, *payload: int) -> None:
    """One register-select write, the way a driver addresses a GT911."""
    dev.start(reading=False)
    dev.write(bytes([reg >> 8, reg & 0xFF, *payload]))


def read(dev, count: int) -> bytes:
    dev.start(reading=True)
    return dev.read(count)


def read_split(dev, count: int, chunk: int) -> bytes:
    """The same logical read, delivered in pieces."""
    dev.start(reading=True)
    out = b""
    while len(out) < count:
        out += dev.read(min(chunk, count - len(out)))
    return out


# -- GT911 ------------------------------------------------------------------


def test_it_identifies_itself_as_a_911() -> None:
    """The driver reads this to decide what it is talking to; the vendor
    firmware prints "Init GT911 Sensor success!" when it matches."""
    touch = GT911()
    select(touch, GT911_PRODUCT_ID)
    assert read(touch, 4) == b"911\x00"


def test_it_reports_its_resolution_where_the_driver_looks() -> None:
    """The vendor driver reads the product-information block, not the config
    block, and printed "Resolution : X = 0 Y = 0" until both were filled in."""
    touch = GT911(width=320, height=240)
    for reg in (0x8146, 0x8048):
        select(touch, reg)
        data = read(touch, 4)
        assert (data[0] | (data[1] << 8)) == 320, hex(reg)
        assert (data[2] | (data[3] << 8)) == 240, hex(reg)


def test_it_reports_how_many_points_it_can_track() -> None:
    """Zero would tell a driver not to bother reading any."""
    touch = GT911()
    select(touch, 0x804C)
    assert read(touch, 1)[0] & 0x0F == 1


def test_an_untouched_panel_reports_ready_with_no_points() -> None:
    touch = GT911()
    select(touch, GT911_STATUS)
    assert read(touch, 1) == bytes([STATUS_READY])


def test_a_touch_is_reported_at_its_coordinates() -> None:
    touch = GT911()
    touch.touch(200, 150)
    select(touch, GT911_STATUS)
    status = read(touch, 1)[0]
    assert status & STATUS_READY
    assert status & 0x0F == 1

    select(touch, GT911_POINT1)
    point = read(touch, 8)
    x = point[1] | (point[2] << 8)
    y = point[3] | (point[4] << 8)
    assert (x, y) == (200, 150)


def test_a_release_clears_the_point() -> None:
    touch = GT911()
    touch.touch(10, 10)
    touch.release()
    select(touch, GT911_STATUS)
    assert read(touch, 1)[0] & 0x0F == 0


def test_the_interrupt_line_idles_high_and_asserts_on_a_finger() -> None:
    """An interrupt with a pull-up on it: idle high, asserted low while there
    is something to report. Both drivers seen work with this, and neither
    writes the mode register, so the model states it (ADR 0031)."""
    touch = GT911()
    assert touch.int_level == 1
    touch.touch(10, 10)
    assert touch.int_level == 0


def test_a_release_is_delivered_before_the_line_goes_idle() -> None:
    """A lift is a report too. Going idle the moment the finger leaves would
    leave a driver that only reads while asserted holding the press for ever
    (ADR 0031)."""
    touch = GT911()
    touch.touch(10, 10)
    touch.release()
    assert touch.int_level == 0, "still asserted: the release has not been read"
    select(touch, GT911_STATUS)
    assert read(touch, 1)[0] & 0x0F == 0
    assert touch.int_level == 1, "read, so now idle"


def test_a_press_and_a_lift_each_give_an_edge() -> None:
    """What an interrupt-driven firmware waits for. A line that never changes
    tells it nothing, which is how the vendor firmware came to read no touch
    at all (ADR 0031)."""
    touch = GT911()
    levels = [touch.int_level]
    touch.touch(10, 10); levels.append(touch.int_level)
    touch.release()
    select(touch, GT911_STATUS); read(touch, 1)
    levels.append(touch.int_level)
    assert levels == [1, 0, 1]


def test_the_mode_register_tells_a_driver_what_the_line_does() -> None:
    """The vendor's driver reads this register and logs it; it must not be
    told rising edge while the line asserts low (ADR 0031)."""
    touch = GT911()
    select(touch, GT911_MODULE_SWITCH1)
    assert read(touch, 1)[0] & 0x03 == 2


def test_a_firmware_that_writes_a_mode_is_followed() -> None:
    """Nothing seen does, but the register is writable and a firmware that
    sets active-high gets active-high (ADR 0029, kept by ADR 0031)."""
    touch = GT911()
    select(touch, GT911_MODULE_SWITCH1, 0x00)
    assert touch.int_mode == 0
    assert touch.int_level == 0, "active high, and nothing to report"
    touch.touch(10, 10)
    assert touch.int_level == 1


def test_a_config_block_write_that_spans_the_mode_register_sets_it() -> None:
    """Drivers write the whole configuration block from 0x8047 in one go, so
    the mode has to be picked out of a longer payload."""
    touch = GT911()
    block = [0x5A, 0x40, 0x01, 0xF0, 0x00, 0x05, 0x03]   # 0x8047..0x804D
    select(touch, 0x8047, *block)
    assert touch.int_mode == 3


def test_the_controller_reports_in_its_own_portrait_raster() -> None:
    """Same way round as the ST7789 behind it, and not the landscape frame the
    firmware draws in (ADR 0030)."""
    touch = GT911()
    assert (touch.width, touch.height) == (240, 320)


def test_the_controller_is_mounted_a_quarter_turn_from_the_display() -> None:
    """A display column becomes the controller's y; a display row becomes its
    x, counted up the screen (ADR 0030)."""
    left, right = touch_point(0, 120), touch_point(319, 120)
    assert right[1] > left[1], "y follows the column, left to right"
    assert left[0] == right[0], "and does not vary along a row"

    top, bottom = touch_point(160, 0), touch_point(160, 239)
    assert top[0] > bottom[0], "x counts up the screen, not down"
    assert top[1] == bottom[1], "and does not vary down a column"


def test_a_tap_survives_the_firmware_s_own_calibration() -> None:
    """Meshtastic maps the controller back through corner points measured on
    real hardware. A tap has to return to where the host aimed it, well inside
    a button two dozen pixels across (ADR 0030). The rows here are the ones
    that discriminate: a target astride the middle of the screen is hit even
    with the axis upside down, which is what hid the direction at first.
    """
    def as_the_firmware_sees(raw_x: int, raw_y: int) -> tuple[int, int]:
        return round((raw_y - 3.5) * 319 / 310.5), round(239 - raw_x * 239 / 223.5)

    for aimed in [(277, 117), (277, 165), (141, 207), (0, 0), (319, 239)]:
        seen = as_the_firmware_sees(*touch_point(*aimed))
        assert abs(seen[0] - aimed[0]) <= 2 and abs(seen[1] - aimed[1]) <= 2, (aimed, seen)


def test_coordinates_are_clamped_to_the_panel() -> None:
    """A viewer bug or a bad scale must not report a finger off the glass."""
    touch = GT911(width=320, height=240)
    touch.touch(9999, -5)
    assert touch.point == (319, 0)


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7])
def test_a_split_report_matches_a_whole_one(chunk: int) -> None:
    """One logical read arrives as several calls. Rebuilding the answer
    part-way through is the bug that cost this project the most time."""
    whole, split = GT911(), GT911()
    for dev in (whole, split):
        dev.touch(123, 45)
        select(dev, GT911_STATUS)
    assert read_split(split, 16, chunk) == read(whole, 16)


def test_the_reply_is_latched_when_the_register_is_chosen() -> None:
    """A touch that arrives mid-read must not change the answer underneath the
    driver, or it gets the status of one moment and the point of another."""
    touch = GT911()
    touch.touch(50, 60)
    select(touch, GT911_POINT1)
    touch.start(reading=True)
    first = touch.read(3)
    touch.touch(200, 200)           # moved, after the read began
    rest = touch.read(5)
    x = first[1] | (first[2] << 8)
    y = rest[0] | (rest[1] << 8)
    assert (x, y) == (50, 60)


def test_reading_past_the_end_pads_rather_than_raising() -> None:
    touch = GT911()
    select(touch, 0x81FE)
    assert len(read(touch, 8)) == 8


# -- the keyboard -----------------------------------------------------------


def test_an_idle_keyboard_reads_zero() -> None:
    """The firmware polls constantly; zero is how it is told nothing happened."""
    assert read(TDeckKeyboard(), 1) == b"\x00"


def test_a_letter_arrives_as_its_character() -> None:
    keys = TDeckKeyboard()
    assert keys.key_event("H", True)
    assert read(keys, 1) == b"h"


@pytest.mark.parametrize("name,char", [
    ("SPACE", b" "), ("ENTER", b"\r"), ("DEL", b"\b"), ("$", b"$"),
])
def test_named_keys_map_to_the_characters_the_firmware_expects(name, char) -> None:
    keys = TDeckKeyboard()
    keys.key_event(name, True)
    assert read(keys, 1) == char


def test_a_modifier_is_accepted_but_sends_nothing() -> None:
    """The modifiers and the microphone key are resolved inside the keyboard's
    own microcontroller, and the trackball is on GPIO rather than this bus.
    All are keys the board has, so the model accepts them; none produces a
    character alone."""
    keys = TDeckKeyboard()
    for name in ("ALT", "SYM", "LSHIFT", "RSHIFT", "MIC", "UP"):
        assert keys.key_event(name, True), name
        assert keys.key_event(name, False), name
    assert read(keys, 1) == bytes(1)


def test_only_the_press_is_reported() -> None:
    """The real keyboard says nothing on release; queueing one would double
    every keystroke."""
    keys = TDeckKeyboard()
    assert keys.key_event("H", False)
    assert read(keys, 1) == b"\x00"


def test_an_unknown_key_sends_nothing() -> None:
    """A cap the board does not have must not invent a character."""
    keys = TDeckKeyboard()
    assert not keys.key_event("F13", True)
    assert read(keys, 1) == b"\x00"


def test_each_keystroke_is_delivered_once() -> None:
    keys = TDeckKeyboard()
    keys.key_event("A", True)
    keys.key_event("B", True)
    assert read(keys, 1) == b"a"
    assert read(keys, 1) == b"b"
    assert read(keys, 1) == b"\x00"


def test_a_split_read_does_not_swallow_a_second_key() -> None:
    """Asking for more bytes than a keystroke must not pop the next one."""
    keys = TDeckKeyboard()
    keys.key_event("A", True)
    keys.key_event("B", True)
    assert read_split(keys, 4, 1) == b"a\x00\x00\x00"
    assert read(keys, 1) == b"b"


# -- the drawn device -------------------------------------------------------


def test_the_tdeck_is_landscape_and_the_max_is_not() -> None:
    assert (TDECK.screen_w, TDECK.screen_h) == (320, 240)
    assert SKINS["tdeck-max"].screen_w < SKINS["tdeck-max"].screen_h


@pytest.mark.parametrize("name", sorted(SKINS))
@pytest.mark.parametrize("scale", [1, 2, 3])
def test_every_skin_maps_its_glass_to_its_panel(name: str, scale: int) -> None:
    skin = SKINS[name]
    assert skin.panel_point(skin.screen_x * scale, skin.screen_y * scale, scale) == (0, 0)
    far_x = (skin.screen_x + skin.screen_w) * scale - 1
    far_y = (skin.screen_y + skin.screen_h) * scale - 1
    assert skin.panel_point(far_x, far_y, scale) == (skin.screen_w - 1,
                                                     skin.screen_h - 1)
    assert skin.panel_point(0, 0, scale) is None


@pytest.mark.parametrize("name", sorted(SKINS))
def test_every_drawn_key_can_reach_that_board(name: str) -> None:
    """A cap that sends nothing is a lie about the hardware. The T-Deck routes
    characters and the Max a matrix, so each has to accept what is drawn."""
    from emu.devices.i2c_devices import TCA8418
    from emu.skin import KEY_NAMES

    labels = [KEY_NAMES.get(label, label)
              for row in SKINS[name].key_rows for label, _span in row]
    if name == "tdeck":
        # Drawn from its own sources, so its keys are the face's (ADR 0126).
        from emu import tdeck_face
        keys = TDeckKeyboard()
        for *_box, label in tdeck_face.build(1).keys:
            assert keys.key_event(label, True), label
    else:
        keymap = TCA8418().keymap
        for label in labels:
            assert label in keymap, label
