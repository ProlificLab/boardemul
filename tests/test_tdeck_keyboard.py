#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The T-Deck keyboard against LilyGO's own sketch for it (ADR 0127).

examples/Keyboard_ESP32C3 is the firmware on the keyboard's ESP32-C3, so each
rule here is a line of it: the two layers, the shifts' arithmetic, Enter and
backspace, Alt+B and Alt+C, the four commands, and raw mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.tdeck_i2c import (  # noqa: E402
    ALT_B_DEFAULT_DUTY, CHORDS, MATRIX, TDeckKeyboard,
)


def read(dev: TDeckKeyboard, count: int = 1) -> bytes:
    dev.start(reading=True)
    return dev.read(count)


def command(dev: TDeckKeyboard, *data: int) -> None:
    """One write transaction, ended by a STOP, the way Wire sends it."""
    dev.start(reading=False)
    dev.write(bytes(data))
    dev.stop()


def press(dev: TDeckKeyboard, *chord: str) -> None:
    """Hold the keys in order and let them go in reverse, as fingers do."""
    for key in chord:
        assert dev.key_event(key, True), key
    for key in reversed(chord):
        dev.key_event(key, False)


def typed(dev: TDeckKeyboard) -> bytes:
    out = b""
    while (byte := read(dev)) != b"\x00":
        out += byte
    return out


# -- what a press sends -----------------------------------------------------


@pytest.mark.parametrize("chord,sent", [
    (("Q",), b"q"),
    (("SYM", "Q"), b"#"),                  # the symbol layer
    (("SYM", "MIC"), b"0"),
    (("LSHIFT", "A"), b"A"),               # char - 32
    (("RSHIFT", "A"), b"A"),
    (("ALT", "Q"), b"q"),                  # ALT is not a layer
    (("LSHIFT", "ENTER"), b"\r"),          # Enter is set after the shift
    (("SYM", "LSHIFT", "T"), b"\x08"),     # '(' - 32
    (("LSHIFT", "MIC"), b"\xe0"),          # (char)(0 - 32), char unsigned
    (("LSHIFT", "SPACE"), b""),            # ' ' - 32 is zero
    (("SYM", "SPACE"), b""),               # no symbol: NULL, zero
    (("SYM", "$"), b""),
    (("ALT", "C"), b"\x0c"),
])
def test_a_chord_sends_what_the_sketch_computes(chord, sent) -> None:
    keys = TDeckKeyboard()
    press(keys, *chord)
    assert typed(keys) == sent


def test_a_layer_lasts_only_while_its_key_is_held() -> None:
    keys = TDeckKeyboard()
    keys.key_event("SYM", True)
    press(keys, "W")
    keys.key_event("SYM", False)
    press(keys, "W")
    assert typed(keys) == b"1w"


def test_pressing_a_held_key_again_is_a_new_press() -> None:
    """A finger cannot press a held key without lifting it, and the drivers
    that send only presses rely on that."""
    keys = TDeckKeyboard()
    keys.key_event("E", True)
    keys.key_event("E", True)
    assert typed(keys) == b"ee"


def test_presses_are_queued_not_overwritten() -> None:
    """The device keeps one byte; the model keeps every press, because the
    host's clock can run ahead of the guest's (ADR 0127)."""
    keys = TDeckKeyboard()
    for key in "HELLO":
        press(keys, key)
    assert typed(keys) == b"hello"


# -- the backlight ----------------------------------------------------------


def test_the_backlight_boots_dark_and_alt_b_toggles_it() -> None:
    keys = TDeckKeyboard()
    assert (keys.lit, keys.pwm) == (False, 0)
    press(keys, "ALT", "B")
    assert (keys.lit, keys.pwm) == (True, ALT_B_DEFAULT_DUTY)
    press(keys, "ALT", "B")
    assert (keys.lit, keys.pwm) == (False, 0)
    assert typed(keys) == b"", "Alt+B sends no character"


