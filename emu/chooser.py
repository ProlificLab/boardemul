#!/usr/bin/env python3
"""The window's first screen: pick a device, start it (ADR 0134).

Drawn the way the device faces are drawn (ADR 0126): one anti-aliased image
for the shapes — the panel, the rows, the buttons — re-rendered on every
state change, with the system's font for everything readable over it. A row
is a card: the device's name, then its board and card. The selected device's
own face is rendered beside the list, so what you are about to open looks
like what it is.

Keyboard: up and down choose, Enter starts, Escape leaves. Double-click
starts. A run started here is the window's to stop, which is `main`'s
business; this module only decides.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

from emu.raster import Raster, hex_colour, mix

C_DESK = hex_colour("#0f1113")
C_PANEL = hex_colour("#131416")
C_RIM = hex_colour("#2c2e31")
C_WELL = hex_colour("#0d0f11")
C_ROW = hex_colour("#1a1d20")
C_ROW_HOVER = hex_colour("#232830")
C_ROW_SELECTED = hex_colour("#28303a")
C_ACCENT = hex_colour("#5a8fd0")
C_INK = "#e8eaec"
C_INK_DIM = "#8f979e"
C_BUTTON = hex_colour("#22262b")
C_BUTTON_PRIMARY = hex_colour("#31517a")
C_BUTTON_HOVER = hex_colour("#2c3138")

PAD = 14
ROW_H = 46
ROW_GAP = 8
ROW_R = 7
BTN_R = 7
TITLE_TOP = 30


class Chooser:
    """One screen, repainted on every state change into a single image."""

    def __init__(self, root: tk.Tk, devices: Path, scale: int = 2,
                 creator=None, askdirectory=None):
        self.devices = devices
        self.scale = scale
        self.creator = creator
        self.askdirectory = askdirectory or self._ask_directory
        self.pick: tuple[str, object] | None = None
        self.devices_list: list = []
        self.selected: int | None = None
        self.hover: str | None = None
        self.state = "list"                 # or "form"
        self.form_board_index = 0
        self.form_firmware: Path | None = None
        self.boards = ("tdeck-max", "tdeck")
        self.note_text = ""

        self.top = tk.Toplevel(root)
        self.top.title("BoardEmul — devices")
        self.top.configure(background="#0f1113")
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self._quit)
        self.top.bind("<Escape>", lambda _e: self._quit())
        self.top.bind("<Return>", lambda _e: self._start())
        self.top.bind("<Down>", lambda _e: self._move(1))
        self.top.bind("<Up>", lambda _e: self._move(-1))

        self.ui = tkfont.Font(family="Segoe UI", size=-10 * scale)
        self.ui_small = tkfont.Font(family="Segoe UI", size=-9 * scale)
        self.ui_bold = tkfont.Font(family="Segoe UI", size=-12 * scale,
                                   weight="bold")
        self.ui_title = tkfont.Font(family="Segoe UI", size=-18 * scale,
                                    weight="bold")

        self.canvas = tk.Canvas(self.top, highlightthickness=0,
                                background="#0f1113")
        self.canvas.pack()
        self.widgets: list[tk.Widget] = []
        self.preview_cache: dict[str, tk.PhotoImage] = {}

        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-Button-1>", lambda _e: self._start())
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))

        self._reload()
        self._render()

        self.top.update_idletasks()
        x = max(0, (self.top.winfo_screenwidth() - self.top.winfo_reqwidth()) // 2)
        y = max(0, (self.top.winfo_screenheight() - self.top.winfo_reqheight()) // 3)
        self.top.geometry(f"+{x}+{y}")

    # -- geometry -----------------------------------------------------------

    @property
    def s(self) -> float:
        return float(self.scale)

    @property
    def width(self) -> int:
        return int(560 * self.s)

    @property
    def height(self) -> int:
        return int(436 * self.s)

    def _list_rect(self) -> tuple[float, float, float, float]:
        s = self.s
        return PAD * s, 76 * s, 368 * s, 336 * s

    def _preview_rect(self) -> tuple[float, float, float, float]:
        s = self.s
        return 384 * s, 76 * s, self.width - PAD * s, 336 * s

    def _buttons(self) -> dict[str, tuple[float, float, float, float]]:
        s = self.s
        y0, y1 = 352 * s, 390 * s
        if self.state == "form":
            return {"create": (PAD * s, y0, 200 * s, y1),
                    "cancel": (208 * s, y0, 330 * s, y1),
                    "board": (140 * s, 126 * s, 352 * s, 152 * s),
                    "firmware": (140 * s, 156 * s, 352 * s, 182 * s)}
        return {"start": (PAD * s, y0, 150 * s, y1),
                "new": (158 * s, y0, 296 * s, y1),
                "watch": (304 * s, y0, 454 * s, y1),
                "quit": (462 * s, y0, self.width - PAD * s, y1)}

    # -- data ---------------------------------------------------------------

    def _reload(self) -> None:
        from emu import device as device_folder
        self.devices_list = device_folder.all_devices(self.devices)
        if self.selected is None or self.selected >= len(self.devices_list):
            self.selected = 0 if self.devices_list else None

    def rows(self) -> list[str]:
        """What each row says, for tests and for the blind."""
        return [f"{d.name} — {d.board}, {'card' if d.has_sdcard else 'no card'}"
                for d in self.devices_list]

    def select(self, index: int) -> None:
        if self.devices_list and 0 <= index < len(self.devices_list):
            self.selected = index
            self._render()

    def _move(self, step: int) -> None:
        if not self.devices_list:
            return
        at = (self.selected or 0) + step
        self.select(max(0, min(len(self.devices_list) - 1, at)))

    # -- actions ------------------------------------------------------------

    def _start(self) -> None:
        if self.state != "list" or self.selected is None:
            return
        self.pick = ("device", self.devices_list[self.selected])
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
        return None

    def _click(self, event) -> None:
        hit = self._region_at(event.x, event.y)
        actions = {"start": self._start, "new": self._form, "watch": self._watch,
                   "quit": self._quit, "create": self._create,
                   "cancel": self._cancel, "board": self._cycle_board,
                   "firmware": self._choose_firmware}
        if hit in actions:
            actions[hit]()
            return
        if self.state != "list":
            return
        lx0, ly0, lx1, _ly1 = self._list_rect()
        if not (lx0 <= event.x <= lx1):
            return
        at = int((event.y - ly0) // ((ROW_H + ROW_GAP) * self.s))
        if 0 <= at < len(self.devices_list):
            self.select(at)

    def _motion(self, event) -> None:
        hover = self._region_at(event.x, event.y)
        if hover is None and self.state == "list":
            lx0, ly0, lx1, _ly1 = self._list_rect()
            if lx0 <= event.x <= lx1:
                at = int((event.y - ly0) // ((ROW_H + ROW_GAP) * self.s))
                if 0 <= at < len(self.devices_list):
                    hover = f"row{at}"
        self._set_hover(hover)

    def _set_hover(self, hover: str | None) -> None:
        if hover != self.hover:
            self.hover = hover
            self._render()

    # -- the new-device form ------------------------------------------------

    def _form(self) -> None:
        self.state = "form"
        self.form_firmware = None
        self.form_board_index = 0
        self.note_text = ""
        self._render()
        self.form_name_var = tk.StringVar()
        entry = tk.Entry(self.top, textvariable=self.form_name_var, width=26,
                         background="#0d0f11", foreground="#e8eaec",
                         insertbackground="#e8eaec", relief="flat")
        entry.place(x=int(146 * self.s), y=int(92 * self.s))
        self.widgets.append(entry)
        entry.focus_set()

    def _cancel(self) -> None:
        self.state = "list"
        self._clear_widgets()
        self.note_text = ""
        self._render()

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
        self._render()

    def _clear_widgets(self) -> None:
        for widget in self.widgets:
            widget.destroy()
        self.widgets = []

    @staticmethod
    def _ask_directory():
        from tkinter import filedialog
        return filedialog.askdirectory(title="Watch which run's frames?")

    # -- painting -----------------------------------------------------------

    def _render(self) -> None:
        s = self.s
        img = Raster(self.width, self.height, C_DESK)
        _rr(img, 0, 0, self.width, self.height, 10 * s, C_PANEL)
        img.rrect(0, 0, 2 * s, self.height, 1 * s, C_RIM)

        regions = self._buttons()
        if self.state == "list":
            self._paint_list(img)
            self._paint_preview_well(img)
        else:
            self._paint_form(img)

        for name, (x0, y0, x1, y1) in regions.items():
            primary = name in ("start", "create")
            fill = C_BUTTON_PRIMARY if primary else C_BUTTON
            if self.hover == name:
                fill = mix(fill, C_BUTTON_HOVER if not primary else hex_colour("#3d6292"),
                           0.9)
            _rr(img, x0, y0, x1, y1, BTN_R * s, C_RIM)
            _rr(img, x0 + 1, y0 + 1, x1 - 1, y1 - 1, (BTN_R - 1) * s, fill)

        self.canvas.delete("all")
        self.photo = tk.PhotoImage(data=img.tk_data())
        self.canvas.configure(width=self.width, height=self.height)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.image = self.photo
        if self.state == "list":
            self._paint_preview_photo()
        self._paint_text()

    def _paint_list(self, img: Raster) -> None:
        s = self.s
        lx0, ly0, lx1, _ly1 = self._list_rect()
        for index in range(len(self.devices_list)):
            y0 = ly0 + index * (ROW_H + ROW_GAP) * s
            y1 = y0 + ROW_H * s
            if index == self.selected:
                fill = C_ROW_SELECTED
            elif f"row{index}" == self.hover:
                fill = C_ROW_HOVER
            else:
                fill = C_ROW
            _rr(img, lx0, y0, lx1, y1, ROW_R * s, fill)
            if index == self.selected:
                _rr(img, lx0, y0 + 6 * s, lx0 + 3 * s, y1 - 6 * s, 1.5 * s, C_ACCENT)

    def _paint_preview_well(self, img: Raster) -> None:
        s = self.s
        x0, y0, x1, y1 = self._preview_rect()
        _rr(img, x0, y0, x1, y1, ROW_R * s, C_WELL)
        _rr(img, x0 + 1, y0 + 1, x1 - 1, y1 - 1, (ROW_R - 1) * s, C_PANEL)

    def _paint_preview_photo(self) -> None:
        if self.selected is None:
            return
        device = self.devices_list[self.selected]
        photo = self.preview_cache.get(device.board)
        if photo is None:
            photo = self._render_face(device.board)
            self.preview_cache[device.board] = photo
        if photo is None:
            return
        x0, y0, x1, y1 = self._preview_rect()
        self.canvas.create_image((x0 + x1) / 2, (y0 + y1) / 2, image=photo,
                                 anchor="center")

    def _render_face(self, board: str) -> tk.PhotoImage | None:
        """The device's own face, small: what you are about to open."""
        from emu.skin import SKINS
        skin = SKINS.get(board)
        if skin is None or skin.face is None:
            return None
        from emu import max_face, tdeck_face
        module = {"max": max_face, "tdeck": tdeck_face}[skin.face]
        _x0, _y0, x1, y1 = self._preview_rect()
        want_h = int((y1 - _y0) - 12 * self.s)
        want_w = int((x1 - _x0) - 12 * self.s)
        face = module.build(1)
        small = _shrink(face.raster, want_w, want_h)
        return tk.PhotoImage(data=small.tk_data())

    def _paint_form(self, img: Raster) -> None:
        s = self.s
        # Wells behind the two real widgets.
        _rr(img, 138 * s, 86 * s, 352 * s, 114 * s, 5 * s, C_WELL)
        _rr(img, 138 * s, 156 * s, 352 * s, 182 * s, 5 * s, C_WELL)

    def _paint_text(self) -> None:
        s = self.s
        put = self.canvas.create_text
        put(24 * s, 26 * s, text="BoardEmul", fill=C_INK,
            font=self.ui_title, anchor="w")
        put(24 * s, 50 * s, text="pick a device, then start it",
            fill=C_INK_DIM, font=self.ui_small, anchor="w")

        if self.state == "list":
            for index, device in enumerate(self.devices_list):
                y0 = 76 * s + index * (ROW_H + ROW_GAP) * s
                put(40 * s, y0 + 16 * s, text=device.name, fill=C_INK,
                    font=self.ui_bold, anchor="w")
                put(40 * s, y0 + 33 * s,
                    text=f"{device.board} · "
                         f"{'card' if device.has_sdcard else 'no card'}",
                    fill=C_INK_DIM, font=self.ui_small, anchor="w")
            if not self.devices_list:
                put(40 * s, 100 * s, text="no devices yet — make one",
                    fill=C_INK_DIM, font=self.ui, anchor="w")
        else:
            put(40 * s, 84 * s, text="new device", fill=C_INK,
                font=self.ui_bold, anchor="w")
            put(40 * s, 106 * s, text="name", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(40 * s, 134 * s, text="board", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(40 * s, 164 * s, text="firmware", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(146 * s, 134 * s, text=self.boards[self.form_board_index],
                fill=C_INK, font=self.ui, anchor="w")
            fw = (self.form_firmware.name if self.form_firmware
                  else "choose an image…")
            shown = fw if len(fw) < 36 else "…" + fw[-35:]
            put(146 * s, 164 * s, text=shown,
                fill=C_INK if self.form_firmware else C_INK_DIM,
                font=self.ui, anchor="w")

        labels = {"start": "Start", "new": "New device…",
                  "watch": "Watch a folder…", "quit": "Quit",
                  "create": "Create and start", "cancel": "Cancel"}
        for name, (x0, y0, x1, y1) in self._buttons().items():
            text = labels.get(name)
            if text:
                put((x0 + x1) / 2, (y0 + y1) / 2, text=text,
                    fill="#ffffff" if name in ("start", "create") else "#c9cdd0",
                    font=self.ui, anchor="center")
        if self.note_text:
            put(self.width - 24 * s, self.height - 26 * s, text=self.note_text,
                fill="#e0b0b0", font=self.ui_small, anchor="e")


def _rr(img: Raster, x0, y0, x1, y1, r, paint) -> None:
    img.rrect(x0, y0, x1, y1, r, paint)


def _shrink(src: Raster, want_w: int, want_h: int) -> Raster:
    """Box-average an image into a smaller one, keeping its aspect."""
    ratio = min(want_w / src.w, want_h / src.h)
    w = max(1, int(src.w * ratio))
    h = max(1, int(src.h * ratio))
    out = Raster(w, h)
    for ty in range(h):
        sy0, sy1 = int(ty / ratio), max(int(ty / ratio) + 1, int((ty + 1) / ratio))
        for tx in range(w):
            sx0, sx1 = int(tx / ratio), max(int(tx / ratio) + 1, int((tx + 1) / ratio))
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
