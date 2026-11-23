#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The window's first screen: pick a device, start it (ADR 0134).

The language is Material 3, dark: flat tonal surfaces — background, surface,
surface-variant — where elevation is a step in tone rather than a bevel, a
single accent family, state layers (a wash of white or primary over a
control for hover, press and selection), pill-shaped buttons in three weights
(filled, tonal, text), a search field that filters as you type, and a detail
card that shows the selected device's own face with its specification
underneath. Pressing a control ripples.

The screen is one anti-aliased image, repainted on every state change; the
system's font carries everything readable over it. The face is rendered the
way the faces are rendered, by the face modules themselves.

Keyboard: up and down choose, Enter starts, Escape leaves, "/" focuses the
filter. The list scrolls with the wheel. A run started here is the window's
to stop; this module only decides.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

from emu.raster import Raster, hex_colour, mix

# -- Material 3 dark, as tone steps ------------------------------------------
C_BACKGROUND = hex_colour("#0f1113")       # the desk
C_SURFACE = hex_colour("#191c20")          # cards, fields
C_SURFACE_HIGH = hex_colour("#22262c")     # hovered / tonal buttons
C_SURFACE_VARIANT = hex_colour("#2a2f36")
C_OUTLINE = hex_colour("#343a41")
C_PRIMARY = hex_colour("#a8c7fa")          # filled buttons; dark ink on it
C_ON_PRIMARY = "#0b2a4d"
C_PRIMARY_DIM = hex_colour("#5a7fb0")
C_SECONDARY_CONTAINER = hex_colour("#2e3f56")
C_ON_SECONDARY = "#cfe0f5"
C_ERROR = hex_colour("#f2b8b5")
C_INK = "#e8eaec"
C_INK_DIM = "#9aa0a6"
C_INK_FAINT = "#5f656d"
C_INK_DIM_RGB = hex_colour("#9aa0a6")
C_WHITE = hex_colour("#ffffff")

FONT = "Segoe UI"

# Layout, in scale units.
W, H = 744, 640
BAR_Y = 30                                  # the app bar's first line
SEARCH_Y0, SEARCH_Y1 = 96, 136
LIST_X0, LIST_X1 = 26, 452
LIST_Y0, LIST_Y1 = 152, 500
CARD_X0, CARD_X1 = 468, W - 26
CARD_Y0, CARD_Y1 = 152, 500
FOOT_Y0, FOOT_Y1 = 520, 576
ROW_H, ROW_GAP, ROW_R = 68, 10, 15
FIELD_R = 12


