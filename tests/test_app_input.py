#!/usr/bin/env python3
"""The viewer's input path, checked without a display or a toolkit.

The important property is that what the app writes is exactly what the emulator
reads, so these tests round-trip through the emulator's own tailer rather than
asserting on a format both sides could get wrong together.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.skin import (  # noqa: E402
    KEY_NAMES, KEY_ROWS, LETTER_KEYS, SCREEN_H, SCREEN_W, SCREEN_X, SCREEN_Y,
    InputWriter, panel_point,
)
from emu.devices.i2c_devices import TCA8418  # noqa: E402
from emu.inputs import InputTail  # noqa: E402


# -- coordinate mapping -----------------------------------------------------


@pytest.mark.parametrize("scale", [1, 2, 3, 4])
def test_screen_corners_map_to_panel_corners(scale: int) -> None:
    assert panel_point(SCREEN_X * scale, SCREEN_Y * scale, scale) == (0, 0)
    far_x = (SCREEN_X + SCREEN_W) * scale - 1
    far_y = (SCREEN_Y + SCREEN_H) * scale - 1
    assert panel_point(far_x, far_y, scale) == (SCREEN_W - 1, SCREEN_H - 1)


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_clicks_off_the_glass_are_not_touches(scale: int) -> None:
    """The bezel, the keyboard and the shell are not the panel; treating a click
    on them as a touch would put a phantom finger at the screen edge."""
    assert panel_point(0, 0, scale) is None
    assert panel_point((SCREEN_X - 1) * scale, SCREEN_Y * scale, scale) is None
    assert panel_point(SCREEN_X * scale,
                       (SCREEN_Y + SCREEN_H) * scale, scale) is None


def test_scale_does_not_move_the_panel_origin() -> None:
    centre = [panel_point((SCREEN_X + 120) * s, (SCREEN_Y + 75) * s, s)
              for s in (1, 2, 3, 4)]
    assert centre == [(120, 75)] * 4


# -- what the app writes, read back by the emulator -------------------------


def drain(tail: InputTail) -> list[dict]:
    for _ in range(50):
        events = tail.drain()
        if events:
            return events
        time.sleep(0.02)
    return []


@pytest.fixture()
def linked(tmp_path: Path):
    path = tmp_path / "input.jsonl"
    tail = InputTail(path, interval=0.01)
    tail.start()
    yield InputWriter(path), tail
    tail.stop()


def test_a_tap_round_trips(linked) -> None:
    writer, tail = linked
    writer.touch(120, 75)
    writer.release()
    events = drain(tail) + drain(tail)
    assert {"type": "touch", "x": 120, "y": 75, "down": True} in events
    assert {"type": "touch", "down": False} in events


def test_a_drag_sends_only_movement(linked) -> None:
    """Tk fires motion many times per pixel; repeating an identical coordinate
    would flood the emulator for no information."""
    writer, tail = linked
    for _ in range(5):
        writer.touch(100, 100)
    writer.touch(101, 100)
    events = drain(tail)
    while len(events) < 2:
        more = drain(tail)
        if not more:
            break
        events += more
    assert len(events) == 2


def test_a_key_round_trips_into_the_controller(linked) -> None:
    """End to end for a keystroke: what the app writes reaches the keypad model
    as the byte the firmware's own table expects."""
    writer, tail = linked
    writer.key("Q", True)
    events = drain(tail)
    assert events and events[0] == {"type": "key", "key": "Q", "down": True}

    keypad = TCA8418()
    assert keypad.key_event(events[0]["key"], events[0]["down"])
    assert keypad.pending == [0x8A]


# -- the drawn keyboard -----------------------------------------------------


def drawn_labels() -> list[str]:
    return [label for row in KEY_ROWS for label, _span in row]


def test_every_drawn_key_exists_on_the_real_device() -> None:
    """A cap that sends nothing is a lie about the hardware."""
    keymap = TCA8418().keymap
    for label in drawn_labels():
        name = KEY_NAMES.get(label, label)
        assert name in keymap, f"{label!r} is drawn but is not a key on this board"


def test_the_drawn_keyboard_covers_the_matrix() -> None:
    """Every populated position in the vendor's matrix should be reachable."""
    keymap = TCA8418().keymap
    reachable = {KEY_NAMES.get(label, label) for label in drawn_labels()}
    missing = set(keymap) - reachable
    # Both UP positions are the same key to the firmware, so only one is drawn.
    assert missing <= {"UP2"}, f"unreachable keys: {sorted(missing)}"


def test_letter_styling_matches_the_labels() -> None:
    """LETTER_KEYS only drives how a cap is drawn, but a mismatch means a real
    key is rendered as a dim modifier."""
    for label in drawn_labels():
        if len(label) == 1:
            assert label in LETTER_KEYS, label


# -- the bindings themselves, on a machine that has a display ---------------


def _display() -> bool:
    try:
        import tkinter as tk
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except Exception:
        return False
    root.destroy()
    return True


needs_display = pytest.mark.skipif(not _display(), reason="no display or no tkinter")


@needs_display
def test_clicking_the_glass_sends_a_touch(tmp_path: Path) -> None:
    """Everything above tests the protocol; this tests that the widget is
    actually wired to it, which is the one link the rest cannot see."""
    import tkinter as tk

    from emu.app import DeckWindow
    from emu.skin import SCREEN_X, SCREEN_Y

    events = tmp_path / "input.jsonl"
    scale = 2
    root = tk.Tk()
    root.withdraw()
    try:
        window = DeckWindow(tk.Toplevel(root), tmp_path / "latest.png", scale,
                            InputWriter(events))
        root.update()
        x = (SCREEN_X + 120) * scale
        y = (SCREEN_Y + 75) * scale
        window.canvas.event_generate("<Button-1>", x=x, y=y)
        window.canvas.event_generate("<ButtonRelease-1>", x=x, y=y)
        root.update()

        lines = [json.loads(l) for l in events.read_text().splitlines()]
    finally:
        root.destroy()

    assert lines[0] == {"type": "touch", "x": 120, "y": 75, "down": True}
    assert lines[-1] == {"type": "touch", "down": False}


@needs_display
def test_clicking_a_cap_sends_that_key(tmp_path: Path) -> None:
    import tkinter as tk

    from emu.app import DeckWindow

    events = tmp_path / "input.jsonl"
    scale = 2
    root = tk.Tk()
    root.withdraw()
    try:
        window = DeckWindow(tk.Toplevel(root), tmp_path / "latest.png", scale,
                            InputWriter(events))
        root.update()
        x0, y0, x1, y1, _ = next(k for k in window.key_hits if k[4] == "Q")
        cx, cy = int((x0 + x1) / 2 * scale), int((y0 + y1) / 2 * scale)
        window.canvas.event_generate("<Button-1>", x=cx, y=cy)
        window.canvas.event_generate("<ButtonRelease-1>", x=cx, y=cy)
        root.update()

        lines = [json.loads(l) for l in events.read_text().splitlines()]
    finally:
        root.destroy()

    assert {"type": "key", "key": "Q", "down": True} in lines
    assert {"type": "key", "key": "Q", "down": False} in lines
