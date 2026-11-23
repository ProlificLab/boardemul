#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The web front door: a localhost server serving the interface (ADR 0135).

    python -m emu.web [--port 8420] [--dir devices] [--no-open]

The API is the folder format and the run protocol, verbatim: devices map
onto `emu/device.py`, a started run is a child `emu/run.py --device` whose
frames are served as PNGs and whose input goes into the run's own
`input.jsonl`. The keyboard's brains stay here in Python — the page posts
host key events, and the chord logic (ADR 0127's `TDeckTyping`) decides
what they mean on the board.

Bound to 127.0.0.1. This is a localhost tool, not a service, and it serves
only its own page: other pages in the same browser are refused, and a run
takes per-run settings from here, never a program or a path (ADR 0139).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from emu import device as device_folder  # noqa: E402
from emu import home  # noqa: E402
from emu.skin import InputWriter, LETTER_KEYS, KEYSYMS, TDeckTyping  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent / "web"
# The serial ports a run has: the UART, the USB Serial/JTAG port, and the CDC
# port on the USB OTG controller (ADRs 0156, 0166).
SERIAL_PORTS = ("uart", "usb", "otg")
# Where RunHandle puts each run's directory, by device.
LOGS = home.logs()
UPLOADS = ".uploads"
# The most a firmware upload may carry: four times the largest flash these
# boards have (ADR 0139).
UPLOAD_LIMIT = 64 * 1024 * 1024

# The per-run settings the browser may pass, and the ones a device keeps,
# are emu/device.py's RUN_FLAGS (ADRs 0139, 0145).
RUN_FLAGS = device_folder.RUN_FLAGS

# What a request's Host may name, at the server's own port (ADR 0139).
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")

# How long a frame request waits for a redraw before answering "not yet".
# A test turns this down so a quiet long-poll is fast to assert on.
FRAME_POLL_SECONDS = 25.0

# The keyboard's brains, per run: the T-Deck's chord table (ADR 0127) and
# the Max's plainer mapping, both writing through the run's InputWriter.


# The browser's KeyboardEvent.key for keys whose Tk keysym differs; the chord
# logic and KEYSYMS were written against Tk's (ADR 0127).
BROWSER_KEYSYMS = {"Enter": "Return", "Backspace": "BackSpace",
                   "Control": "Control_L", "Alt": "Alt_L", " ": "space"}


class KeyBridge:
    """Host key events in, device key events out, for one run."""

    def __init__(self, board: str, writer: InputWriter):
        self.board = board
        self.writer = writer
        self.typing = TDeckTyping(writer.key) if board == "tdeck" else None

    def key(self, name: str, down: bool) -> None:
        """A drawn cap or a named device key, pressed as it is."""
        self.writer.key(name, down)

    def host(self, keysym: str, char: str, down: bool, code: str = "") -> None:
        """A key on the user's keyboard, as the browser reported it.

        `code` names the physical key (KeyN), the same at its press and its
        release whatever Shift did in between, so a release finds the chord
        its press made; a client that sends none falls back on the key's
        name. The browser's names for Enter and Backspace become the Tk
        keysyms the chord logic was written against (ADR 0127)."""
        keysym = BROWSER_KEYSYMS.get(keysym, keysym)
        if self.typing is not None:
            keycode = hash(code or keysym) & 0xFFFF
            if down:
                self.typing.host_press(keysym, char, keycode)
            else:
                self.typing.host_release(keycode)
            return
        # The Max: a plainer mapping, as the Tk window's _keysym did.
        name = None
        if keysym in KEYSYMS:
            name = KEYSYMS[keysym]
        elif len(char) == 1 and char.upper() in LETTER_KEYS:
            name = char.upper()
        if name is not None:
            self.writer.key(name, down)

    def text(self, text: str) -> None:
        if self.typing is not None:
            # The board's keypad types it, chord by chord, as it reads the
            # run's input file (ADR 0127); TDeckTyping has no text of its own.
            self.writer.text(text)
            return
        for char in text:
            if len(char) == 1 and char.upper() in LETTER_KEYS:
                self.writer.key(char.upper(), True)
                self.writer.key(char.upper(), False)


class ServerState:
    """The runs this server started, and the folders they belong to."""

    def __init__(self, devices: Path):
        self.devices = devices
        self.runs: dict[str, device_folder.RunHandle] = {}
        self.bridges: dict[str, KeyBridge] = {}
        self.writers: dict[str, InputWriter] = {}
        self.lock = threading.Lock()

    def handle(self, name: str) -> device_folder.RunHandle | None:
        with self.lock:
            return self.runs.get(name)

    def start(self, name: str, extra: tuple[str, ...] = ()) -> dict:
        with self.lock:
            existing = self.runs.get(name)
            if existing is not None and existing.alive:
                # Idempotent: starting a running device opens it, however
                # many times the button is pressed.
                return {"id": name, "outdir": str(existing.outdir),
                        "board": existing.device.board}
            device = device_folder.load(device_folder.folder(self.devices, name))
            handle = device_folder.RunHandle(device, extra=extra)
            handle.start()
            self.runs[name] = handle
            self.writers[name] = InputWriter(handle.outdir / "input.jsonl")
            self.bridges[name] = KeyBridge(device.board, self.writers[name])
        return {"id": name, "outdir": str(handle.outdir), "board": device.board}

    def stop(self, name: str) -> None:
        with self.lock:
            handle = self.runs.pop(name, None)
            bridge = self.bridges.pop(name, None)
            writer = self.writers.pop(name, None)
        if handle is not None:
            handle.stop()

    def stop_all(self) -> None:
        with self.lock:
            handles = list(self.runs.values())
            self.runs.clear()
            self.bridges.clear()
            self.writers.clear()
        for handle in handles:
            handle.stop()

    def writer(self, name: str) -> InputWriter | None:
        with self.lock:
            return self.writers.get(name)

    def bridge(self, name: str) -> KeyBridge | None:
        with self.lock:
            return self.bridges.get(name)


class Conflict(Exception):
    pass


class Refused(Exception):
    """A request this server will not serve, and the status that says why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def run_args(args) -> tuple[str, ...]:
    """The browser's run arguments, held to RUN_FLAGS (ADRs 0139, 0145)."""
    try:
        return device_folder.check_settings(args)
    except device_folder.DeviceError as exc:
        raise Refused(400, str(exc)) from None


class NotFound(Exception):
    pass


# A played stream's file, as the I2S controller names it (ADR 0146).
AUDIO_FILE = re.compile(r"i2s[0-9]-[0-9]+\.wav\Z")


def audio_streams(outdir: Path) -> list[dict]:
    """The run's played streams, oldest first, with their length in seconds
    read from each WAV header (ADR 0146)."""
    found = []
    folder = outdir / "audio"
    if not folder.is_dir():
        return found
    for path in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime):
        if not AUDIO_FILE.match(path.name):
            continue
        try:
            with wave.open(str(path), "rb") as wav:
                seconds = wav.getnframes() / float(wav.getframerate() or 1)
                rate, channels = wav.getframerate(), wav.getnchannels()
        except (OSError, EOFError, wave.Error):
            continue
        found.append({"name": path.name, "seconds": round(seconds, 2),
                      "rate": rate, "channels": channels,
                      "bytes": path.stat().st_size})
    return found


