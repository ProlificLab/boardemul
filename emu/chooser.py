#!/usr/bin/env python3
"""The window's first screen: pick a device, start it (ADR 0134).

Drawn the way the device faces are drawn (ADR 0126), and with the same
tricks: a panel lit from the top, cards with a lit top edge the way keycaps
have one, wells that read as holes because their bottom edge catches light,
and buttons with depth that press on release, not on touch. The whole screen
is one anti-aliased image, repainted on every state change; the system's
font carries everything readable over it.

The selected device's own face is rendered in a well beside the list, named
under it, so what you are about to open looks like what it is.

Keyboard: up and down choose, Enter starts, Escape leaves. Double-click
starts. The list scrolls with the wheel when devices outnumber it. A run
started here is the window's to stop; this module only decides.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

from emu.raster import Raster, hex_colour, mix

# The palette is the faces': the desk, the panel on it, and what light does.
C_DESK = hex_colour("#0f1113")
C_PANEL_TOP = hex_colour("#15171a")
C_PANEL_BOTTOM = hex_colour("#101215")
C_RIM = hex_colour("#2c2e31")
C_WELL = hex_colour("#0c0e10")
C_WELL_LIP = hex_colour("#343940")

C_ROW_TOP = hex_colour("#232830")
C_ROW_BOTTOM = hex_colour("#191c21")
C_ROW_TOP_HOVER = hex_colour("#2a3038")
C_ROW_BOTTOM_HOVER = hex_colour("#1e232a")
C_ROW_TOP_SELECTED = hex_colour("#313a46")
C_ROW_BOTTOM_SELECTED = hex_colour("#242b34")
C_ACCENT_TOP = hex_colour("#6ba3e8")
C_ACCENT_BOTTOM = hex_colour("#3f6ea8")

C_BUTTON_TOP = hex_colour("#2b3037")
C_BUTTON_BOTTOM = hex_colour("#21252b")
C_BUTTON_TOP_PRESS = hex_colour("#1e2126")
C_BUTTON_BOTTOM_PRESS = hex_colour("#191c21")
C_PRIMARY_TOP = hex_colour("#3f6ea8")
C_PRIMARY_BOTTOM = hex_colour("#2a4a78")
C_PRIMARY_TOP_PRESS = hex_colour("#335a8a")
C_PRIMARY_BOTTOM_PRESS = hex_colour("#223d63")
C_BUTTON_EDGE = hex_colour("#3a4048")
C_WHITE = hex_colour("#ffffff")

C_INK = "#e8eaec"
C_INK_DIM = "#8f979e"
C_INK_FAINT = "#5c646d"

# Layout, in scale units: a header, a scrolling list of cards on the left,
# the selected device's face in a well on the right, and a footer of buttons.
W, H = 672, 556
HEADER_Y = 44
SUBTITLE_Y = 74
RULE_Y = 96
LIST_X0, LIST_X1 = 26, 424
LIST_Y0, LIST_Y1 = 110, 460
PREVIEW_X0 = 442
PREVIEW_PAD = 26
ROW_H, ROW_GAP, ROW_R = 60, 10, 9
FOOT_Y0, FOOT_Y1 = 478, 524


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
        self.scroll = 0
        self.hover: str | None = None
        self.pressed: str | None = None
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
        self.top.bind("<MouseWheel>", self._wheel)
        self.top.bind("<Button-4>", lambda _e: self._wheel_step(-1))
        self.top.bind("<Button-5>", lambda _e: self._wheel_step(1))

        self.ui = tkfont.Font(family="Segoe UI", size=-10 * scale)
        self.ui_small = tkfont.Font(family="Segoe UI", size=-9 * scale)
        self.ui_bold = tkfont.Font(family="Segoe UI", size=-13 * scale,
                                   weight="bold")
        self.ui_title = tkfont.Font(family="Segoe UI", size=-24 * scale,
                                    weight="bold")

        self.canvas = tk.Canvas(self.top, highlightthickness=0,
                                background="#0f1113")
        self.canvas.pack()
        self.widgets: list[tk.Widget] = []
        self.faces: dict[str, Raster] = {}

        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
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

    def _preview_rect(self) -> tuple[float, float, float, float]:
        return (self._X(PREVIEW_X0), self._X(LIST_Y0),
                self._X(W - PREVIEW_PAD), self._X(LIST_Y1))

    def _visible_rows(self) -> int:
        _lx0, ly0, _lx1, ly1 = self._list_rect()
        return max(1, int((ly1 - ly0) / ((ROW_H + ROW_GAP) * self.s)))

    def _buttons(self) -> dict[str, tuple[float, float, float, float]]:
        s = self.s
        buttons = {
            "start": (self._X(26), self._X(FOOT_Y0), self._X(190), self._X(FOOT_Y1)),
            "new": (self._X(202), self._X(FOOT_Y0), self._X(370), self._X(FOOT_Y1)),
            "watch": (self._X(382), self._X(FOOT_Y0), self._X(550), self._X(FOOT_Y1)),
            "quit": (self._X(W - 136), self._X(FOOT_Y0),
                     self._X(W - 26), self._X(FOOT_Y1)),
            "board": (self._X(150), self._X(206), self._X(400), self._X(240)),
            "firmware": (self._X(150), self._X(248), self._X(400), self._X(282)),
            "create": (self._X(26), self._X(FOOT_Y0), self._X(230), self._X(FOOT_Y1)),
            "cancel": (self._X(242), self._X(FOOT_Y0), self._X(370), self._X(FOOT_Y1)),
        }
        if self.state == "list":
            for gone in ("board", "firmware", "create", "cancel"):
                buttons.pop(gone)
        else:
            for gone in ("start", "new", "watch", "quit"):
                buttons.pop(gone)
        return buttons

    # -- data ---------------------------------------------------------------

    def _reload(self) -> None:
        from emu import device as device_folder
        self.devices_list = device_folder.all_devices(self.devices)
        if self.selected is None or self.selected >= len(self.devices_list):
            self.selected = 0 if self.devices_list else None
        self.scroll = max(0, min(self.scroll,
                                 max(0, len(self.devices_list) - self._visible_rows())))

    def rows(self) -> list[str]:
        """What each row says, for tests and for the blind."""
        return [f"{d.name} — {d.board}, {'card' if d.has_sdcard else 'no card'}"
                for d in self.devices_list]

    def select(self, index: int) -> None:
        if self.devices_list and 0 <= index < len(self.devices_list):
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
        if not self.devices_list:
            return
        at = (self.selected or 0) + step
        self.select(max(0, min(len(self.devices_list) - 1, at)))

    def _wheel(self, event) -> None:
        self._wheel_step(-1 if event.delta > 0 else 1)

    def _wheel_step(self, rows: int) -> None:
        if self.state != "list":
            return
        ceiling = max(0, len(self.devices_list) - self._visible_rows())
        if self.scroll != max(0, min(ceiling, self.scroll + rows)):
            self.scroll = max(0, min(ceiling, self.scroll + rows))
            self._render()

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

    def _row_at(self, y: float) -> int | None:
        _lx0, ly0, _lx1, ly1 = self._list_rect()
        if not ly0 <= y <= ly1:
            return None
        at = self.scroll + int((y - ly0) // ((ROW_H + ROW_GAP) * self.s))
        if 0 <= at < len(self.devices_list):
            return at
        return None

    def _press(self, event) -> None:
        hit = self._region_at(event.x, event.y)
        if hit:
            self.pressed = hit
            self._render()
            return
        at = self._row_at(event.y)
        if at is not None:
            self.select(at)

    def _release(self, event) -> None:
        pressed, self.pressed = self.pressed, None
        action = {"start": self._start, "new": self._form, "watch": self._watch,
                  "quit": self._quit, "create": self._create,
                  "cancel": self._cancel, "board": self._cycle_board,
                  "firmware": self._choose_firmware}.get(pressed)
        if action is not None and self._region_at(event.x, event.y) == pressed:
            action()
            return
        self._render()

    def _motion(self, event) -> None:
        hover = self._region_at(event.x, event.y)
        if hover is None and self.state == "list":
            at = self._row_at(event.y)
            if at is not None:
                hover = f"row{at}"
        self.canvas.configure(cursor="hand2" if hover else "")
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
        entry = tk.Entry(self.top, textvariable=self.form_name_var, width=28,
                         background="#0c0e10", foreground="#e8eaec",
                         insertbackground="#e8eaec", relief="flat")
        entry.place(x=int(150 * self.s), y=int(172 * self.s))
        self.widgets.append(entry)
        entry.focus_set()

    def _cancel(self) -> None:
        self.state = "list"
        self._clear_widgets()
        self.note_text = ""
        self._reload()
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
        # The panel is lit from the top, the way every face here is.
        img.rrect(0, 0, self.width, self.height, 12 * s,
                  lambda y: mix(C_PANEL_BOTTOM, C_PANEL_TOP,
                                1.0 - min(1.0, y / (H * s * 0.5))))
        img.rrect(0, 0, 2 * s, self.height, 1 * s, C_RIM)

        if self.state == "list":
            self._paint_list(img)
            self._paint_preview(img)
        else:
            self._paint_form(img)

        for name, (x0, y0, x1, y1) in self._buttons().items():
            pressed = self.pressed == name
            primary = name in ("start", "create")
            if pressed:
                top = C_PRIMARY_TOP_PRESS if primary else C_BUTTON_TOP_PRESS
                bottom = C_PRIMARY_BOTTOM_PRESS if primary else C_BUTTON_BOTTOM_PRESS
            elif primary:
                top, bottom = C_PRIMARY_TOP, C_PRIMARY_BOTTOM
            else:
                top, bottom = C_BUTTON_TOP, C_BUTTON_BOTTOM
            _rr(img, x0, y0, x1, y1, 9 * s, C_RIM)
            _rr(img, x0 + 1, y0 + 1, x1 - 1, y1 - 1, 8 * s,
                lambda y, t=top, b=bottom, y0=y0, y1=y1:
                    mix(t, b, (y - y0) / (y1 - y0)))
            # The lit top edge that makes it a raised thing.
            img.rrect(x0 + 8, y0 + 1.5, x1 - 8, y0 + 2.5, 0.5,
                      mix(top, C_WHITE, 0.22))

        self.canvas.delete("all")
        self.photo = tk.PhotoImage(data=img.tk_data())
        self.canvas.configure(width=self.width, height=self.height)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.image = self.photo
        self._paint_text()

    def _paint_list(self, img: Raster) -> None:
        """The row cards, painted into their own image and clipped to the
        list, so scrolling crops them."""
        s = self.s
        lx0, ly0, lx1, ly1 = self._list_rect()
        w, h = int(lx1 - lx0), int(ly1 - ly0)
        strip = Raster(w, h, C_PANEL_BOTTOM)
        visible = self._visible_rows() + 1
        for at in range(self.scroll, min(len(self.devices_list), self.scroll + visible)):
            index = at - self.scroll
            y0 = index * (ROW_H + ROW_GAP) * s
            y1 = y0 + ROW_H * s
            selected = at == self.selected
            hovered = f"row{at}" == self.hover
            if selected:
                top, bottom = C_ROW_TOP_SELECTED, C_ROW_BOTTOM_SELECTED
            elif hovered:
                top, bottom = C_ROW_TOP_HOVER, C_ROW_BOTTOM_HOVER
            else:
                top, bottom = C_ROW_TOP, C_ROW_BOTTOM
            _rr(strip, 2, y0, 2 + w - 4, y1, ROW_R * s, C_RIM)
            _rr(strip, 3, y0 + 1, w - 2, y1 - 1, (ROW_R - 1) * s,
                lambda y, t=top, b=bottom, y0=y0, y1=y1: mix(t, b, (y - y0) / (y1 - y0)))
            strip.rrect(12, y0 + 1.5, w - 12, y0 + 2.5, 0.5,
                        mix(top, C_WHITE, 0.16))
            if selected:
                _rr(strip, 3, y0 + 10 * s, 8, y1 - 10 * s, 2.5 * s,
                    lambda y, y0=y0, y1=y1: mix(C_ACCENT_TOP, C_ACCENT_BOTTOM,
                                                (y - y0) / (y1 - y0)))
        _blit(img, strip, int(lx0), int(ly0))
        if self.scroll + self._visible_rows() < len(self.devices_list):
            img.rrect(lx1 - 16 * s, ly1 - 5 * s, lx1 - 6 * s, ly1 - 3 * s, 1,
                      C_BUTTON_EDGE)

    def _paint_preview(self, img: Raster) -> None:
        """A well with the selected device's own face in it."""
        s = self.s
        px0, py0, px1, py1 = self._preview_rect()
        _rr(img, px0, py0, px1, py1, 10 * s, C_RIM)
        _rr(img, px0 + 1, py0 + 1, px1 - 1, py1 - 1, 9 * s, C_WELL)
        # A hole reads as a hole because its bottom edge catches light.
        img.rrect(px0 + 10, py1 - 3.5, px1 - 10, py1 - 2.5, 0.5, C_WELL_LIP)

        device = (self.devices_list[self.selected]
                  if self.selected is not None and self.devices_list else None)
        if device is None:
            return
        face = self.faces.get(device.board)
        if face is None:
            face = self._render_face(device.board)
            self.faces[device.board] = face
        if face is not None:
            _blit_keyed(img, face, int((px0 + px1) / 2 - face.w / 2),
                        int(py0 + 16 * s), C_DESK)

    def _render_face(self, board: str) -> Raster | None:
        from emu.skin import SKINS
        skin = SKINS.get(board)
        if skin is None or skin.face is None:
            return None
        from emu import max_face, tdeck_face
        module = {"max": max_face, "tdeck": tdeck_face}[skin.face]
        face = module.build(1)
        _px0, _py0, px1, py1 = self._preview_rect()
        want_w = int((px1 - _px0) - 40 * self.s)
        want_h = int((py1 - _py0) - 84 * self.s)
        return _shrink(face.raster, want_w, want_h)

    def _paint_form(self, img: Raster) -> None:
        s = self.s
        # The well behind the name's entry widget.
        _rr(img, self._X(146), self._X(166), self._X(400), self._X(196),
            6 * s, C_WELL)

    def _paint_text(self) -> None:
        s = self.s
        put = self.canvas.create_text
        put(28 * s, self._X(HEADER_Y), text="BoardEmul", fill=C_INK,
            font=self.ui_title, anchor="w")
        put(self.width - 28 * s, self._X(HEADER_Y),
            text=f"{len(self.devices_list)} device"
                 f"{'s' if len(self.devices_list) != 1 else ''}",
            fill=C_INK_FAINT, font=self.ui, anchor="e")
        put(28 * s, self._X(SUBTITLE_Y),
            text="pick a device, then start it — storage is kept between runs",
            fill=C_INK_DIM, font=self.ui_small, anchor="w")
        self.canvas.create_line(self._X(26), self._X(RULE_Y),
                                self.width - self._X(26), self._X(RULE_Y),
                                fill="#23262b")
        self.canvas.create_line(self._X(26), self._X(RULE_Y),
                                self._X(84), self._X(RULE_Y), fill="#4a6a94",
                                width=max(1, round(1.4 * self.s)))

        if self.state == "list":
            lx0, ly0, lx1, _ly1 = self._list_rect()
            for at in range(self.scroll,
                            min(len(self.devices_list),
                                self.scroll + self._visible_rows())):
                device = self.devices_list[at]
                y0 = ly0 + (at - self.scroll) * (ROW_H + ROW_GAP) * s
                selected = at == self.selected
                put(lx0 + self._X(24), y0 + self._X(19), text=device.name,
                    fill=C_INK, font=self.ui_bold, anchor="w")
                put(lx0 + self._X(24), y0 + self._X(40),
                    text=f"{device.board} · "
                         f"{'card' if device.has_sdcard else 'no card'}"
                         f" · {Path(device.firmware).name}",
                    fill=C_INK_DIM, font=self.ui_small, anchor="w")
                if selected:
                    put(lx1 - self._X(22), y0 + self._X(ROW_H / 2), text="start ↵",
                        fill="#9ec1ee", font=self.ui_small, anchor="e")
            if not self.devices_list:
                cx = (lx0 + lx1) / 2
                put(cx, ly0 + self._X(120), text="no devices yet",
                    fill=C_INK_DIM, font=self.ui_bold, anchor="center")
                put(cx, ly0 + self._X(148),
                    text="make one — it keeps its storage between runs",
                    fill=C_INK_FAINT, font=self.ui_small, anchor="center")

            # The well's caption, under the face.
            device = (self.devices_list[self.selected]
                      if self.selected is not None and self.devices_list else None)
            if device is not None:
                px0, _py0, px1, py1 = self._preview_rect()
                put((px0 + px1) / 2, py1 - self._X(52), text=device.name,
                    fill=C_INK, font=self.ui_bold, anchor="center")
                put((px0 + px1) / 2, py1 - self._X(32),
                    text=f"{device.board} · {Path(device.firmware).name}",
                    fill=C_INK_DIM, font=self.ui_small, anchor="center")
        else:
            put(self._X(40), self._X(140), text="new device", fill=C_INK,
                font=self.ui_bold, anchor="w")
            put(self._X(40), self._X(186), text="name", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(self._X(40), self._X(222), text="board", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(self._X(40), self._X(264), text="firmware", fill=C_INK_DIM,
                font=self.ui_small, anchor="w")
            put(self._X(158), self._X(223), text=self.boards[self.form_board_index],
                fill=C_INK, font=self.ui, anchor="w")
            fw = (self.form_firmware.name if self.form_firmware
                  else "choose an image…")
            shown = fw if len(fw) < 44 else "…" + fw[-43:]
            put(self._X(158), self._X(265), text=shown,
                fill=C_INK if self.form_firmware else C_INK_DIM,
                font=self.ui, anchor="w")

        empty = not self.devices_list and self.state == "list"
        labels = {"start": "Start", "new": ("Create your first device" if empty
                                            else "New device…"),
                  "watch": ("Watch a folder…" if not empty else ""),
                  "quit": "Quit", "create": "Create and start", "cancel": "Cancel"}
        for name, (x0, y0, x1, y1) in self._buttons().items():
            text = labels.get(name)
            if not text:
                continue
            pressed = self.pressed == name
            put((x0 + x1) / 2, (y0 + y1) / 2 + (1 if pressed else 0),
                text=text,
                fill="#0d0f11" if name in ("start", "create") else (
                    C_INK_FAINT if name == "start" and empty else "#e8eaec"),
                font=self.ui_bold if name in ("start", "create", "new")
                else self.ui, anchor="center")
        if self.note_text:
            put(self._X(28), self.height - self._X(18), text=self.note_text,
                fill="#e0b0b0", font=self.ui_small, anchor="w")


def _rr(img: Raster, x0, y0, x1, y1, r, paint) -> None:
    img.rrect(x0, y0, x1, y1, r, paint)


def _blit(dst: Raster, src: Raster, x: int, y: int) -> None:
    """Copy src over dst, scanline by scanline, clipped to both."""
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
    """Copy src over dst; pixels equal to key — the face's own desk — stay
    dst's, so the face sits on the well rather than in a rectangle of it."""
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
    """Box-average an image into a smaller one, keeping its aspect."""
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
