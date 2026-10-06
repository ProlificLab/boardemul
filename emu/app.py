#!/usr/bin/env python3
"""A T-Deck Max on the desktop: the emulated panel inside a drawn device body.

The panel model (emu/devices/epaper.py) rewrites logs/frames/latest.png on every
refresh command, so a viewer that watches that one file needs no coupling to the
emulator at all -- no import, no socket, no shared process. Start it before,
during or after a run and it catches up on the next poll.

Each board's body is one anti-aliased image rendered from its own sources
(ADRs 0126, 0130), with the legends drawn as Canvas text over it. Bundling a
device photograph would put a binary asset in a repo whose only dependency is
the Python standard library (README, "Dependencies"), and would fix the skin
at one resolution besides.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Launched as a script rather than as a module, so the package root has to be
# put on the path before importing from it.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_FRAMES = REPO_ROOT / "logs" / "frames"

TITLES = {"tdeck-max": "T-Deck Max", "tdeck": "T-Deck"}

# Faster than the panel can refresh (real e-paper needs hundreds of ms) and slow
# enough that the stat() call is free.
POLL_MS = 250

# Layout, key matrix and the input protocol live in emu/skin.py: they are
# arithmetic and data, and keeping them out of here lets the tests import
# them on a machine with no display.
from emu.skin import (  # noqa: E402
    DEFAULT, KEY_NAMES, KEYSYMS, LETTER_KEYS, SKINS, InputWriter, Skin, TDeckTyping,
)
from emu.devices.tdeck_trackball import NOTCH  # noqa: E402

# The keyboard backlight's halo: from the plate's grey towards a white LED.
C_GLOW_FROM = "#5a5d61"
C_GLOW_TO = "#eef4ff"


def _mix(a: str, b: str, t: float) -> str:
    from emu.raster import hex_colour, mix
    return "#%02x%02x%02x" % mix(hex_colour(a), hex_colour(b), t)

# The host's arrow keys roll the T-Deck's trackball a notch (ADR 0128).
ROLLS = {"Up": "up", "Down": "down", "Left": "left", "Right": "right"}

# Lit from the top left throughout: highlights go on top and left edges,
# shadows on bottom and right. Consistency is what sells the depth, since
# tkinter has neither gradients nor alpha to sell it with.
C_DESK = "#0f1113"
C_KEY_LIT = "#4a525a"
C_GHOST = "#c6c9c0"
C_IDLE_INK = "#8b9088"

# Ten columns across, spans in column units. Labels are the T-Deck's BlackBerry
# keyboard; they are decoration, as nothing here feeds input back to the guest.


def _family(root: tk.Misc, *names: str) -> str:
    """Tk substitutes silently for a missing family, which is how a careful
    layout ends up in a serif face on someone else's machine. Ask first."""
    available = set(tkfont.families(root))
    for name in names:
        if name in available:
            return name
    return names[-1]


def _round_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float,
                r: float, **kw) -> int:
    """Canvas has no rounded rectangle, and create_polygon(smooth=True) rounds
    by spline tension rather than by radius. Walk the corner arcs instead."""
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts: list[float] = []
    for cx, cy, start in ((x1 - r, y0 + r, -90.0), (x1 - r, y1 - r, 0.0),
                          (x0 + r, y1 - r, 90.0), (x0 + r, y0 + r, 180.0)):
        for i in range(9):
            a = math.radians(start + 90.0 * i / 8)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return canvas.create_polygon(pts, **kw)


