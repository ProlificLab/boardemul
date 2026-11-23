#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""How the window presses the T-Deck's keys (ADR 0127, decision 7).

The host's keyboard types characters, which the window presses as the chords
the device needs; a mouse latches modifiers to hold two keys at once. Each
test feeds what the window sends into the keyboard model and reads back what
the firmware would receive.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.tdeck_i2c import TDeckKeyboard  # noqa: E402
from emu.skin import TDeckTyping  # noqa: E402


class Rig:
    def __init__(self) -> None:
        self.keyboard = TDeckKeyboard()
        self.sent: list[tuple[str, bool]] = []
        self.typing = TDeckTyping(self._emit)

    def _emit(self, key: str, down: bool) -> None:
        self.sent.append((key, down))
        assert self.keyboard.key_event(key, down), key

    def host(self, keysym: str, char: str = "", keycode: int = 0) -> None:
        """One host key pressed and released."""
        self.typing.host_press(keysym, char, keycode)
        self.typing.host_release(keycode)

    def received(self) -> bytes:
        out = b""
        while True:
            self.keyboard.start(reading=True)
            byte = self.keyboard.read(1)
            if byte == b"\x00":
                return out
            out += byte


def test_a_host_letter_is_its_key() -> None:
    rig = Rig()
    rig.host("a", "a", 65)
    assert rig.sent == [("A", True), ("A", False)]
    assert rig.received() == b"a"


def test_a_capital_is_typed_with_the_devices_shift_not_the_hosts() -> None:
    rig = Rig()
    rig.typing.host_press("Shift_L", "", 16)
    rig.host("A", "A", 65)
    rig.typing.host_release(16)
    assert rig.sent == [("LSHIFT", True), ("A", True), ("A", False), ("LSHIFT", False)]
    assert rig.received() == b"A"


def test_a_printed_second_character_is_typed_with_sym() -> None:
    rig = Rig()
    for keysym, char, code in (("numbersign", "#", 51), ("1", "1", 49),
                               ("question", "?", 191), ("at", "@", 50)):
        rig.host(keysym, char, code)
    assert rig.received() == b"#1?@"
    assert not rig.typing.down


def test_ctrl_is_sym_and_alt_is_alt() -> None:
    """Ctrl+Q types '#' and Alt+B toggles the backlight, as on the device."""
    rig = Rig()
    rig.typing.host_press("Control_L", "", 17)
    rig.host("q", "\x11", 81)
    rig.typing.host_release(17)
    rig.typing.host_press("Alt_L", "", 18)
    rig.host("b", "b", 66)
    rig.typing.host_release(18)
    assert rig.received() == b"#"
    assert rig.keyboard.lit


def test_a_chord_under_a_held_ctrl_does_not_let_sym_go() -> None:
    """Counted: the chord for '1' needs SYM, which Ctrl already holds."""
    rig = Rig()
    rig.typing.host_press("Control_L", "", 17)
    rig.host("1", "1", 49)
    assert ("SYM", False) not in rig.sent
    rig.typing.host_release(17)
    assert rig.sent[-1] == ("SYM", False)


def test_host_auto_repeat_is_ignored() -> None:
    rig = Rig()
    for _ in range(5):
        rig.typing.host_press("e", "e", 69)
    rig.typing.host_release(69)
    assert rig.received() == b"e"


def test_losing_focus_lets_every_host_key_go() -> None:
    rig = Rig()
    rig.typing.host_press("Control_L", "", 17)
    rig.typing.host_press("q", "\x11", 81)
    rig.typing.host_release_all()
    assert not rig.typing.down and not rig.keyboard.held


def test_keys_the_device_lacks_are_not_sent() -> None:
    rig = Rig()
    for keysym, char in (("percent", "%"), ("Tab", "\t"), ("F1", ""), ("Shift_R", "")):
        rig.host(keysym, char, 1)
    assert rig.sent == []


def test_a_clicked_modifier_latches_until_the_next_key() -> None:
    rig = Rig()
    rig.typing.click_press("SYM")
    rig.typing.click_release("SYM")
    assert rig.typing.latched == ["SYM"] and rig.typing.down == {"SYM"}
    rig.typing.click_press("Q")
    rig.typing.click_release("Q")
    assert rig.sent == [("SYM", True), ("Q", True), ("Q", False), ("SYM", False)]
    assert not rig.typing.latched
    assert rig.received() == b"#"


def test_clicking_a_latched_modifier_again_lets_it_go() -> None:
    rig = Rig()
    rig.typing.click_press("ALT")
    rig.typing.click_press("ALT")
    assert rig.sent == [("ALT", True), ("ALT", False)]
    assert not rig.typing.latched


def test_latches_combine() -> None:
    """SYM and a shift latched, then T: '(' - 32, a backspace."""
    rig = Rig()
    for key in ("SYM", "RSHIFT", "T"):
        rig.typing.click_press(key)
        rig.typing.click_release(key)
    assert rig.received() == b"\x08"
    assert not rig.typing.down


def test_a_host_key_also_uses_up_a_latch() -> None:
    rig = Rig()
    rig.typing.click_press("SYM")
    rig.host("w", "w", 87)
    rig.host("w", "w", 87)
    assert rig.received() == b"1w"
