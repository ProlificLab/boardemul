#!/usr/bin/env python3
"""A T-Deck Max on the desktop: the emulated panel inside a drawn device body.

The panel model (emu/devices/epaper.py) rewrites logs/frames/latest.png on every
refresh command, so a viewer that watches that one file needs no coupling to the
emulator at all -- no import, no socket, no shared process. Start it before,
during or after a run and it catches up on the next poll.

Everything on screen is drawn with Canvas primitives. Bundling a device
photograph would put a binary asset in a repo whose only dependency is the
Python standard library (README, "Dependencies"), and would fix the skin at one
resolution besides.
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

# Faster than the panel can refresh (real e-paper needs hundreds of ms) and slow
# enough that the stat() call is free.
POLL_MS = 250

# Layout, key matrix and the input protocol live in emu/skin.py: they are
# arithmetic and data, and keeping them out of here lets the tests import
# them on a machine with no display.
from emu.skin import (  # noqa: E402
    BALL_COL, BALL_GAP, BEZEL, BODY_CX, BODY_H, BODY_W, BODY_X0, BODY_X1,
    BODY_Y0, BODY_Y1, CANVAS_H, CANVAS_W, DESK, FOOT, GAP, HEAD, KB_INSET,
    KEY_NAMES, KEY_ROWS, KEYPAD_H, KEYSYMS, LETTER_KEYS, RADIUS, SCREEN_H,
    SCREEN_W, SCREEN_X, SCREEN_Y, SIDE, InputWriter, panel_point,
)

# Lit from the top left throughout: highlights go on top and left edges,
# shadows on bottom and right. Consistency is what sells the depth, since
# tkinter has neither gradients nor alpha to sell it with.
C_DESK = "#0f1113"
C_EDGE = "#05070a"
C_SHELL = "#22272b"
C_SHELL_LIT = "#3b4249"
C_SHELL_DIM = "#171b1e"
C_WELL = "#0d1012"
C_PLATE = "#1c2125"
C_KEY = "#31373d"
C_KEY_LIT = "#4a525a"
C_KEY_DIM = "#0e1113"
C_LEGEND = "#ccd2d8"
C_LEGEND_DIM = "#8c959d"
C_BRAND = "#aab2ba"
C_PAPER = "#d5d7cf"        # e-paper white is a grey; a pure white looks like LCD
C_PAPER_RIM = "#b4b7ae"
C_GHOST = "#c6c9c0"
C_IDLE_INK = "#8b9088"
C_LED = "#5fbf6a"

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
                 events: "InputWriter | None" = None):
        self.root = root
        self.path = path
        self.scale = scale
        self.events = events
        self.key_hits: list[tuple[float, float, float, float, str]] = []
        self.pressed: int | None = None
        self.touching = False
        self.frames = 0
        self.stamp: tuple[int, int] | None = None
        self.photo: tk.PhotoImage | None = None
        self.note = "waiting"

        self.ui = _family(root, "Segoe UI", "DejaVu Sans", "Helvetica")
        self.mono = _family(root, "Consolas", "DejaVu Sans Mono", "Courier")

        self.canvas = tk.Canvas(root, width=CANVAS_W * scale, height=CANVAS_H * scale,
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

    def _panel_point(self, x: float, y: float) -> tuple[int, int] | None:
        return panel_point(x, y, self.scale)

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
        label = self._key_at(event.x, event.y)
        if label is not None:
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
        if self.pressed is not None:
            label = self._flash(None, False)
            if label:
                self.events.key(KEY_NAMES.get(label, label), False)

    def _on_keypress(self, event: "tk.Event") -> None:
        name = self._keysym(event)
        if name:
            self.events.key(name, True)
            self._flash(name if name != "ENTER" else "ENT", True)

    def _on_keyrelease(self, event: "tk.Event") -> None:
        name = self._keysym(event)
        if name:
            self.events.key(name, False)
            self._flash(None, False)

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
        self._draw_shadow()
        self._draw_side_buttons()
        self._draw_body()
        self._draw_head()
        self._draw_bezel()
        self._draw_keypad()
        self._draw_foot()

    def _draw_shadow(self) -> None:
        # Stipple is the only translucency Tk offers; two coarse passes at
        # increasing offset read as a soft contact shadow.
        for offset, pattern in ((3, "gray50"), (7, "gray25")):
            self._rr(BODY_X0 + offset, BODY_Y0 + offset, BODY_X1 + offset,
                     BODY_Y1 + offset, RADIUS, fill="#000000", outline="",
                     stipple=pattern)

    def _draw_side_buttons(self) -> None:
        # Drawn before the shell so the body overlaps their inner half and they
        # look moulded in rather than stuck on.
        for y0, y1 in ((HEAD + 70, HEAD + 96), (HEAD + 110, HEAD + 156)):
            self._rr(BODY_X1 - 4, DESK + y0, BODY_X1 + 5, DESK + y1, 4,
                     fill="#1a1e21", outline=C_EDGE)
            self._rr(BODY_X1 - 4, DESK + y0, BODY_X1 + 5, DESK + y0 + 3, 2,
                     fill="#2f363c", outline="")
        self._rr(BODY_X0 - 5, DESK + HEAD + 84, BODY_X0 + 4, DESK + HEAD + 110, 4,
                 fill="#1a1e21", outline=C_EDGE)

        # SMA stub for the LoRa radio, top right: the one feature that makes the
        # silhouette read as this board and not a generic slab.
        self._rr(BODY_X1 - 52, BODY_Y0 - 5, BODY_X1 - 36, BODY_Y0 + 6, 3,
                 fill="#20262b", outline=C_EDGE)
        self._oval(BODY_X1 - 44, BODY_Y0 - 7, 4.5, fill="#3a4248", outline=C_EDGE)
        self._oval(BODY_X1 - 44, BODY_Y0 - 7, 1.6, fill="#c8b273", outline="")

    def _draw_body(self) -> None:
        self._rr(BODY_X0, BODY_Y0, BODY_X1, BODY_Y1, RADIUS, fill=C_EDGE, outline="")
        self._rr(BODY_X0 + 1.5, BODY_Y0 + 1.5, BODY_X1 - 1.5, BODY_Y1 - 1.5,
                 RADIUS - 1, fill=C_SHELL_LIT, outline="")
        self._rr(BODY_X0 + 3, BODY_Y0 + 3.5, BODY_X1 - 3, BODY_Y1 - 2.5,
                 RADIUS - 2, fill=C_SHELL_DIM, outline="")
        self._rr(BODY_X0 + 3, BODY_Y0 + 3, BODY_X1 - 3, BODY_Y1 - 4,
                 RADIUS - 3, fill=C_SHELL, outline="")

        # A seam where the two shell halves meet, just inside the edge.
        self._rr(BODY_X0 + 6, BODY_Y0 + 6, BODY_X1 - 6, BODY_Y1 - 7, RADIUS - 6,
                 fill="", outline="#2b3136")

    def _draw_head(self) -> None:
        band = DESK + 8
        self._oval(BODY_X0 + 22, band, 2.6, fill="#141719", outline="")
        self._oval(BODY_X0 + 22, band, 1.6, fill=C_LED, outline="")

        # Grille holes rather than slots: at 2x a slot is three pixels tall and
        # turns into a smear.
        for col in range(10):
            for row in (-3.0, 3.0):
                self._oval(BODY_X0 + 34 + col * 6, band + row, 1.4,
                           fill="#0e1113", outline="")

        self._text(BODY_CX, band, "LILYGO", 9, C_BRAND, bold=True)
        self._text(BODY_X1 - 18, band, "T-DECK MAX", 6, C_LEGEND_DIM, anchor="e")

    def _draw_bezel(self) -> None:
        x0, y0 = SCREEN_X, SCREEN_Y
        x1, y1 = SCREEN_X + SCREEN_W, SCREEN_Y + SCREEN_H

        self._rr(x0 - BEZEL - 4, y0 - BEZEL - 4, x1 + BEZEL + 4, y1 + BEZEL + 4, 10,
                 fill=C_PLATE, outline="#2c3238")
        # The well: dark all round, with the lit edge at the bottom right, which
        # is what tells the eye this is a hole and not a raised block.
        self._rr(x0 - BEZEL, y0 - BEZEL, x1 + BEZEL, y1 + BEZEL, 6,
                 fill=C_WELL, outline=C_EDGE)
        self.canvas.create_line(*self.s(x0 - BEZEL + 2, y1 + BEZEL - 1,
                                        x1 + BEZEL - 2, y1 + BEZEL - 1),
                                fill="#31383e")
        # White FPC margin around the active area, as on the bare module.
        self.canvas.create_rectangle(*self.s(x0 - 3, y0 - 3, x1 + 3, y1 + 3),
                                     fill=C_PAPER_RIM, outline="#8f9289")

    def _draw_keypad(self) -> None:
        kx0, ky0 = BODY_X0 + KB_INSET, DESK + HEAD + SCREEN_H + GAP
        kx1 = BODY_X1 - KB_INSET
        ball_cx = kx1 - BALL_COL / 2
        col = ((kx1 - kx0) - BALL_COL - BALL_GAP) / 10
        pitch = KEYPAD_H / 4

        for r, row in enumerate(KEY_ROWS):
            y = ky0 + r * pitch + 1.5
            x = kx0
            for label, span in row:
                w = col * span
                self._key(x + 1.5, y, x + w - 1.5, y + pitch - 4.5, label)
                x += w

        cy = ky0 + KEYPAD_H / 2
        self._trackball(ball_cx, cy)
        for dy in (-34, 34):
            self._oval(ball_cx, cy + dy, 6.5, fill="#272c31", outline=C_KEY_DIM)
            self._oval(ball_cx, cy + dy - 0.8, 5, fill="#333940", outline="")

    def _key(self, x0: float, y0: float, x1: float, y1: float, label: str) -> None:
        self.key_hits.append((x0, y0, x1, y1, label))
        self._rr(x0, y0 + 1.2, x1, y1 + 1.2, 3, fill=C_KEY_DIM, outline="")
        self._rr(x0, y0, x1, y1, 3, fill=C_KEY, outline="#1b1f22")
        self.canvas.create_line(*self.s(x0 + 2.5, y0 + 1.2, x1 - 2.5, y0 + 1.2),
                                fill=C_KEY_LIT)
        letter = label in LETTER_KEYS
        self._text((x0 + x1) / 2, (y0 + y1) / 2, label,
                   9 if letter else 5.5,
                   C_LEGEND if letter else C_LEGEND_DIM, bold=letter)

    def _trackball(self, cx: float, cy: float) -> None:
        self._oval(cx, cy, 15, fill=C_WELL, outline=C_EDGE)
        self._oval(cx, cy - 0.5, 12.5, fill="#3d454c", outline="#20252a")
        self._oval(cx - 3.5, cy - 4, 5, fill="#69737c", outline="", stipple="gray50")

    def _draw_foot(self) -> None:
        y = BODY_Y1 - 9
        self._rr(BODY_CX - 15, y, BODY_CX + 15, y + 7, 3.5,
                 fill="#0b0e10", outline="#2c3237")
        self._text(BODY_X0 + 16, y + 3, "ESP32-S3  SX1262  240x320",
                   5.5, "#474e54", anchor="w")

    # -- the screen ------------------------------------------------------

    def _show_idle(self) -> None:
        """A powered panel with nothing written to it still shows something --
        the last image, faintly. A blank window would read as a broken app."""
        self.canvas.delete("screen")
        x0, y0 = SCREEN_X, SCREEN_Y
        x1, y1 = SCREEN_X + SCREEN_W, SCREEN_Y + SCREEN_H
        self.canvas.create_rectangle(*self.s(x0, y0, x1, y1), fill=C_PAPER,
                                     outline="", tags="screen")
        # Ghosting bands, kept clear of the middle so they do not read as a
        # strikethrough across the text.
        for gy in (y0 + 55, y0 + 80, y0 + 240, y0 + 265):
            self.canvas.create_line(*self.s(x0 + 20, gy, x1 - 20, gy),
                                    fill=C_GHOST, tags="screen")
        self.canvas.create_rectangle(*self.s(x0 + 12, y0 + 12, x1 - 12, y1 - 12),
                                     outline=C_PAPER_RIM, dash=(4, 4), tags="screen")
        self._text((x0 + x1) / 2, y0 + 140, "NO SIGNAL", 18, C_IDLE_INK,
                   bold=True, tags="screen")
        self._text((x0 + x1) / 2, y0 + 168, "panel has not refreshed yet", 8.5,
                   C_IDLE_INK, tags="screen")
        # Whatever --frames was given can be arbitrarily long; unelided it
        # runs straight off the glass and over the bezel.
        self._text((x0 + x1) / 2, y0 + 190,
                   self._elide(self._short_path(), (SCREEN_W - 20) * self.scale,
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
        box_w, box_h = SCREEN_W * self.scale, SCREEN_H * self.scale
        take_w, take_h = min(src.width(), box_w), min(src.height(), box_h)
        shown = tk.PhotoImage(master=self.canvas, width=box_w, height=box_h)
        shown.put(C_PAPER, to=(0, 0, box_w, box_h))
        shown.tk.call(shown, "copy", src, "-from", 0, 0, take_w, take_h,
                      "-to", (box_w - take_w) // 2, (box_h - take_h) // 2)

        self.canvas.delete("screen")
        # Tk frees a photo when its last Python reference drops, so the new one
        # has to be held before the old is released or the screen flashes.
        self.photo = shown
        self.canvas.create_image(*self.s(SCREEN_X, SCREEN_Y), image=shown,
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
        finally:
            self.root.after(POLL_MS, self._poll)

    def _refresh_status(self) -> None:
        head = f"frames {self.frames:<3d} last {self.note:<13s} "
        budget = CANVAS_W * self.scale - 12 * self.scale - self.metrics.measure(head)
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
    args = ap.parse_args(argv)

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

    root.title("BoardEmul - LilyGO T-Deck Max")
    root.configure(background=C_DESK)
    root.resizable(False, False)
    root.bind("<Escape>", lambda _e: root.destroy())

    app = DeckWindow(root, path, args.scale, events)

    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = max(0, (root.winfo_screenwidth() - w) // 2)
    y = max(0, (root.winfo_screenheight() - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")

    print(f"panel   : {app.path}")
    print(f"scale   : {args.scale}x ({SCREEN_W * args.scale}x{SCREEN_H * args.scale})")
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
