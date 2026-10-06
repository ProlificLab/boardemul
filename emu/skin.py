#!/usr/bin/env python3
"""Device layout and the input protocol, with no toolkit attached.

Everything here is arithmetic and data: where the glass sits inside the body,
which keys the board actually has, and what an event looks like on its way to
the emulator. `emu/app.py` draws it with tkinter; this module is what the tests
can import on a machine with no display, and on a build container with no GUI
libraries at all.

Two boards now, and they differ in the numbers rather than in the ideas: the
T-Deck Max is portrait with an e-paper panel, the T-Deck landscape with a colour
LCD. The geometry is therefore a `Skin` rather than a page of constants, with
the first board's values still exported under their old names so that anything
which only ever knew about one board keeps working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from emu import max_face, tdeck_face
from emu.devices.tdeck_i2c import CHORDS, MODIFIERS

# The vendor's own matrix, from examples/factory/peri_keypad.cpp. Drawing an
# invented QWERTY would look right and send keys the firmware does not have.
# Both boards carry the same BlackBerry-style keyboard; what differs is how the
# key reaches the firmware, which is the device model's business, not the skin's.
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


@dataclass(frozen=True)
class Skin:
    """One device's proportions, in panel pixels.

    Everything is expressed in panel pixels so that --scale multiplies the whole
    device and the screen stays pixel-exact at every setting.
    """

    name: str
    screen_w: int
    screen_h: int
    idle: str                   # what the glass looks like with nothing on it
    idle_rim: str
    colour_frames: bool         # whether the panel writes RGB or greyscale

    desk: int = 14              # empty space around the body, for the shadow
    side: int = 20              # body edge to screen edge
    head: int = 26              # body top to screen top: grille, brand, light
    gap: int = 14               # screen bezel to keyboard
    keypad_h: int = 96
    foot: int = 12
    bezel: int = 7              # width of the recess ring around the glass
    radius: int = 22            # shell corner radius
    kb_inset: int = 14          # body edge to keyboard block
    ball_col: int = 38
    ball_gap: int = 6

    key_rows: tuple = KEY_ROWS

    # A board drawn from its own sources rather than from the numbers above
    # (ADRs 0126, 0130) names its face, and says where its panel sits and how
    # big its canvas is, in panel pixels at scale 1.
    face: str | None = None
    origin: tuple[int, int] | None = None
    size: tuple[int, int] | None = None

    @property
    def body_w(self) -> int:
        return self.screen_w + 2 * self.side

    @property
    def body_h(self) -> int:
        return self.head + self.screen_h + self.gap + self.keypad_h + self.foot

    @property
    def canvas_w(self) -> int:
        return self.size[0] if self.size else self.body_w + 2 * self.desk

    @property
    def canvas_h(self) -> int:
        return self.size[1] if self.size else self.body_h + 2 * self.desk

    @property
    def screen_x(self) -> int:
        return self.origin[0] if self.origin else self.desk + self.side

    @property
    def screen_y(self) -> int:
        return self.origin[1] if self.origin else self.desk + self.head

    @property
    def body_x0(self) -> int:
        return self.desk

    @property
    def body_y0(self) -> int:
        return self.desk

    @property
    def body_x1(self) -> int:
        return self.desk + self.body_w

    @property
    def body_y1(self) -> int:
        return self.desk + self.body_h

    @property
    def body_cx(self) -> int:
        return self.desk + self.body_w // 2

    def panel_point(self, x: float, y: float, scale: int) -> tuple[int, int] | None:
        """Canvas pixel to panel pixel, or None if it missed the glass.

        The bezel, keyboard and shell are not the panel; treating a click on
        them as a touch would put a phantom finger at the screen edge.
        """
        px = x / scale - self.screen_x
        py = y / scale - self.screen_y
        if 0 <= px < self.screen_w and 0 <= py < self.screen_h:
            return int(px), int(py)
        return None


# e-paper white is a grey; a pure white would read as an LCD. Drawn from the
# Max's own case, panel and photographs (ADR 0130).
TDECK_MAX = Skin(name="tdeck-max", screen_w=240, screen_h=320,
                 idle="#d5d7cf", idle_rim="#b4b7ae", colour_frames=False,
                 face="max",
                 origin=max_face.panel_origin(), size=max_face.canvas_size())

# Drawn from LilyGO's drawing, photographs and keyboard sketch (ADR 0126): its
# own face, its own panel position, and its own keys.
TDECK = Skin(name="tdeck", screen_w=320, screen_h=240,
             idle="#050607", idle_rim="#050607", colour_frames=True,
             face="tdeck",
             origin=tdeck_face.panel_origin(), size=tdeck_face.canvas_size())

SKINS = {TDECK_MAX.name: TDECK_MAX, TDECK.name: TDECK}
DEFAULT = TDECK_MAX

# The first board's geometry under the names it has always had, so that code and
# tests which predate the second board keep working unchanged.
SCREEN_W, SCREEN_H = DEFAULT.screen_w, DEFAULT.screen_h
DESK, SIDE, HEAD, GAP = DEFAULT.desk, DEFAULT.side, DEFAULT.head, DEFAULT.gap
KEYPAD_H, FOOT, BEZEL, RADIUS = (DEFAULT.keypad_h, DEFAULT.foot,
                                 DEFAULT.bezel, DEFAULT.radius)
KB_INSET, BALL_COL, BALL_GAP = DEFAULT.kb_inset, DEFAULT.ball_col, DEFAULT.ball_gap
BODY_W, BODY_H = DEFAULT.body_w, DEFAULT.body_h
CANVAS_W, CANVAS_H = DEFAULT.canvas_w, DEFAULT.canvas_h
SCREEN_X, SCREEN_Y = DEFAULT.screen_x, DEFAULT.screen_y
BODY_X0, BODY_Y0 = DEFAULT.body_x0, DEFAULT.body_y0
BODY_X1, BODY_Y1 = DEFAULT.body_x1, DEFAULT.body_y1
BODY_CX = DEFAULT.body_cx


def panel_point(x: float, y: float, scale: int) -> tuple[int, int] | None:
    return DEFAULT.panel_point(x, y, scale)


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

    def roll(self, direction: str, steps: int) -> None:
        """The T-Deck's trackball rolled `steps` changes of a line (ADR 0128)."""
        self._emit({"type": "trackball", "roll": direction, "steps": steps})

    def ball(self, down: bool) -> None:
        """The T-Deck's trackball pressed or let go."""
        self._emit({"type": "trackball", "press": down})