class DeckWindow:
    """The window: a drawn shell, plus whatever the panel last wrote."""

    def __init__(self, root: tk.Tk, path: Path, scale: int,
                 events: "InputWriter | None" = None, skin: Skin = DEFAULT):
        self.root = root
        self.path = path
        self.scale = scale
        self.skin = skin
        self.events = events
        self.key_hits: list[tuple[float, float, float, float, str]] = []
        self.pressed: int | None = None
        self.touching = False
        self.frames = 0
        self.stamp: tuple[int, int] | None = None
        self.photo: tk.PhotoImage | None = None
        self.note = "waiting"
        # The T-Deck's keys are pressed the way the device presses them
        # (ADR 0127); what is down is drawn lit, a latched modifier ringed.
        self.typing = (TDeckTyping(self._typed)
                       if skin.face == "tdeck" and events is not None else None)
        self.clicked: str | None = None
        self.lit: list[int] = []
        self.ball_down = False
        self.ball_items: list[int] = []
        # The keyboard's backlight, as the board reports it (ADR 0129).
        self.board_stamp: tuple[int, int] | None = None
        self.glow = 0.0
        self.glow_items: list[int] = []
        self._ball_after: str | None = None

        self.ui = _family(root, "Segoe UI", "DejaVu Sans", "Helvetica")
        self.mono = _family(root, "Consolas", "DejaVu Sans Mono", "Courier")

        self.canvas = tk.Canvas(root, width=self.skin.canvas_w * scale,
                                height=self.skin.canvas_h * scale,
                                background=C_DESK, highlightthickness=0)
        self.canvas.pack()
        # width=1 so the path in the status text cannot widen the window past
        # the canvas and leave the device sitting off-centre; fill="x" then
        # stretches it back to whatever the canvas asked for.
        self.metrics = tkfont.Font(root=root, family=self.mono, size=-8 * scale)
        self.screen_metrics = tkfont.Font(root=root, family=self.mono,
                                          size=-max(7, int(7.5 * scale)))
        self.status = tk.Label(root, anchor="w", width=1, background="#16191c",
                               foreground="#8f979e", font=self.metrics,
                               padx=5 * scale, pady=3 * scale)
        self.status.pack(fill="x")

        self._draw_shell()
        self._show_idle()
        self._bind_input()
        self._refresh_status()
        self._poll()

    # -- input -----------------------------------------------------------

    def _bind_input(self) -> None:
        if self.events is None:
            return
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<KeyPress>", self._on_keypress)
        self.root.bind("<KeyRelease>", self._on_keyrelease)
        self.root.bind("<FocusOut>", self._on_focus_out)
        if self.typing is not None:
            # The trackball (ADR 0128): the wheel rolls it up and down, and
            # with Shift left and right; X11 reports the wheel as buttons.
            self.canvas.bind("<MouseWheel>", self._on_wheel)
            self.canvas.bind("<Button-4>", lambda e: self._roll("up"))
            self.canvas.bind("<Button-5>", lambda e: self._roll("down"))

    def _panel_point(self, x: float, y: float) -> tuple[int, int] | None:
        return self.skin.panel_point(x, y, self.scale)

    def _key_at(self, x: float, y: float) -> str | None:
        ux, uy = x / self.scale, y / self.scale
        for x0, y0, x1, y1, label in self.key_hits:
            if x0 <= ux <= x1 and y0 <= uy <= y1:
                return label
        return None

    def _on_press(self, event: "tk.Event") -> None:
        point = self._panel_point(event.x, event.y)
        if point is not None:
            self.touching = True
            self.events.touch(*point)
            return
        if self.typing is not None and self._on_ball(event.x, event.y):
            self.ball_down = True
            self.events.ball(True)
            self._show_ball()
            return
        label = self._key_at(event.x, event.y)
        if label is not None and self.typing is not None:
            self.clicked = label
            self.typing.click_press(label)
        elif label is not None:
            self._flash(label, True)
            self.events.key(KEY_NAMES.get(label, label), True)

    def _on_drag(self, event: "tk.Event") -> None:
        if not self.touching:
            return
        point = self._panel_point(event.x, event.y)
        if point is not None:
            self.events.touch(*point)

    def _on_release(self, _event: "tk.Event") -> None:
        if self.touching:
            self.touching = False
            self.events.release()
        if self.ball_down:
            self.ball_down = False
            self.events.ball(False)
            self._show_ball()
        if self.clicked is not None:
            self.typing.click_release(self.clicked)
            self.clicked = None
        if self.pressed is not None:
            label = self._flash(None, False)
            if label:
                self.events.key(KEY_NAMES.get(label, label), False)

    def _on_keypress(self, event: "tk.Event") -> None:
        if self.typing is not None:
            if event.keysym in ROLLS:
                # Held, the host repeats it, and the ball keeps rolling.
                self._roll(ROLLS[event.keysym])
                return
            self.typing.host_press(event.keysym, event.char, event.keycode)
            return
        name = self._keysym(event)
        if name:
            self.events.key(name, True)
            self._flash(name if name != "ENTER" else "ENT", True)

    def _on_keyrelease(self, event: "tk.Event") -> None:
        if self.typing is not None:
            self.typing.host_release(event.keycode)
            return
        name = self._keysym(event)
        if name:
            self.events.key(name, False)
            self._flash(None, False)

    def _on_wheel(self, event: "tk.Event") -> None:
        sideways = bool(event.state & 0x0001)        # Shift held
        ahead = event.delta > 0
        notches = max(1, abs(event.delta) // 120)
        for _ in range(notches):
            self._roll(("left" if ahead else "right") if sideways
                       else ("up" if ahead else "down"))

    def _on_ball(self, x: float, y: float) -> bool:
        cx, cy, r = self.face.trackball
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def _roll(self, direction: str) -> None:
        self.events.roll(direction, NOTCH)
        self._show_ball(direction)

    def _show_ball(self, rolling: str | None = None) -> None:
        """Light the ball while it is pressed; while it rolls, slide its
        highlight the way it turns, then put it back."""
        for item in self.ball_items:
            self.canvas.delete(item)
        self.ball_items = []
        cx, cy, ring = self.face.trackball
        r = ring * 0.63                             # the ball inside its ring
        if self.ball_down:
            self.ball_items.append(self._disc(
                cx, cy, r, fill="#ffffff", stipple="gray25",
                outline="#ffffff", width=max(1, round(1.5 * self.scale))))
        if rolling is not None:
            dx, dy = {"up": (0, -1), "down": (0, 1),
                      "left": (-1, 0), "right": (1, 0)}[rolling]
            hx, hy, hr = cx + dx * r * 0.45, cy + dy * r * 0.45, r * 0.32
            self.ball_items.append(self._disc(hx, hy, hr, fill="#ffffff",
                                              stipple="gray50", outline=""))
            if self._ball_after is not None:
                self.root.after_cancel(self._ball_after)
            self._ball_after = self.root.after(140, self._settle_ball)

    def _disc(self, cx: float, cy: float, r: float, **kw) -> int:
        """A circle as a polygon, in canvas pixels: Tk on Windows stipples a
        polygon but fills an oval solid, and these have to be translucent."""
        points = []
        for i in range(32):
            a = 2 * math.pi * i / 32
            points += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return self.canvas.create_polygon(points, **kw)

    def _settle_ball(self) -> None:
        self._ball_after = None
        self._show_ball()

    def _on_focus_out(self, _event: "tk.Event") -> None:
        if self.typing is not None:
            self.typing.host_release_all()

    def _typed(self, key: str, down: bool) -> None:
        self.events.key(key, down)
        self._show_down()

    def _show_down(self) -> None:
        """Light every key that is down, with a pale wash that keeps its
        legend readable, and ring a modifier that a click latched."""
        for item in self.lit:
            self.canvas.delete(item)
        self.lit = []
        down, latched = self.typing.down, set(self.typing.latched)
        for x0, y0, x1, y1, name in self.key_hits:
            if name in down:
                self.lit.append(self._rr(
                    x0, y0, x1, y1, 5, fill="#ffffff", stipple="gray25",
                    outline="#ffffff" if name in latched else "",
                    width=max(1, round(1.5 * self.scale))))

    @staticmethod
    def _keysym(event: "tk.Event") -> str | None:
        if event.keysym in KEYSYMS:
            return KEYSYMS[event.keysym]
        if len(event.char) == 1 and event.char.upper() in LETTER_KEYS:
            return event.char.upper()
        return None

    def _flash(self, label: str | None, down: bool) -> str | None:
        """Light the cap while it is held, so a click has a visible effect even
        before the panel refreshes -- which on e-paper takes a second."""
        if self.pressed is not None:
            self.canvas.delete(self.pressed)
            self.pressed = None
        held = getattr(self, "_held_label", None)
        if not down:
            self._held_label = None
            return held
        for x0, y0, x1, y1, name in self.key_hits:
            if name == label:
                self.pressed = self._rr(x0, y0, x1, y1, 3,
                                        fill=C_KEY_LIT, outline="")
                self.canvas.tag_raise(self.pressed)
                self._text((x0 + x1) / 2, (y0 + y1) / 2, name,
                           9 if name in LETTER_KEYS else 5.5, C_DESK,
                           bold=name in LETTER_KEYS)
                self._held_label = name
                return name
        self._held_label = label
        return label

    # -- geometry helpers ------------------------------------------------
    # Everything below lays out in panel pixels and converts on the way to the
    # canvas, so a scale change cannot leave one element behind.

    def s(self, *values: float) -> tuple[float, ...]:
        return tuple(v * self.scale for v in values)

    def _px(self, units: float) -> int:
        return max(7, int(units * self.scale))

    def _text(self, x: float, y: float, label: str, size: float, colour: str,
              bold: bool = False, anchor: str = "center", mono: bool = False,
              tags: str = "") -> int:
        # Negative Tk font sizes are pixels, not points: the drawing has to keep
        # its proportions when Windows is running at 125% or 150% DPI.
        style = (self.mono if mono else self.ui, -self._px(size))
        return self.canvas.create_text(*self.s(x, y), text=label, fill=colour,
                                       font=(style + ("bold",)) if bold else style,
                                       anchor=anchor, tags=tags)

    def _rr(self, x0: float, y0: float, x1: float, y1: float, r: float, **kw) -> int:
        return _round_rect(self.canvas, *self.s(x0, y0, x1, y1, r), **kw)

    def _oval(self, cx: float, cy: float, r: float, **kw) -> int:
        return self.canvas.create_oval(*self.s(cx - r, cy - r, cx + r, cy + r), **kw)

    # -- the shell -------------------------------------------------------

    def _draw_shell(self) -> None:
        if self.skin.face:
            self._draw_face()
            return

    def _draw_face(self) -> None:
        """A board with a face of its own (ADRs 0126, 0130): the body as one
        anti-aliased image, the legends as text over it."""
        from emu import max_face, tdeck_face
        faces = {"tdeck": tdeck_face, "max": max_face}
        face = faces[self.skin.face].build(self.scale)
        self.face_photo = tk.PhotoImage(master=self.canvas, data=face.raster.tk_data())
        self.canvas.create_image(0, 0, image=self.face_photo, anchor="nw")
        legend = _family(self.root, "Segoe UI Semibold", "Segoe UI", "DejaVu Sans", "Helvetica")
        small = _family(self.root, "Segoe UI", "DejaVu Sans", "Helvetica")
        for t in face.texts:
            size = -max(6, round(t.size))
            font = (legend, size, "bold") if t.bold else (small, size)
            # A cap's legend is tagged with its printed colour, for the glow.
            tags = ("legend", t.colour) if t.glows else ()
            self.canvas.create_text(t.x, t.y, text=t.text, fill=t.colour, font=font,
                                    anchor=t.anchor, tags=tags)
        self.key_hits = [(x0 / self.scale, y0 / self.scale, x1 / self.scale,
                          y1 / self.scale, name) for x0, y0, x1, y1, name in face.keys]
        self.face = face

    # -- the screen ------------------------------------------------------

    def _show_idle(self) -> None:
        """A powered panel with nothing written to it still shows something --
        the last image, faintly. A blank window would read as a broken app."""
        self.canvas.delete("screen")
        x0, y0 = self.skin.screen_x, self.skin.screen_y
        x1, y1 = self.skin.screen_x + self.skin.screen_w, self.skin.screen_y + self.skin.screen_h
        self.canvas.create_rectangle(*self.s(x0, y0, x1, y1), fill=self.skin.idle,
                                     outline="", tags="screen")
        # Ghosting bands, kept clear of the middle so they do not read as a
        # strikethrough across the text.
        for gy in (y0 + 55, y0 + 80, y0 + 240, y0 + 265):
            self.canvas.create_line(*self.s(x0 + 20, gy, x1 - 20, gy),
                                    fill=C_GHOST, tags="screen")
        self.canvas.create_rectangle(*self.s(x0 + 12, y0 + 12, x1 - 12, y1 - 12),
                                     outline=self.skin.idle_rim, dash=(4, 4), tags="screen")
        self._text((x0 + x1) / 2, y0 + 140, "NO SIGNAL", 18, C_IDLE_INK,
                   bold=True, tags="screen")
        self._text((x0 + x1) / 2, y0 + 168, "panel has not refreshed yet", 8.5,
                   C_IDLE_INK, tags="screen")
        # Whatever --frames was given can be arbitrarily long; unelided it
        # runs straight off the glass and over the bezel.
        self._text((x0 + x1) / 2, y0 + 190,
                   self._elide(self._short_path(), (self.skin.screen_w - 20) * self.scale,
                               self.screen_metrics),
                   7.5, "#9ba095", mono=True, tags="screen")

    def _short_path(self) -> str:
        parts = self.path.parts
        return "/".join(parts[-3:]) if len(parts) > 3 else str(self.path)

    @staticmethod
    def _elide(text: str, budget: float, font: tkfont.Font) -> str:
        """Eaten from the front: for a path the tail is the part that says which
        run's frames these are, so it is the part that has to survive."""
        if font.measure(text) <= budget:
            return text
        while text and font.measure("..." + text) > budget:
            text = text[1:]
        return "..." + text

    def _load(self) -> bool:
        """False means try again next tick. epaper.render() writes latest.png
        with a plain write_bytes, so a poll can land mid-write and hand Tk a
        truncated PNG; that is a timing artefact, not a failure to report."""
        try:
            src = tk.PhotoImage(master=self.canvas, file=str(self.path))
        except tk.TclError:
            return False
        if self.scale > 1:
            src = src.zoom(self.scale)

        # Composite onto a fixed-size buffer so that a PNG which is not the
        # panel's own 240x320 -- say --frames pointed at some other capture --
        # is letterboxed or cropped instead of spilling over the bezel.
        box_w, box_h = self.skin.screen_w * self.scale, self.skin.screen_h * self.scale
        take_w, take_h = min(src.width(), box_w), min(src.height(), box_h)
        shown = tk.PhotoImage(master=self.canvas, width=box_w, height=box_h)
        shown.put(self.skin.idle, to=(0, 0, box_w, box_h))
        shown.tk.call(shown, "copy", src, "-from", 0, 0, take_w, take_h,
                      "-to", (box_w - take_w) // 2, (box_h - take_h) // 2)

        self.canvas.delete("screen")
        # Tk frees a photo when its last Python reference drops, so the new one
        # has to be held before the old is released or the screen flashes.
        self.photo = shown
        self.canvas.create_image(*self.s(self.skin.screen_x, self.skin.screen_y), image=shown,
                                 anchor="nw", tags="screen")
        self.frames += 1
        self.note = time.strftime("%H:%M:%S")
        if (src.width(), src.height()) != (box_w, box_h):
            self.note += f"  ({src.width() // self.scale}x{src.height() // self.scale})"
        return True

    # -- the loop --------------------------------------------------------

    def _poll(self) -> None:
        # Rescheduled in a finally: a viewer that quietly stopped updating would
        # be read as a stalled emulator, which is a far worse lie than a
        # traceback on the console.
        try:
            try:
                st = self.path.stat()
            except OSError:
                st = None                   # a run may not have created it yet
            if st is None:
                if self.stamp is not None:
                    self.stamp, self.note = None, "no frame file"
                    self._show_idle()
                    self._refresh_status()
            else:
                # Size as well as mtime: two refreshes inside one filesystem
                # timestamp tick are entirely possible at emulated speed.
                stamp = (st.st_mtime_ns, st.st_size)
                if stamp != self.stamp and self._load():
                    self.stamp = stamp
                    self._refresh_status()
            if self.skin.face:
                self._poll_board()
        finally:
            self.root.after(POLL_MS, self._poll)

    def _poll_board(self) -> None:
        """Read the board's state file when it changes (ADR 0129)."""
        path = self.path.parent / "board.json"
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp == self.board_stamp:
                return
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.board_stamp = stamp
        glow = float(state.get("keyboard_backlight", 0.0))
        if glow != self.glow:
            self.glow = glow
            self._show_glow()

    def _show_glow(self) -> None:
        """The keyboard's LEDs sit under the caps: lit, light leaks round
        every cap's edge and the legends shine, in proportion to the duty."""
        for item in self.glow_items:
            self.canvas.delete(item)
        self.glow_items = []
        legend = "#ffffff" if self.glow > 0 else None
        for item in self.canvas.find_withtag("legend"):
            self.canvas.itemconfigure(item, fill=legend or self.canvas.gettags(item)[1])
        if self.glow <= 0:
            return
        level = min(1.0, self.glow)
        halo = _mix(C_GLOW_FROM, C_GLOW_TO, 0.35 + 0.65 * level)
        width = max(1, round((0.8 + 1.2 * level) * self.scale))
        for x0, y0, x1, y1, _name in self.key_hits:
            self.glow_items.append(self._rr(x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5, 5,
                                            fill="", outline=halo, width=width))

    def _refresh_status(self) -> None:
        head = f"frames {self.frames:<3d} last {self.note:<13s} "
        budget = self.skin.canvas_w * self.scale - 12 * self.scale - self.metrics.measure(head)
        self.status.config(
            text=head + self._elide(str(self.path), budget, self.metrics))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=Path, default=DEFAULT_FRAMES,
                    help="frame directory, or a single PNG, to watch "
                         "(default: logs/frames)")
    ap.add_argument("--input", type=Path, default=None,
                    help="event file the emulator tails "
                         "(default: input.jsonl beside the frame directory)")
    ap.add_argument("--read-only", action="store_true",
                    help="watch without sending anything")
    ap.add_argument("--scale", type=int, default=2, choices=(1, 2, 3, 4),
                    help="integer pixel zoom; a 1-bit panel must not be "
                         "interpolated (default: 2)")
    ap.add_argument("--board", default=DEFAULT.name, choices=sorted(SKINS),
                    help=f"which device to draw (default: {DEFAULT.name})")
    args = ap.parse_args(argv)

    skin = SKINS[args.board]
    frames = args.frames
    path = frames / "latest.png" if (frames.is_dir() or not frames.suffix) else frames
    events = None
    if not args.read_only:
        events = InputWriter(args.input or (path.parent.parent / "input.jsonl"))

    # On a display scaled above 100%, Windows bitmap-stretches a DPI-unaware
    # window: the integer zoom below would then be resampled back to fractional
    # and every panel pixel would come out soft. Opting in costs nothing on a
    # 100% display and is a no-op everywhere else.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"no display: {exc}", file=sys.stderr)
        return 2

    root.title(f"BoardEmul - LilyGO {TITLES[skin.name]}")
    root.configure(background=C_DESK)
    root.resizable(False, False)
    root.bind("<Escape>", lambda _e: root.destroy())

    app = DeckWindow(root, path, args.scale, events, skin=skin)

    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = max(0, (root.winfo_screenwidth() - w) // 2)
    y = max(0, (root.winfo_screenheight() - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")

    print(f"board   : {skin.name}")
    print(f"panel   : {app.path}")
    print(f"scale   : {args.scale}x ({skin.screen_w * args.scale}x{skin.screen_h * args.scale})")
    print(f"poll    : {POLL_MS} ms")
    # A portrait panel gets tall fast; say so rather than let the window run off
    # the bottom of the display with no explanation.
    if h > root.winfo_screenheight():
        print(f"note    : window is {h}px tall, taller than this display; "
              f"try --scale {max(1, args.scale - 1)}")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
