#!/usr/bin/env python3
"""Device layout and the input protocol, with no toolkit attached.

Everything here is arithmetic and data: where the glass sits inside the body,
which keys the board actually has, and what an event looks like on its way to
the emulator. `emu/app.py` draws it with tkinter; this module is what the tests
can import on a machine with no display, and on a build container with no GUI
libraries at all.
"""

from __future__ import annotations

import json
from pathlib import Path

# The firmware draws the launcher upright on the panel's native raster, so the
# PNG on disk is portrait and so is the device: screen on top, keyboard under
# it, in a tall narrow body.
SCREEN_W, SCREEN_H = 240, 320

# Body geometry in panel pixels, so that --scale multiplies the whole device and
# the screen stays pixel-exact at every setting.
DESK = 14          # empty space around the body, for the drop shadow
SIDE = 20          # body edge to screen edge
HEAD = 26          # body top to screen top: grille, brand, indicator
GAP = 14           # screen bezel to keyboard; must clear BEZEL + the plate ring
KEYPAD_H = 96
FOOT = 12
BEZEL = 7          # width of the recess ring around the glass
RADIUS = 22        # shell corner radius

KB_INSET = 14      # body edge to keyboard block
BALL_COL = 38
BALL_GAP = 6

BODY_W = SCREEN_W + 2 * SIDE
BODY_H = HEAD + SCREEN_H + GAP + KEYPAD_H + FOOT
CANVAS_W = BODY_W + 2 * DESK
CANVAS_H = BODY_H + 2 * DESK

SCREEN_X = DESK + SIDE
SCREEN_Y = DESK + HEAD
BODY_X0, BODY_Y0 = DESK, DESK
BODY_X1, BODY_Y1 = DESK + BODY_W, DESK + BODY_H
BODY_CX = DESK + BODY_W // 2

# The vendor's own matrix, from examples/factory/peri_keypad.cpp. Drawing an
# invented QWERTY would look right and send keys the firmware does not have.
KEY_ROWS = (
    (("Q", 1), ("W", 1), ("E", 1), ("R", 1), ("T", 1),
     ("Y", 1), ("U", 1), ("I", 1), ("O", 1), ("P", 1)),
    (("A", 1), ("S", 1), ("D", 1), ("F", 1), ("G", 1),
     ("H", 1), ("J", 1), ("K", 1), ("L", 1), ("DEL", 1)),
    (("ALT", 1), ("Z", 1), ("X", 1), ("C", 1), ("V", 1),
     ("B", 1), ("N", 1), ("M", 1), ("$", 1), ("ENT", 1)),
    (("SYM", 1), ("0", 1), ("SPACE", 6), ("UP", 2)),
)
LETTER_KEYS = set("QWERTYUIOPASDFGHJKLZXCVBNM$0")

# Where the cap legend and the emulator's key name differ.
KEY_NAMES = {"ENT": "ENTER"}

# Physical keys, for typing rather than clicking. Only what the device has.
KEYSYMS = {
    "Return": "ENTER", "BackSpace": "DEL", "space": "SPACE",
    "Shift_L": "UP", "Shift_R": "UP", "Alt_L": "ALT", "Alt_R": "ALT",
    "Control_L": "SYM", "Control_R": "SYM", "dollar": "$",
}


def panel_point(x: float, y: float, scale: int) -> tuple[int, int] | None:
    """Canvas pixel to panel pixel, or None if it missed the glass.

    The bezel, keyboard and shell are not the panel; treating a click on them
    as a touch would put a phantom finger at the screen edge.
    """
    px = x / scale - SCREEN_X
    py = y / scale - SCREEN_Y
    if 0 <= px < SCREEN_W and 0 <= py < SCREEN_H:
        return int(px), int(py)
    return None


class InputWriter:
    """Appends events to the JSON-lines file the emulator tails (ADR 0010).

    Deliberately fire-and-forget: nothing here waits on the emulator, and it is
    fine for the file to exist before the emulator starts -- the tailer seeks to
    the end on startup, so a stale log cannot replay into a fresh boot.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self._last: tuple[int, int] | None = None

    def _emit(self, event: dict) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                print(json.dumps(event, separators=(",", ":")), file=fh)
        except OSError:
            return
        self.count += 1

    def touch(self, x: int, y: int) -> None:
        # A drag fires many times per pixel; only movement is worth sending.
        if self._last == (x, y):
            return
        self._last = (x, y)
        self._emit({"type": "touch", "x": x, "y": y, "down": True})

    def release(self) -> None:
        self._last = None
        self._emit({"type": "touch", "down": False})

    def key(self, name: str, down: bool) -> None:
        self._emit({"type": "key", "key": name, "down": down})