class TDeckTyping:
    """How a mouse and the host's keyboard press the T-Deck's keys (ADR 0127).

    A character typed on the host is pressed as the chord that types it on
    the device, so the host's shift is never forwarded alone: it is already
    in the character. The host's Ctrl is SYM and its Alt is ALT, held while
    the host holds them. On the drawn device the modifiers latch when
    clicked, which is how a mouse holds two keys at once.

    Every key is counted, so a modifier the host holds, a chord that needs
    it and a latch can overlap: the board hears it go down when the first of
    them presses it and up when the last lets go.
    """

    HOST_HELD = {"Control_L": "SYM", "Control_R": "SYM",
                 "Alt_L": "ALT", "Alt_R": "ALT"}
    # Host keys whose character is not the one the device's key sends.
    HOST_NAMED = {"Return": "ENTER", "KP_Enter": "ENTER", "BackSpace": "DEL"}

    def __init__(self, emit):
        self.emit = emit                    # emit(key, down)
        self.count: dict[str, int] = {}
        self.host: dict[int, tuple[str, ...]] = {}
        self.latched: list[str] = []

    @property
    def down(self) -> set[str]:
        return {key for key, n in self.count.items() if n}

    def _press(self, key: str) -> None:
        n = self.count.get(key, 0)
        self.count[key] = n + 1
        if n == 0:
            self.emit(key, True)

    def _release(self, key: str) -> None:
        n = self.count.get(key, 0)
        if n == 0:
            return
        self.count[key] = n - 1
        if n == 1:
            self.emit(key, False)

    # -- the host's keyboard ------------------------------------------------

    def chord_for(self, keysym: str, char: str) -> tuple[str, ...] | None:
        if keysym in self.HOST_HELD:
            return (self.HOST_HELD[keysym],)
        if keysym in self.HOST_NAMED:
            return (self.HOST_NAMED[keysym],)
        # With the host's Ctrl or Alt down a letter's character is a control
        # code or nothing, so the key is taken from its keysym.
        held = any(chord[0] in ("SYM", "ALT") and len(chord) == 1
                   for chord in self.host.values())
        if held and len(keysym) == 1 and keysym.isalpha():
            return (keysym.upper(),)
        return CHORDS.get(char) if len(char) == 1 else None

    def host_press(self, keysym: str, char: str, keycode: int) -> tuple[str, ...] | None:
        if keycode in self.host:
            return None                     # auto-repeat; the keyboard has none
        chord = self.chord_for(keysym, char)
        if chord is None:
            return None
        self.host[keycode] = chord
        for key in chord:
            self._press(key)
        return chord

    def host_release(self, keycode: int) -> None:
        chord = self.host.pop(keycode, None)
        if chord is None:
            return
        for key in reversed(chord):
            self._release(key)
        if chord[-1] not in MODIFIERS:
            self._drop_latches()

    def host_release_all(self) -> None:
        """The window lost focus, so the host's releases will not come."""
        for keycode in list(self.host):
            self.host_release(keycode)

    # -- the mouse on the drawn device -------------------------------------

    def click_press(self, key: str) -> None:
        if key in MODIFIERS:
            if key in self.latched:
                self.latched.remove(key)
                self._release(key)
            else:
                self.latched.append(key)
                self._press(key)
            return
        self._press(key)

    def click_release(self, key: str) -> None:
        if key in MODIFIERS:
            return
        self._release(key)
        self._drop_latches()

    def _drop_latches(self) -> None:
        while self.latched:
            self._release(self.latched.pop())