def test_alt_b_lights_at_the_duty_the_host_set() -> None:
    keys = TDeckKeyboard()
    command(keys, 0x01, 200)
    assert (keys.lit, keys.pwm) == (True, 200)
    press(keys, "ALT", "B")
    assert (keys.lit, keys.pwm) == (False, 0)
    press(keys, "ALT", "B")
    assert (keys.lit, keys.pwm) == (True, 200)


def test_a_zero_duty_leaves_alt_b_its_own() -> None:
    keys = TDeckKeyboard()
    command(keys, 0x02, 40)
    command(keys, 0x01, 0)
    assert (keys.lit, keys.pwm) == (False, 0)
    press(keys, "ALT", "B")
    assert keys.pwm == 40


def test_alt_b_takes_only_a_duty_above_30() -> None:
    keys = TDeckKeyboard()
    command(keys, 0x02, 30)
    assert keys.alt_b_duty == ALT_B_DEFAULT_DUTY
    command(keys, 0x02, 31)
    assert keys.alt_b_duty == 31


def test_brightness_runs_on_into_alt_b() -> None:
    """0x01 has no break: the byte after its duty is 0x02's."""
    keys = TDeckKeyboard()
    command(keys, 0x01, 50, 96)
    assert (keys.pwm, keys.alt_b_duty) == (50, 96)


def test_a_command_after_brightness_is_swallowed_by_the_fall_through() -> None:
    """Which is why a driver sends brightness on its own."""
    keys = TDeckKeyboard()
    command(keys, 0x01, 50, 0x03)
    assert not keys.raw


# -- raw mode ---------------------------------------------------------------


def test_raw_mode_reads_the_matrix_column_by_column() -> None:
    keys = TDeckKeyboard()
    command(keys, 0x03)
    keys.key_event("Q", True)                  # column 0, row 0
    keys.key_event("K", True)                  # column 4, row 6
    assert read(keys, 5) == bytes([0x01, 0, 0, 0, 0x40])
    keys.key_event("Q", False)
    keys.key_event("K", False)
    assert read(keys, 5) == bytes(5)


def test_a_tap_between_raw_reads_is_seen_once() -> None:
    keys = TDeckKeyboard()
    command(keys, 0x03)
    press(keys, "LSHIFT")                      # column 1, row 6
    assert read(keys, 5) == bytes([0, 0x40, 0, 0, 0])
    assert read(keys, 5) == bytes(5)


def test_key_mode_after_raw_gets_the_latest_press() -> None:
    """The sketch goes on setting its one byte while raw."""
    keys = TDeckKeyboard()
    command(keys, 0x03)
    press(keys, "A")
    press(keys, "B")
    command(keys, 0x04)
    assert typed(keys) == b"b"


# -- typing text ------------------------------------------------------------


def test_text_is_typed_as_the_chords_the_device_needs() -> None:
    keys = TDeckKeyboard()
    assert keys.type_text("emu1 Hi#?\r") == 10
    assert typed(keys) == b"emu1 Hi#?\r"
    assert not keys.held, "every chord is released"


def test_text_the_keyboard_cannot_type_is_skipped() -> None:
    keys = TDeckKeyboard()
    assert keys.type_text("5%") == 1
    assert typed(keys) == b"5"


def test_every_printed_character_has_a_chord() -> None:
    printed = {s for *_cr, _c, s in MATRIX.values() if s}
    letters = {c for *_cr, c, _s in MATRIX.values() if c and c.isalpha()}
    assert printed | letters | {c.upper() for c in letters} | {" ", "$"} <= set(CHORDS)


# -- what the window is told ------------------------------------------------


def test_the_board_state_file_is_replaced_whole(tmp_path) -> None:
    """The window reads the keyboard's backlight from it (ADR 0129), so it is
    renamed into place like latest.png, never written where it is read."""
    import json

    from emu.board_tdeck import write_state

    path = tmp_path / "frames" / "board.json"
    write_state(path, {"keyboard_backlight": 0.498})
    write_state(path, {"keyboard_backlight": 0.0})
    assert json.loads(path.read_text()) == {"keyboard_backlight": 0.0}
    assert [p.name for p in path.parent.iterdir()] == ["board.json"]