class Chooser:
    """One screen, repainted on every state change into a single image."""

    def __init__(self, root: tk.Tk, devices: Path, scale: int = 2,
                 creator=None, askdirectory=None):
        self.devices = devices
        self.scale = scale
        self.creator = creator
        self.askdirectory = askdirectory or self._ask_directory
        self.pick: tuple[str, object] | None = None
        self.devices_list: list = []        # every device
        self.shown: list = []               # the ones the filter lets through
        self.selected: int | None = None    # index into shown
        self.filter_text = ""
        self.scroll = 0
        self.hover: str | None = None
        self.pressed: str | None = None
        self.ripple: tuple[str, float, float, float] | None = None
        self.state = "list"                 # or "form"
        self.form_board_index = 0
        self.form_firmware: Path | None = None
        self.boards = ("tdeck-max", "tdeck")
        self.note_text = ""
        self.note_until = 0.0

        self.top = tk.Toplevel(root)
        self.top.title("BoardEmul — devices")
        self.top.configure(background="#0f1113")
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self._quit)
        self.top.bind("<Escape>", lambda _e: self._quit())
        self.top.bind("<Return>", lambda _e: self._start())
        self.top.bind("<Down>", lambda _e: self._move(1))
        self.top.bind("<Up>", lambda _e: self._move(-1))
        self.top.bind("<MouseWheel>", self._wheel)
        self.top.bind("<Button-4>", lambda _e: self._wheel_step(-1))
        self.top.bind("<Button-5>", lambda _e: self._wheel_step(1))
        self.top.bind("/", self._slash)
        self.filter_entry: tk.Entry | None = None

        self.ui_label = tkfont.Font(family=FONT, size=-9 * scale)
        self.ui = tkfont.Font(family=FONT, size=-10 * scale)
        self.ui_small = tkfont.Font(family=FONT, size=-9 * scale)
        self.ui_medium = tkfont.Font(family=FONT, size=-11 * scale)
        self.ui_bold = tkfont.Font(family=FONT, size=-13 * scale, weight="bold")
        self.ui_headline = tkfont.Font(family=FONT, size=-22 * scale, weight="bold")
        self.ui_title = tkfont.Font(family=FONT, size=-11 * scale, weight="bold")

        self.canvas = tk.Canvas(self.top, highlightthickness=0,
                                background="#0f1113")
        self.canvas.pack()
        self.widgets: list[tk.Widget] = []
        self.faces: dict[str, Raster] = {}
        self.cards: dict[str, Raster] = {}

        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", lambda _e: self._start())
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))

        self._reload()
        self._render()
        self._make_search_entry()

        self.top.update_idletasks()
        x = max(0, (self.top.winfo_screenwidth() - self.top.winfo_reqwidth()) // 2)
        y = max(0, (self.top.winfo_screenheight() - self.top.winfo_reqheight()) // 3)
        self.top.geometry(f"+{x}+{y}")

    # -- geometry -----------------------------------------------------------

    @property
    def s(self) -> float:
        return float(self.scale)

    def _X(self, v: float) -> float:
        return v * self.s

    @property
    def width(self) -> int:
        return int(W * self.s)

    @property
    def height(self) -> int:
        return int(H * self.s)

    def _list_rect(self) -> tuple[float, float, float, float]:
        return self._X(LIST_X0), self._X(LIST_Y0), self._X(LIST_X1), self._X(LIST_Y1)

    def _card_rect(self) -> tuple[float, float, float, float]:
        return self._X(CARD_X0), self._X(CARD_Y0), self._X(CARD_X1), self._X(CARD_Y1)

    def _visible_rows(self) -> int:
        _lx0, ly0, _lx1, ly1 = self._list_rect()
        return max(1, int((ly1 - ly0) / ((ROW_H + ROW_GAP) * self.s)))

    def _buttons(self) -> dict[str, tuple[float, float, float, float]]:
        s = self.s
        buttons = {
            "start": (self._X(26), self._X(FOOT_Y0), self._X(220), self._X(FOOT_Y1)),
            "new": (self._X(234), self._X(FOOT_Y0), self._X(420), self._X(FOOT_Y1)),
            "watch": (self._X(434), self._X(FOOT_Y0), self._X(586), self._X(FOOT_Y1)),
            "close": (self._X(W - 78), self._X(BAR_Y - 8),
                      self._X(W - 30), self._X(BAR_Y + 40)),
            "board": (self._X(150), self._X(356), self._X(400), self._X(392)),
            "firmware": (self._X(150), self._X(436), self._X(400), self._X(470)),
            "create": (self._X(26), self._X(FOOT_Y0), self._X(250), self._X(FOOT_Y1)),
            "cancel": (self._X(264), self._X(FOOT_Y0), self._X(400), self._X(FOOT_Y1)),
        }
        if self.state == "list":
            for gone in ("board", "firmware", "create", "cancel"):
                buttons.pop(gone)
        else:
            for gone in ("start", "new", "watch", "close"):
                buttons.pop(gone)
        return buttons

    def _search_rect(self) -> tuple[float, float, float, float]:
        return self._X(26), self._X(SEARCH_Y0), self._X(W - 26), self._X(SEARCH_Y1)

    # -- data ---------------------------------------------------------------

    def _reload(self) -> None:
        from emu import device as device_folder
        self.devices_list = device_folder.all_devices(self.devices)
        needle = self.filter_text.strip().lower()
        self.shown = [d for d in self.devices_list
                      if needle in d.name.lower()] if needle else list(self.devices_list)
        if self.selected is None or self.selected >= len(self.shown):
            self.selected = 0 if self.shown else None
        self.scroll = max(0, min(self.scroll,
                                 max(0, len(self.shown) - self._visible_rows())))

    def rows(self) -> list[str]:
        """What each row says, for tests and for the blind."""
        return [f"{d.name} — {d.board}, {'card' if d.has_sdcard else 'no card'}"
                for d in self.shown]

    def select(self, index: int) -> None:
        if self.shown and 0 <= index < len(self.shown):
            self.selected = index
            self._keep_visible()
            self._render()

    def _keep_visible(self) -> None:
        if self.selected is None:
            return
        visible = self._visible_rows()
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + visible:
            self.scroll = self.selected - visible + 1
        self.scroll = max(0, self.scroll)

    def _move(self, step: int) -> None:
        if not self.shown:
            return
        at = (self.selected or 0) + step
        self.select(max(0, min(len(self.shown) - 1, at)))

    def _wheel(self, event) -> None:
        self._wheel_step(-1 if event.delta > 0 else 1)

    def _wheel_step(self, rows: int) -> None:
        if self.state != "list":
            return
        ceiling = max(0, len(self.shown) - self._visible_rows())
        if self.scroll != max(0, min(ceiling, self.scroll + rows)):
            self.scroll = max(0, min(ceiling, self.scroll + rows))
            self._render()

    # -- actions ------------------------------------------------------------

    def _start(self) -> None:
        if self.state != "list" or self.selected is None:
            return
        self.pick = ("device", self.shown[self.selected])
        self.top.destroy()

    def _quit(self) -> None:
        self.pick = None
        self.top.destroy()

    def _watch(self) -> None:
        chosen = self.askdirectory()
        if chosen:
            self.pick = ("watch", Path(chosen))
            self.top.destroy()

    def _region_at(self, x: float, y: float) -> str | None:
        for name, (x0, y0, x1, y1) in self._buttons().items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        sx0, sy0, sx1, sy1 = self._search_rect()
        if sx0 <= x <= sx1 and sy0 <= y <= sy1:
            return "search"
        return None

    def _row_at(self, y: float) -> int | None:
        _lx0, ly0, _lx1, ly1 = self._list_rect()
        if not ly0 <= y <= ly1:
            return None
        at = self.scroll + int((y - ly0) // ((ROW_H + ROW_GAP) * self.s))
        if 0 <= at < len(self.shown):
            return at
        return None

    def _press(self, event) -> None:
        hit = self._region_at(event.x, event.y)
        if hit == "search":
            for widget in self.widgets:
                if isinstance(widget, tk.Entry):
                    widget.focus_set()
            return
        if hit:
            self.pressed = hit
            cx, cy = event.x, event.y
            self.ripple = (hit, cx, cy, 6 * self.s)
            self._render()
            self._ripple_out(hit, cx, cy)
            return
        at = self._row_at(event.y)
        if at is not None:
            self.select(at)

    def _ripple_out(self, hit: str, cx: float, cy: float) -> None:
        x0, y0, x1, y1 = self._buttons()[hit]
        reach = max(x1 - x0, y1 - y0) * 0.9
        step = max(4 * self.s, reach / 4)

        def grow() -> None:
            if self.ripple is None or self.ripple[0] != hit:
                return
            _hit, rx, ry, r = self.ripple
            r += step
            if r >= reach:
                self.ripple = None
                self._render()
                return
            self.ripple = (hit, rx, ry, r)
            self._render()
            self.top.after(40, grow)

        self.top.after(40, grow)

    def _release(self, event) -> None:
        pressed, self.pressed = self.pressed, None
        action = {"start": self._start, "new": self._form, "watch": self._watch,
                  "quit": self._quit, "create": self._create,
                  "cancel": self._cancel, "board": self._cycle_board,
                  "firmware": self._choose_firmware,
                  "close": self._quit}.get(pressed)
        if action is not None and self._region_at(event.x, event.y) == pressed:
            self.ripple = None
            action()
            return
        self._render()

    def _motion(self, event) -> None:
        hover = self._region_at(event.x, event.y)
        if hover is None and self.state == "list":
            at = self._row_at(event.y)
            if at is not None:
                hover = f"row{at}"
        self.canvas.configure(cursor="hand2" if hover and hover != "search"
                              else "xterm" if hover == "search" else "")
        self._set_hover(hover)

    def _set_hover(self, hover: str | None) -> None:
        if hover != self.hover:
            self.hover = hover
            self._render()

    # -- the filter field ---------------------------------------------------

    def _filter_changed(self, *_ignored) -> None:
        text = self.filter_var.get().strip()
        if text == self.filter_text:
            return
        keep = (self.shown[self.selected].name
                if self.selected is not None and self.shown else None)
        self.filter_text = text
        self._reload()
        if keep:
            for index, device in enumerate(self.shown):
                if device.name == keep:
                    self.selected = index
                    break
        self._keep_visible()
        self._render()

    def _focus_search(self) -> None:
        self._make_search_entry()
        if self.filter_entry is not None:
            self.filter_entry.focus_set()

    def _slash(self, _event) -> None:
        self._focus_search()
        return "break"

    def _make_search_entry(self) -> None:
        if self.filter_entry is not None:
            return
        self.filter_var = tk.StringVar(value=self.filter_text)
        self.filter_entry = tk.Entry(
            self.top, textvariable=self.filter_var, width=30,
            background="#191c20", foreground="#e8eaec",
            insertbackground="#a8c7fa", relief="flat")
        self.filter_entry.place(x=int(72 * self.s),
                                y=int((SEARCH_Y0 + SEARCH_Y1) / 2 * self.s
                                      - 12 * self.s))
        self.filter_entry.bind("<KeyRelease>", self._filter_changed)
        self.widgets.append(self.filter_entry)

    # -- the new-device form ------------------------------------------------

    def _form(self) -> None:
        self.state = "form"
        self.form_firmware = None
        self.form_board_index = 0
        self.note_text = ""
        self.filter_entry = None
        self._render()
        self.form_name_var = tk.StringVar()
        entry = tk.Entry(self.top, textvariable=self.form_name_var, width=30,
                         background="#191c20", foreground="#e8eaec",
                         insertbackground="#a8c7fa", relief="flat")
        entry.place(x=int(158 * self.s), y=int(286 * self.s))
        self.widgets.append(entry)
        entry.focus_set()

    def _cancel(self) -> None:
        self.state = "list"
        self._clear_widgets()
        self.note_text = ""
        self._reload()
        self._render()
        self._make_search_entry()

    def _cycle_board(self) -> None:
        self.form_board_index = (self.form_board_index + 1) % len(self.boards)
        self._render()

    def _choose_firmware(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askopenfilename(
            title="Which firmware image?",
            filetypes=[("firmware images", "*.bin"), ("all files", "*")])
        if chosen:
            self.form_firmware = Path(chosen)
            self.note_text = ""
            self._render()

    def _create(self) -> None:
        name = self.form_name_var.get().strip() if hasattr(self, "form_name_var") else ""
        if not name:
            return self._note("give the device a name")
        if self.form_firmware is None:
            return self._note("choose a firmware image")
        try:
            if self.creator is not None:
                device = self.creator(self.devices, name, self.form_firmware)
            else:
                from emu import device as device_folder
                device = device_folder.create(
                    self.devices, name, self.form_firmware,
                    board=self.boards[self.form_board_index])
        except Exception as exc:
            return self._note(str(exc))
        self.pick = ("device", device)
        self.top.destroy()

    def _note(self, text: str) -> None:
        self.note_text = text
        self.note_until = time_monotonic() + 6.0
        self._render()

    def _clear_widgets(self) -> None:
        for widget in self.widgets:
            widget.destroy()
        self.widgets = []
        self.filter_entry = None

    @staticmethod
    def _ask_directory():
        from tkinter import filedialog
        return filedialog.askdirectory(title="Watch which run's frames?")

    # -- painting -----------------------------------------------------------

    def _render(self) -> None:
        s = self.s
        img = Raster(self.width, self.height, C_BACKGROUND)

        if self.state == "list":
            self._paint_search(img)
            self._paint_list(img)
            self._paint_card(img)
        else:
            self._paint_form(img)

        for name, (x0, y0, x1, y1) in self._buttons().items():
            if name in ("board", "firmware"):
                continue
            pressed = self.pressed == name
            primary = name in ("start", "create")
            if primary:
                fill = C_PRIMARY
                if pressed:
                    fill = mix(C_PRIMARY, C_BACKGROUND, 0.25)
                _rr(img, x0, y0, x1, y1, (y1 - y0) / 2, fill)
            elif name in ("watch", "cancel"):
                pass                        # a text button has no surface
            else:
                fill = C_SURFACE_HIGH
                if pressed:
                    fill = mix(C_SURFACE_HIGH, C_BACKGROUND, 0.3)
                elif self.hover == name:
                    fill = mix(C_SURFACE_HIGH, C_WHITE, 0.08)
                _rr(img, x0, y0, x1, y1, (y1 - y0) / 2, fill)
            if self.ripple and self.ripple[0] == name:
                _hit, rx, ry, r = self.ripple
                self._paint_ripple(img, x0, y0, x1, y1, rx, ry, r)

        self.canvas.delete("all")
        self.photo = tk.PhotoImage(data=img.tk_data())
        self.canvas.configure(width=self.width, height=self.height)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.image = self.photo
        self._paint_text()

    def _paint_ripple(self, img: Raster, x0, y0, x1, y1, cx, cy, r) -> None:
        """A circle of white at rest on the control, clipped to it — the
        Material response to a press."""
        wash = mix(C_SURFACE, C_WHITE, 0.14)
        for y in range(max(int(y0), int(cy - r)), min(int(y1), int(cy + r))):
            for x in range(max(int(x0), int(cx - r)), min(int(x1), int(cx + r))):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    i = (y * img.w + x) * 3
                    img.px[i] = min(255, img.px[i] + (wash[0] - img.px[i]) // 3)
                    img.px[i + 1] = min(255, img.px[i + 1] + (wash[1] - img.px[i + 1]) // 3)
                    img.px[i + 2] = min(255, img.px[i + 2] + (wash[2] - img.px[i + 2]) // 3)

    def _paint_search(self, img: Raster) -> None:
        x0, y0, x1, y1 = self._search_rect()
        _rr(img, x0, y0, x1, y1, (y1 - y0) / 2, C_SURFACE)
        # The magnifier, drawn: a ring and a handle.
        s = self.s
        cx, cy = x0 + 22 * s, (y0 + y1) / 2
        r = 6.5 * s
        for y in range(int(cy - r) - 1, int(cy + r) + 2):
            for x in range(int(cx - r) - 1, int(cx + r) + 2):
                d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                if r * 0.72 <= d <= r:
                    i = (y * img.w + x) * 3
                    img.px[i:i + 3] = C_INK_DIM_RGB
        dx, dy = 5.2 * s, 5.2 * s
        img.segment(cx + dx * 0.7, cy + dy * 0.7, cx + dx * 1.9, cy + dy * 1.9,
                    2.2 * s, C_INK_DIM_RGB)

    def _paint_list(self, img: Raster) -> None:
        """The rows as cards, painted into their own image and clipped."""
        s = self.s
        lx0, ly0, lx1, ly1 = self._list_rect()
        w, h = int(lx1 - lx0), int(ly1 - ly0)
        strip = Raster(w, h, C_BACKGROUND)
        visible = self._visible_rows() + 1
        for at in range(self.scroll, min(len(self.shown), self.scroll + visible)):
            index = at - self.scroll
            y0 = index * (ROW_H + ROW_GAP) * s
            y1 = y0 + ROW_H * s
            selected = at == self.selected
            hovered = f"row{at}" == self.hover
            if selected:
                fill = mix(C_SURFACE, C_PRIMARY_DIM, 0.30)
            elif hovered:
                fill = mix(C_SURFACE, C_WHITE, 0.07)
            else:
                fill = C_SURFACE
            _rr(strip, 0, y0, w, y1, ROW_R * s, fill)
            if selected:
                _rr(strip, 0, y0 + (y1 - y0) / 2 - 12 * s, 5 * s,
                    y0 + (y1 - y0) / 2 + 12 * s, 2.5 * s, C_PRIMARY)
        _blit(img, strip, int(lx0), int(ly0))

    def _paint_card(self, img: Raster) -> None:
        """The detail card: surface, then the device's own face, composited
        without its desk so it sits on the card."""
        s = self.s
        x0, y0, x1, y1 = self._card_rect()
        _rr(img, x0, y0, x1, y1, 16 * s, C_SURFACE)
        device = (self.shown[self.selected]
                  if self.selected is not None and self.shown else None)
        if device is None:
            return
        face = self.faces.get(device.board)
        if face is None:
            face = self._render_face(device.board)
            self.faces[device.board] = face
        if face is not None:
            _blit_keyed(img, face, int((x0 + x1) / 2 - face.w / 2),
                        int(y0 + 20 * s), C_BACKGROUND)

    def _render_face(self, board: str) -> Raster | None:
        from emu.skin import SKINS
        skin = SKINS.get(board)
        if skin is None or skin.face is None:
            return None
        from emu import max_face, tdeck_face
        module = {"max": max_face, "tdeck": tdeck_face}[skin.face]
        face = module.build(1)
        _x0, _y0, x1, _y1 = self._card_rect()
        want_w = int((x1 - _x0) - 48 * self.s)
        want_h = int(180 * self.s)
        return _shrink(face.raster, want_w, want_h)

    def _paint_form(self, img: Raster) -> None:
        s = self.s
        # The name's filled field, with the real entry over it.
        _rr(img, self._X(146), self._X(280), self._X(400), self._X(314),
            10 * s, C_SURFACE)
        # The board selector: a container with the chosen segment filled.
        bx0, by0, bx1, by1 = self._buttons()["board"]
        _rr(img, bx0, by0, bx1, by1, (by1 - by0) / 2, C_SURFACE)
        segment_w = (bx1 - bx0) / 2
        if self.form_board_index == 0:
            _rr(img, bx0 + 3, by0 + 3, bx0 + segment_w - 3, by1 - 3,
                (by1 - by0 - 6) / 2, C_SECONDARY_CONTAINER)
        else:
            _rr(img, bx0 + segment_w + 3, by0 + 3, bx1 - 3, by1 - 3,
                (by1 - by0 - 6) / 2, C_SECONDARY_CONTAINER)
        # The firmware chooser's field.
        fx0, fy0, fx1, fy1 = self._buttons()["firmware"]
        _rr(img, fx0, fy0, fx1, fy1, 10 * s, C_SURFACE)
        _rr(img, fx0, fy0, fx0 + 2 * s, fy1, 1 * s, C_PRIMARY_DIM)

    def _paint_text(self) -> None:
        s = self.s
        put = self.canvas.create_text
        # -- the app bar -----------------------------------------------------
        put(28 * s, self._X(BAR_Y - 4), text="BOARDEMUL", fill=C_INK_FAINT,
            font=self.ui_label, anchor="w")
        put(28 * s, self._X(BAR_Y + 14), text="Devices", fill=C_INK,
            font=self.ui_headline, anchor="w")
        if self.state == "list":
            count = len(self.shown)
            total = len(self.devices_list)
            meta = f"{count} of {total}" if count != total else f"{total}"
            put(self._X(W - 92), self._X(BAR_Y + 16), text=meta,
                fill=C_INK_FAINT, font=self.ui_small, anchor="e")
            self._icon_button_text("close", "×")

        if self.state == "list":
            # -- the search field ---------------------------------------------
            sx0, sy0, sx1, sy1 = self._search_rect()
            placeholder = "Filter by name    ( / to focus )"
            shown = self.filter_text or placeholder
            put(sx0 + self._X(44), (sy0 + sy1) / 2, text=shown,
                fill=C_INK if self.filter_text else C_INK_FAINT,
                font=self.ui, anchor="w")
            # -- the rows ------------------------------------------------------
            lx0, ly0, lx1, _ly1 = self._list_rect()
            for at in range(self.scroll,
                            min(len(self.shown),
                                self.scroll + self._visible_rows())):
                device = self.shown[at]
                y0 = ly0 + (at - self.scroll) * (ROW_H + ROW_GAP) * s
                selected = at == self.selected
                put(lx0 + self._X(20), y0 + self._X(17), text=device.name,
                    fill=C_INK, font=self.ui_bold, anchor="w")
                put(lx0 + self._X(20), y0 + self._X(42),
                    text=f"{device.board} · "
                         f"{_storage(device)}",
                    fill=C_INK_DIM, font=self.ui_small, anchor="w")
                put(lx1 - self._X(20), y0 + self._X(ROW_H / 2), text="›",
                    fill=C_INK_FAINT, font=self.ui_medium, anchor="e")
                if selected:
                    put(lx1 - self._X(46), y0 + self._X(ROW_H / 2), text="start ↵",
                        fill="#9ec1ee", font=self.ui_small, anchor="e")
            if not self.shown:
                cx = (lx0 + lx1) / 2
                put(cx, ly0 + self._X(110),
                    text="nothing matches" if self.filter_text else "no devices yet",
                    fill=C_INK_DIM, font=self.ui_bold, anchor="center")
                put(cx, ly0 + self._X(140),
                    text=("clear the filter to see the rest" if self.filter_text
                          else "make one — it keeps its storage between runs"),
                    fill=C_INK_FAINT, font=self.ui_small, anchor="center")

            # -- the detail card ------------------------------------------------
            device = (self.shown[self.selected]
                      if self.selected is not None and self.shown else None)
            if device is not None:
                cx0, _cy0, cx1, cy1 = self._card_rect()
                put((cx0 + cx1) / 2, cy1 - self._X(150), text=device.name,
                    fill=C_INK, font=self.ui_bold, anchor="center")
                specs = [
                    ("board", device.board),
                    ("mac", device.mac),
                    ("storage", _storage(device)),
                    ("created", (device.created or "").split("T")[0]),
                ]
                sy = cy1 - self._X(118)
                for label, value in specs:
                    put(cx0 + self._X(24), sy, text=label, fill=C_INK_FAINT,
                        font=self.ui_small, anchor="w")
                    put(cx1 - self._X(24), sy, text=value, fill=C_INK,
                        font=self.ui_small, anchor="e")
                    sy += self._X(24)
        else:
            put(self._X(40), self._X(216), text="New device", fill=C_INK,
                font=self.ui_headline, anchor="w")
            put(self._X(40), self._X(258), text="name", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(self._X(40), self._X(366), text="board", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(self._X(40), self._X(446), text="firmware", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            segment = self._buttons()["board"]
            other = 1 - self.form_board_index
            put(segment[0] + (segment[2] - segment[0]) * (0.25 if other else 0.75),
                (segment[1] + segment[3]) / 2,
                text=self.boards[other], fill=C_INK_FAINT, font=self.ui,
                anchor="center")
            put((segment[0] + segment[2]) / 2 if not other else 0, 0, text="",
                font=self.ui)
            chosen_x = segment[0] + (segment[2] - segment[0]) * (0.75 if not other else 0.25)
            put(chosen_x, (segment[1] + segment[3]) / 2,
                text=self.boards[self.form_board_index], fill=C_ON_SECONDARY,
                font=self.ui, anchor="center")
            fw = (self.form_firmware.name if self.form_firmware
                  else "choose an image…")
            shown = fw if len(fw) < 46 else "…" + fw[-45:]
            put(self._X(166), self._X(453), text=shown,
                fill=C_INK if self.form_firmware else C_INK_DIM,
                font=self.ui, anchor="w")

        labels = {"start": "Start", "new": "New device", "watch": "Watch folder…",
                  "quit": "Quit", "create": "Create device", "cancel": "Cancel"}
        for name, (x0, y0, x1, y1) in self._buttons().items():
            text = labels.get(name)
            if not text:
                continue
            pressed = self.pressed == name
            fill = C_ON_PRIMARY if name in ("start", "create") else C_INK
            if name in ("start", "new") and self.state == "list" \
                    and not self.devices_list and name == "start":
                fill = C_INK_FAINT
            put((x0 + x1) / 2, (y0 + y1) / 2 + (1 if pressed else 0),
                text=text, fill=fill,
                font=self.ui_medium, anchor="center")
        if self.note_text and time_monotonic() < self.note_until:
            put(self._X(W / 2), self.height - self._X(24), text=self.note_text,
                fill=C_INK, font=self.ui_small, anchor="center")

    def _icon_button_text(self, name: str, glyph: str) -> None:
        x0, y0, x1, y1 = self._buttons()[name]
        put = self.canvas.create_text
        put((x0 + x1) / 2, (y0 + y1) / 2, text=glyph, fill=C_INK_DIM,
            font=self.ui_headline, anchor="center")


def time_monotonic() -> float:
    import time
    return time.monotonic()


def _storage(device) -> str:
    parts = []
    try:
        mb = device.flash.stat().st_size / (1024 * 1024)
        parts.append(f"{mb:.0f} MB flash")
    except OSError:
        pass
    if device.has_sdcard:
        try:
            parts.append(f"{device.sdcard.stat().st_size / (1024 * 1024):.0f} MB card")
        except OSError:
            parts.append("card")
    return " · ".join(parts) or "no storage yet"


def _rr(img: Raster, x0, y0, x1, y1, r, paint) -> None:
    img.rrect(x0, y0, x1, y1, r, paint)


def _blit(dst: Raster, src: Raster, x: int, y: int) -> None:
    for sy in range(src.h):
        dy = y + sy
        if not 0 <= dy < dst.h:
            continue
        count = min(src.w, dst.w - x)
        if count <= 0:
            continue
        at = (dy * dst.w + x) * 3
        dst.px[at:at + count * 3] = src.px[(sy * src.w) * 3:
                                           (sy * src.w + count) * 3]


def _blit_keyed(dst: Raster, src: Raster, x: int, y: int, key: tuple) -> None:
    for sy in range(src.h):
        dy = y + sy
        if not 0 <= dy < dst.h:
            continue
        for sx in range(src.w):
            dx = x + sx
            if not 0 <= dx < dst.w:
                continue
            i = (sy * src.w + sx) * 3
            if (src.px[i], src.px[i + 1], src.px[i + 2]) == key:
                continue
            j = (dy * dst.w + dx) * 3
            dst.px[j:j + 3] = src.px[i:i + 3]


def _shrink(src: Raster, want_w: int, want_h: int) -> Raster:
    ratio = min(want_w / src.w, want_h / src.h)
    w = max(1, int(src.w * ratio))
    h = max(1, int(src.h * ratio))
    out = Raster(w, h)
    for ty in range(h):
        sy0 = int(ty / ratio)
        sy1 = max(sy0 + 1, int((ty + 1) / ratio))
        for tx in range(w):
            sx0 = int(tx / ratio)
            sx1 = max(sx0 + 1, int((tx + 1) / ratio))
            r = g = b = n = 0
            for sy in range(sy0, min(sy1, src.h)):
                base = sy * src.w
                for sx in range(sx0, min(sx1, src.w)):
                    i = (base + sx) * 3
                    r += src.px[i]
                    g += src.px[i + 1]
                    b += src.px[i + 2]
                    n += 1
            j = (ty * w + tx) * 3
            out.px[j], out.px[j + 1], out.px[j + 2] = r // n, g // n, b // n
    return out