def latest_run_dir(name: str, logs: Path | None = None) -> Path | None:
    """The highest-numbered run directory a device has, where RunHandle
    puts them, or None if it never ran."""
    runs = (logs or LOGS) / "devices" / name
    numbered = [p for p in runs.iterdir() if p.is_dir() and p.name.isdigit()] \
        if runs.is_dir() else []
    return max(numbered, key=lambda p: int(p.name), default=None)


def run_logs_zip(device, outdir: Path) -> bytes:
    """The run's directory as a zip, with what it ran on (ADR 0163).

    Regular files only: the sockets and pipes a run keeps beside its logs
    are no use off this machine, and a file that cannot be read now (one
    being replaced) is left out rather than failing the whole download."""
    import io
    import platform
    import zipfile
    from emu.version import VERSION
    from tools import doctor
    where = ("on this host" if device_folder.runs_natively()
             else "in the container " + (os.environ.get("BOARDEMUL_IMAGE")
                                         or device_folder.DEFAULT_IMAGE))
    about = (f"BoardEmul {VERSION}\n"
             f"QEMU sources {doctor.sources_digest()}\n"
             f"host {platform.system()} {platform.release()}, "
             f"Python {platform.python_version()}, runs start {where}\n"
             f"device {device.name}, run {outdir.name}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("boardemul.txt", about)
        manifest = device.root / device_folder.MANIFEST
        if manifest.is_file():
            zf.write(manifest, "device.json")
        for path in sorted(outdir.rglob("*")):
            # A socket made in the container shows on a Windows host as an
            # empty regular file; by its name, it is not a log.
            if path.suffix == ".sock" or not path.is_file():
                continue
            try:
                zf.writestr(path.relative_to(outdir).as_posix(), path.read_bytes())
            except OSError:
                continue
    return buf.getvalue()


def device_json(device) -> dict:
    running = False
    return {
        "name": device.name,
        "board": device.board,
        "firmware": Path(device.firmware).name,
        "mac": device.mac,
        "storage": _storage(device),
        "created": (device.created or "").split("T")[0],
        "overlays": list(device.overlays),
        "settings": list(device.settings),
        "cloned_from": device.cloned_from,
        "running": running,
    }


def _storage(device) -> str:
    parts = []
    try:
        parts.append(f"{device.flash.stat().st_size / (1024 * 1024):.0f} MB flash")
    except OSError:
        pass
    if device.has_sdcard:
        try:
            parts.append(f"{device.sdcard.stat().st_size / (1024 * 1024):.0f} MB card")
        except OSError:
            parts.append("card")
    return " · ".join(parts) or "no storage yet"


_SKIN_CACHE: dict[str, bytes] = {}
_SKIN_GEOMETRY_CACHE: dict[str, dict] = {}


def _face_module(board: str):
    from emu.skin import SKINS
    skin = SKINS.get(board)
    if skin is None or skin.face is None:
        return None
    from emu import max_face, tdeck_face
    return {"max": max_face, "tdeck": tdeck_face}[skin.face]


def _bake_legends(face) -> None:
    """Render the face's Tk text items into its own raster (ADR 0135): a
    browser cannot draw the canvas text the window lays over the image."""
    from emu import font
    from emu.raster import hex_colour
    for text in face.texts:
        font.draw_text(face.raster, text.x, text.y, text.text, text.size,
                       hex_colour(text.colour), anchor=text.anchor,
                       bold=text.bold)


def _skin_png(board: str, scale: int = 1) -> bytes | None:
    """The board's drawn face, as the detail card's portrait - with its
    legends baked in, because a browser cannot draw the Tk text that the
    window lays over the raster (ADR 0126, decision 3)."""
    key = f"{board}@{scale}"
    if key in _SKIN_CACHE:
        return _SKIN_CACHE[key]
    import base64
    module = _face_module(board)
    if module is None:
        return None
    face = module.build(scale)
    _bake_legends(face)
    png = base64.b64decode(face.raster.tk_data())
    _SKIN_CACHE[key] = png
    return png


def _skin_geometry(board: str) -> dict | None:
    """The drawn face's interactive geometry, in scale-1 canvas pixels:
    where the panel sits, where every cap is, where the trackball is
    (ADRs 0126, 0130 — the same data the raster skin was painted from)."""
    if board in _SKIN_GEOMETRY_CACHE:
        return _SKIN_GEOMETRY_CACHE[board]
    from emu.skin import SKINS
    skin = SKINS.get(board)
    module = _face_module(board)
    if skin is None or module is None:
        return None
    face = module.build(1)
    geometry = {
        "board": board,
        "width": face.width,
        "height": face.height,
        "image": f"/api/skin/{board}.png?scale=2",
        "screen": {"x": face.screen[0], "y": face.screen[1],
                   "w": skin.screen_w, "h": skin.screen_h,
                   "idle": skin.idle},
        "keys": [{"name": name, "x0": x0, "y0": y0, "x1": x1, "y1": y1}
                 for x0, y0, x1, y1, name in face.keys],
        "trackball": ({"cx": face.trackball[0], "cy": face.trackball[1],
                       "r": face.trackball[2]} if face.trackball[2] else None),
    }
    _SKIN_GEOMETRY_CACHE[board] = geometry
    return geometry


class Handler(BaseHTTPRequestHandler):
    server: "WebServer"

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):     # quiet unless asked
        pass

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, kind: str, download: str | None = None) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._json({"error": f"no file at {path}"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", kind)
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        # Not a type a page elsewhere can send without asking first, and
        # this server grants no such asking (ADR 0139).
        if self._content_type() != "application/json":
            raise Refused(415, "a body must be application/json")
        return json.loads(self.rfile.read(length))

    def _content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    def _guard(self, changes: bool) -> None:
        """Serve this server's own page and programs, nothing else (ADR 0139).

        Host must name the server, so a rebound name reads nothing. A
        request that changes something and carries an Origin must carry
        this server's: browsers send one on every cross-origin POST and
        DELETE, and a program sends none."""
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.hosts:
            raise Refused(403, f"not this server: Host {host!r}")
        origin = self.headers.get("Origin")
        if changes and origin is not None and \
                origin.lower() not in {f"http://{h}" for h in self.server.hosts}:
            raise Refused(403, f"not this server's page: Origin {origin!r}")

    def _segments(self) -> list[str]:
        return [urllib.parse.unquote(s) for s in self.path.split("?")[0].split("/")
                if s]

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(self.path.split("?")[1]).items()} \
            if "?" in self.path else {}

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        try:
            self._guard(changes=False)
            segs = self._segments()
            if not segs:
                return self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            if segs[0] != "api":
                candidate = WEB_DIR / "/".join(segs)
                if candidate.is_file() and candidate.is_relative_to(WEB_DIR):
                    kind = "text/javascript" if candidate.suffix == ".js" else (
                        "text/css" if candidate.suffix == ".css" else
                        "image/png" if candidate.suffix == ".png" else "text/plain")
                    return self._file(candidate, kind)
                return self._json({"error": "not found"}, 404)

            if segs[1] == "devices" and len(segs) == 2:
                return self._json({"devices": [
                    {**device_json(d),
                     "running": d.name in self.server.state.runs}
                    for d in device_folder.all_devices(self.server.state.devices)]})

            if segs[1] == "firmware" and len(segs) == 2:
                # The pinned images a device can be made from (the library).
                from tools import fetch_firmware
                return self._json({"images": [
                    {"name": e["name"], "board": e["board"], "role": e.get("role", ""),
                     "file": Path(e["path"]).name,
                     "present": fetch_firmware.state(e) == "ok"}
                    for e in fetch_firmware.load() if "board" in e]})
            if segs[1] == "version" and len(segs) == 2:
                from emu.version import VERSION
                return self._json({"version": VERSION})
            if segs[1] == "doctor" and len(segs) == 2:
                # What this machine lacks, by the doctor's own checks (ADR 0153).
                from tools import doctor
                return self._json({"findings": [f.__dict__ for f in doctor.diagnose()]})
            if segs[1] == "devices" and len(segs) == 4 and segs[3] == "snapshots":
                device = self._device(segs[2])
                return self._json({"snapshots": [
                    s.__dict__ for s in device_folder.snapshots(device)]})
            if segs[1] == "devices" and len(segs) == 4 and segs[3] == "card":
                device = self._device(segs[2])
                if not device.has_sdcard:
                    return self._json({"error": f"{device.name} has no card"}, 404)
                return self._file(device.sdcard, "application/octet-stream",
                                  download=f"{device.name}-card.img")
            if segs[1] == "devices" and len(segs) == 4 and segs[3] == "logs.zip":
                # The latest run, as one file for a report (ADR 0163).
                device = self._device(segs[2])
                handle = self.server.state.handle(device.name)
                outdir = handle.outdir if handle is not None else latest_run_dir(device.name)
                if outdir is None:
                    return self._json({"error": f"{device.name} has not run yet"}, 404)
                body = run_logs_zip(device, outdir)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename='
                                 f'"{device.name}-run-{outdir.name}.zip"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if segs[1] == "skin" and len(segs) == 3:
                board = segs[2].removesuffix(".png")
                scale = int(self._query().get("scale", 1))
                png = _skin_png(board, scale)
                if png is None:
                    return self._json({"error": "no such board"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                # no-store: the face is re-rendered by this same process, and
                # a cached copy once cost a day of blank-key debugging.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(png)
                return

            if segs[1] == "skin" and len(segs) == 4 and segs[3] == "geometry":
                geometry = _skin_geometry(segs[2])
                if geometry is None:
                    return self._json({"error": "no such board"}, 404)
                return self._json(geometry)

            if segs[1] == "runs" and len(segs) == 2:
                with self.server.state.lock:
                    return self._json({"runs": [
                        {"id": name, "outdir": str(h.outdir),
                         "alive": h.alive}
                        for name, h in self.server.state.runs.items()]})

            if segs[1] == "runs" and len(segs) >= 4:
                name = segs[2]
                handle = self.server.state.handle(name)
                if handle is None:
                    return self._json({"error": f"no run {name!r}"}, 404)
                if segs[3] == "frame":
                    return self._frame(handle)
                if segs[3] == "frames" and len(segs) == 5:
                    name = Path(segs[4]).name          # no climbing out
                    return self._file(handle.outdir / "frames" / name, "image/png")
                if segs[3] == "frames":
                    return self._frame_list(handle)
                if segs[3] == "uart":
                    query = self._query()
                    return self._uart(handle, int(query.get("after", 0)),
                                      query.get("port", "uart"))
                if segs[3] == "console":
                    return self._json({"alive": handle.alive,
                                       "lines": handle.console_tail()})
                if segs[3] == "audio" and len(segs) == 5:
                    # One stream's WAV file (ADR 0146), by its own name only.
                    name = Path(segs[4]).name
                    if not AUDIO_FILE.match(name):
                        return self._json({"error": "not a stream's file"}, 404)
                    return self._file(handle.outdir / "audio" / name, "audio/wav")
                if segs[3] == "audio":
                    return self._json({"streams": audio_streams(handle.outdir)})
                if segs[3] == "state":
                    # Beside the frames, where the board writes it (ADR 0129).
                    path = handle.outdir / "frames" / "board.json"
                    try:
                        return self._json(json.loads(path.read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        return self._json({})
            return self._json({"error": "not found"}, 404)
        except Refused as exc:
            return self._json({"error": str(exc)}, exc.status)
        except (NotFound, device_folder.DeviceError) as exc:
            return self._json({"error": str(exc)}, 404)
        except Conflict as exc:
            return self._json({"error": str(exc)}, 409)
        except Exception as exc:                     # never kill the server
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        try:
            self._guard(changes=True)
            segs = self._segments()
            if segs[:2] == ["api", "firmware"]:
                return self._upload()
            if segs[:2] == ["api", "devices"]:
                if len(segs) == 2:
                    return self._create()
                name = segs[2]
                if len(segs) == 4 and segs[3] == "start":
                    extra = run_args(self._body().get("args", []))
                    result = self.server.state.start(name, extra=extra)
                    return self._json(result)
                if len(segs) == 4 and segs[3] == "stop":
                    self.server.state.stop(name)
                    return self._json({"stopped": name})
                if len(segs) == 4 and segs[3] == "remove":
                    self._remove(name)
                    return self._json({"removed": name})
                if len(segs) == 4 and segs[3] == "settings":
                    return self._settings(name)
                if len(segs) == 4 and segs[3] == "snapshots":
                    device = self._stopped(name)
                    label = self._body().get("label") or None
                    return self._json(device_folder.snapshot(device, label).__dict__, 201)
                if len(segs) == 6 and segs[3] == "snapshots" and segs[5] == "restore":
                    device = device_folder.restore(self._stopped(name), segs[4])
                    return self._json(device_json(device))
                if len(segs) == 4 and segs[3] == "card":
                    return self._card(name)
                if len(segs) == 4 and segs[3] == "clone":
                    body = self._body()
                    try:
                        made = device_folder.clone(self._stopped(name),
                                                   str(body.get("name", "")),
                                                   body.get("mac") or None)
                    except device_folder.DeviceBusy:
                        raise
                    except device_folder.DeviceError as exc:
                        raise Refused(400, str(exc)) from None
                    return self._json(device_json(made), 201)
            if segs[:2] == ["api", "runs"] and len(segs) == 4 and segs[3] == "input":
                return self._input(segs[2])
            return self._json({"error": "not found"}, 404)
        except Refused as exc:
            return self._json({"error": str(exc)}, exc.status)
        except device_folder.DeviceBusy as exc:
            return self._json({"error": str(exc)}, 409)
        except (NotFound, device_folder.DeviceError) as exc:
            return self._json({"error": str(exc)}, 404)
        except Conflict as exc:
            return self._json({"error": str(exc)}, 409)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_DELETE(self):
        try:
            self._guard(changes=True)
            segs = self._segments()
            if segs[:2] == ["api", "devices"] and len(segs) == 3:
                self._remove(segs[2])
                return self._json({"removed": segs[2]})
            if segs[:2] == ["api", "devices"] and len(segs) == 5 and segs[3] == "snapshots":
                device_folder.forget(self._device(segs[2]), segs[4])
                return self._json({"forgot": segs[4]})
            return self._json({"error": "not found"}, 404)
        except Refused as exc:
            return self._json({"error": str(exc)}, exc.status)
        except device_folder.DeviceBusy as exc:
            return self._json({"error": str(exc)}, 409)
        except device_folder.DeviceError as exc:
            return self._json({"error": str(exc)}, 404)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    # -- the routes' bodies ---------------------------------------------------

    def _create(self) -> None:
        body = self._body()
        firmware = body.get("firmware", "")
        if body.get("image"):
            # A pinned image, by its manifest name: the page names it, the
            # server knows where it is (ADR 0139).
            from tools import fetch_firmware
            entry = next((e for e in fetch_firmware.load()
                          if e["name"] == body["image"] and "board" in e), None)
            if entry is None or fetch_firmware.state(entry) != "ok":
                return self._json({"error": f"no fetched image {body['image']!r}"}, 400)
            firmware = str(fetch_firmware.FIRMWARE / entry["path"])
            body.setdefault("board", entry["board"])
        if not firmware or not Path(firmware).is_file():
            return self._json({"error": f"no firmware at {firmware!r}"}, 400)
        if device_folder.folder(self.server.state.devices,
                                body.get("name", "")).exists():
            raise Conflict(f"{body.get('name')!r} already exists")
        device = device_folder.create(
            self.server.state.devices, body.get("name", ""),
            Path(firmware), board=body.get("board", "tdeck-max"),
            psram_octal=bool(body.get("psram_octal", False)),
            overlays=tuple(body.get("overlays", ())))
        # It restarts after a panic, as the board does (ADR 0145, decision 6).
        device = device_folder.set_settings(device, device_folder.OPERATOR_SETTINGS)
        self._json(device_json(device), 201)

    def _device(self, name: str):
        return device_folder.load(device_folder.folder(self.server.state.devices, name))

    def _stopped(self, name: str):
        """The device, if no run of this server holds it; the folder's own
        marker covers runs started elsewhere (ADR 0149)."""
        handle = self.server.state.handle(name)
        if handle is not None and handle.alive:
            raise device_folder.DeviceBusy(f"{name} is running; stop it first")
        return self._device(name)

    def _card(self, name: str) -> None:
        """Replace the device's card with an uploaded image of its size; the
        card it had becomes a snapshot first (ADR 0149)."""
        if self._content_type() != "application/octet-stream":
            raise Refused(415, "a card image must be application/octet-stream")
        length = int(self.headers.get("Content-Length") or 0)
        if not length or length > UPLOAD_LIMIT:
            raise Refused(400, f"a card image is 1 to {UPLOAD_LIMIT} bytes")
        device = self._stopped(name)
        image = self.rfile.read(length)
        try:
            taken = device_folder.replace_card(device, image)
        except device_folder.DeviceBusy:
            raise
        except device_folder.DeviceError as exc:
            raise Refused(400, str(exc)) from None
        self._json({"card": "replaced", "snapshot": taken.label})

    def _settings(self, name: str) -> None:
        """Keep a device's run settings (ADR 0145): held to the same list a
        start takes, each air a name, applied from the next start on."""
        settings = run_args(self._body().get("settings", []))
        device = device_folder.load(device_folder.folder(
            self.server.state.devices, name))
        self._json(device_json(device_folder.set_settings(device, settings)))

    def _remove(self, name: str) -> None:
        self.server.state.stop(name)
        device = device_folder.load(device_folder.folder(
            self.server.state.devices, name))
        device_folder.remove(device)

    def _upload(self) -> None:
        name = self._query().get("name", "firmware.bin")
        name = Path(name).name or "firmware.bin"
        if self._content_type() != "application/octet-stream":
            raise Refused(415, "a firmware upload must be application/octet-stream")
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return self._json({"error": "empty upload"}, 400)
        if length > UPLOAD_LIMIT:
            raise Refused(413, f"an upload is at most {UPLOAD_LIMIT >> 20} MiB")
        uploads = self.server.state.devices / UPLOADS
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / f"{int(time.time())}-{name}"
        remaining = length
        with path.open("wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        self._json({"path": str(path), "bytes": path.stat().st_size})

    def _input(self, name: str) -> None:
        writer = self.server.state.writer(name)
        bridge = self.server.state.bridge(name)
        if writer is None or bridge is None:
            return self._json({"error": f"no run {name!r}"}, 404)
        for event in self._body().get("events", []):
            kind = event.get("type")
            if kind == "touch":
                if event.get("down", True):
                    writer.touch(int(event["x"]), int(event["y"]))
                else:
                    writer.release()
            elif kind == "key":
                writer.key(str(event["key"]), bool(event.get("down", True)))
            elif kind == "hostkey":
                bridge.host(str(event.get("keysym", "")), str(event.get("char", "")),
                            bool(event.get("down", True)),
                            str(event.get("code", "")))
            elif kind == "text":
                bridge.text(str(event.get("text", "")))
            elif kind == "trackball":
                if "roll" in event:
                    writer.roll(str(event["roll"]), int(event.get("steps", 1)))
                else:
                    writer.ball(bool(event.get("press", True)))
            elif kind == "serial":
                port = str(event.get("port", "uart"))
                if port not in SERIAL_PORTS:
                    raise Refused(400, "port is uart, usb or otg")
                writer.serial(port, str(event.get("text", "")))
        self._json({"accepted": True})

    def _frame(self, handle: device_folder.RunHandle) -> None:
        """Long-poll: return the panel's PNG as soon as it is newer than
        `since` (nanoseconds), or 204 after a quiet quarter-minute."""
        latest = handle.outdir / "frames" / "latest.png"
        since = int(self._query().get("since", 0))
        deadline = time.monotonic() + FRAME_POLL_SECONDS
        while time.monotonic() < deadline:
            try:
                mtime = latest.stat().st_mtime_ns
            except OSError:
                mtime = 0
            if mtime and mtime > since:
                body = latest.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Frame-Mtime", str(mtime))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            time.sleep(0.15)
        self.send_response(204)
        self.end_headers()

    def _frame_list(self, handle: device_folder.RunHandle) -> None:
        frames = sorted((handle.outdir / "frames").glob("frame-*.png"))
        return self._json({"frames": [
            {"name": f.name, "mtime": f.stat().st_mtime_ns} for f in frames]})

    def _uart(self, handle: device_folder.RunHandle, after: int,
              port: str = "uart") -> None:
        """One serial port's log: the UART's, the USB Serial/JTAG port's
        (ADR 0156), or the USB OTG port's (ADR 0166)."""
        if port not in SERIAL_PORTS:
            raise Refused(400, "port is uart, usb or otg")
        path = handle.outdir / f"{handle.device.name}.{port}.log"
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return self._json({"lines": [], "total": 0})
        lines = text.splitlines()
        return self._json({"lines": lines[after:], "total": len(lines)})


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    # One server per port. On Windows SO_REUSEADDR lets a second socket bind a
    # port another one is listening on, and a second start of BoardEmul split
    # the page's requests between two servers of different ages. There the
    # port is taken exclusively, so a second start fails and says so; on
    # Linux and macOS SO_REUSEADDR only lets a restart reuse a port in
    # TIME_WAIT, which is what it is for.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, state: ServerState, names: tuple[str, ...] = ()):
        super().__init__(address, Handler)
        self.state = state
        port = self.server_address[1]
        # What a request's Host may say, and so its Origin (ADR 0139): the
        # loopback names at this port, and any name the server was told it
        # is reached by, as `host` or `host:port`.
        self.hosts = {f"{name}:{port}" for name in LOOPBACK}
        for name in names:
            name = name.lower()
            self.hosts.add(name if name.rsplit(":", 1)[-1].isdigit() else f"{name}:{port}")


def make_server(devices: Path, port: int = 0,
                names: tuple[str, ...] = ()) -> WebServer:
    """The server, bound and ready — `port 0` picks a free one, which is
    what tests use."""
    state = ServerState(devices)
    return WebServer(("127.0.0.1", port), state, names)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--dir", type=Path, default=home.devices(),
                    help="where device folders live")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open a browser tab")
    args = ap.parse_args(argv)

    try:
        server = make_server(args.dir, args.port)
    except OSError as exc:
        print(f"port {args.port} is in use ({exc.strerror or exc}): is BoardEmul "
              f"already running? Its page would be http://127.0.0.1:{args.port}/; "
              "or start this one with --port", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    from emu.version import VERSION
    print(f"BoardEmul {VERSION} web interface: {url}")
    print("Ctrl-C stops the server and every run it started.")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.state.stop_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
