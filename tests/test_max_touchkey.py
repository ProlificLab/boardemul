#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The Max's three glass touch keys, as the vendor's own driver reads them.

The report format, the key numbering and the press value are held to the
conditions in LilyGO's hyn_cst3xx.c rather than restated: if the model's bytes
stop satisfying the driver's parsing, these fail (ADR 0131).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.i2c_devices import CST328  # noqa: E402
from emu.inputs import InputTail  # noqa: E402
from emu.skin import InputWriter  # noqa: E402


def select(dev: CST328, reg: int, *payload: int) -> None:
    dev.start(reading=False)
    dev.write(reg.to_bytes(2, "big") + bytes(payload))
    dev.start(reading=True)


def report(dev: CST328) -> bytes:
    select(dev, CST328.REG_REPORT)
    return dev.read(7)


def driver_key_id(buf: bytes) -> int:
    """The driver's own recovery: one-based, in buf[1]'s high nibble."""
    return (buf[1] >> 4) - 1


def test_chip_info_reports_three_keys() -> None:
    """cst3xx_updata_tpinfo reads fw_key_num from byte 3; the wrapper keeps
    three keys and answers nothing beyond them."""
    touch = CST328()
    select(touch, CST328.REG_CHIP_INFO)
    assert touch.read(28)[3] == 3 == CST328.KEYS


@pytest.mark.parametrize("key_id", [0, 1, 2])
def test_a_key_press_satisfies_the_driver(key_id: int) -> None:
    """Every branch condition in the driver's key path, on our bytes."""
    touch = CST328()
    touch.key = (key_id, True)
    buf = report(touch)
    assert buf[6] == 0xAB                       # the magic the read is checked on
    assert buf[5] & 0x80 and buf[5] == 0x80     # a key alone, no fingers
    assert buf[5] & 0x7F == 0
    assert driver_key_id(buf) == key_id
    assert buf[0] == 0x83                       # exactly this is a press


@pytest.mark.parametrize("key_id", [0, 1, 2])
def test_a_key_release_is_not_the_press_value(key_id: int) -> None:
    """Bit 7 set and not 0x83: everything the driver needs of a release."""
    touch = CST328()
    touch.key = (key_id, False)
    buf = report(touch)
    assert buf[0] & 0x80 and buf[0] != 0x83
    assert buf[5] == 0x80 and buf[6] == 0xAB


def test_a_finger_report_still_reads_as_a_finger() -> None:
    """The key bit clear on the touch path, as it always was."""
    touch = CST328()
    touch.touch = (120, 75)
    buf = report(touch)
    assert buf[5] == 0x01                       # one finger, key bit clear
    assert buf[0] == CST328.CONTACT


def test_with_nothing_to_report_only_the_magic_is_set() -> None:
    touch = CST328()
    assert report(touch)[6] == 0xAB
    assert report(touch)[:6] == bytes(6)


# -- what the window writes, read back by the emulator's own tailer ----------


@pytest.fixture()
def linked(tmp_path: Path):
    path = tmp_path / "input.jsonl"
    tail = InputTail(path, interval=0.01)
    tail.start()
    yield InputWriter(path), tail
    tail.stop()


def test_a_touch_key_round_trips(linked) -> None:
    writer, tail = linked
    writer.soft_key(1, True)
    writer.soft_key(1, False)
    events = []
    while len(events) < 2:
        more = tail.drain()
        if not more and not events:
            time.sleep(0.02)
        events += more
    assert events == [{"type": "touchkey", "key": 1, "down": True},
                      {"type": "touchkey", "key": 1, "down": False}]


# -- the drawn icons, on a machine that has a display ------------------------

from tests.test_app_input import needs_display, tk_root  # noqa: E402


@pytest.mark.parametrize("index", [0, 1, 2])
@needs_display
def test_clicking_a_drawn_touch_key_sends_it(tmp_path: Path, index: int) -> None:
    import tkinter as tk

    from emu.app import DeckWindow

    events = tmp_path / "input.jsonl"
    root = tk_root()
    root.withdraw()
    try:
        window = DeckWindow(tk.Toplevel(root), tmp_path / "latest.png", 2,
                            InputWriter(events))
        root.update()
        x0, y0, x1, y1, _i = window.soft_hits[index]
        cx, cy = int((x0 + x1) / 2 * 2), int((y0 + y1) / 2 * 2)
        window.canvas.event_generate("<Button-1>", x=cx, y=cy)
        root.update()
        window.canvas.event_generate("<ButtonRelease-1>", x=cx, y=cy)
        root.update()

        lines = [json.loads(l) for l in events.read_text().splitlines()]
    finally:
        root.destroy()

    assert {"type": "touchkey", "key": index, "down": True} in lines
    assert {"type": "touchkey", "key": index, "down": False} in lines
