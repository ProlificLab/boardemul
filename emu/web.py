#!/usr/bin/env python3
"""The web front door: a localhost server serving the interface (ADR 0135).

    python -m emu.web [--port 8420] [--dir devices] [--no-open]

The API is the folder format and the run protocol, verbatim: devices map
onto `emu/device.py`, a started run is a child `emu/run.py --device` whose
frames are served as PNGs and whose input goes into the run's own
`input.jsonl`. The keyboard's brains stay here in Python — the page posts
host key events, and the chord logic (ADR 0127's `TDeckTyping`) decides
what they mean on the board.

Bound to 127.0.0.1. This is a localhost tool, not a service.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from emu import device as device_folder  # noqa: E402
from emu.skin import InputWriter, LETTER_KEYS, KEYSYMS, TDeckTyping  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent / "web"
UPLOADS = ".uploads"

# How long a frame request waits for a redraw before answering "not yet".
# A test turns this down so a quiet long-poll is fast to assert on.
FRAME_POLL_SECONDS = 25.0

# The keyboard's brains, per run: the T-Deck's chord table (ADR 0127) and
# the Max's plainer mapping, both writing through the run's InputWriter.


class KeyBridge:
    """Host key events in, device key events out, for one run."""

    def __init__(self, board: str, writer: InputWriter):
        self.board = board
        self.writer = writer
        self.typing = TDeckTyping(writer.key) if board == "tdeck" else None

    def key(self, name: str, down: bool) -> None:
        """A drawn cap or a named device key, pressed as it is."""
        self.writer.key(name, down)

    def host(self, keysym: str, char: str, down: bool) -> None:
        """A key on the user's keyboard, as the browser reported it."""
        if self.typing is not None:
            (self.typing.host_press if down else self.typing.host_release)(
                keysym, char, hash((keysym, char)) & 0xFFFF)
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
            self.typing.type_text(text)
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

    def start(self, name: str) -> dict:
        with self.lock:
            existing = self.runs.get(name)
            if existing is not None and existing.alive:
                # Idempotent: starting a running device opens it, however
                # many times the button is pressed.
                return {"id": name, "outdir": str(existing.outdir),
                        "board": existing.device.board}
            device = device_folder.load(device_folder.folder(self.devices, name))
            handle = device_folder.RunHandle(device)
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


class NotFound(Exception):
    pass


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


def _skin_png(board: str, scale: int = 1) -> bytes | None:
    """The board's drawn face, as the detail card's portrait."""
    key = f"{board}@{scale}"
    if key in _SKIN_CACHE:
        return _SKIN_CACHE[key]
    import base64
    module = _face_module(board)
    if module is None:
        return None
    face = module.build(scale)
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

    def _file(self, path: Path, kind: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._json({"error": f"no file at {path}"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

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

            if segs[1] == "skin" and len(segs) == 3:
                board = segs[2].removesuffix(".png")
                scale = int(self._query().get("scale", 1))
                png = _skin_png(board, scale)
                if png is None:
                    return self._json({"error": "no such board"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("Cache-Control", "max-age=86400")
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
                    return self._uart(handle, int(self._query().get("after", 0)))
                if segs[3] == "state":
                    path = handle.outdir / "board.json"
                    try:
                        return self._json(json.loads(path.read_text(encoding="utf-8")))
                    except (OSError, ValueError):
                        return self._json({})
            return self._json({"error": "not found"}, 404)
        except (NotFound, device_folder.DeviceError) as exc:
            return self._json({"error": str(exc)}, 404)
        except Conflict as exc:
            return self._json({"error": str(exc)}, 409)
        except Exception as exc:                     # never kill the server
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        try:
            segs = self._segments()
            if segs[:2] == ["api", "firmware"]:
                return self._upload()
            if segs[:2] == ["api", "devices"]:
                if len(segs) == 2:
                    return self._create()
                name = segs[2]
                if len(segs) == 4 and segs[3] == "start":
                    result = self.server.state.start(name)
                    return self._json(result)
                if len(segs) == 4 and segs[3] == "stop":
                    self.server.state.stop(name)
                    return self._json({"stopped": name})
                if len(segs) == 4 and segs[3] == "remove":
                    self._remove(name)
                    return self._json({"removed": name})
            if segs[:2] == ["api", "runs"] and len(segs) == 4 and segs[3] == "input":
                return self._input(segs[2])
            return self._json({"error": "not found"}, 404)
        except (NotFound, device_folder.DeviceError) as exc:
            return self._json({"error": str(exc)}, 404)
        except Conflict as exc:
            return self._json({"error": str(exc)}, 409)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_DELETE(self):
        try:
            segs = self._segments()
            if segs[:2] == ["api", "devices"] and len(segs) == 3:
                self._remove(segs[2])
                return self._json({"removed": segs[2]})
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    # -- the routes' bodies ---------------------------------------------------

    def _create(self) -> None:
        body = self._body()
        firmware = body.get("firmware", "")
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
        self._json(device_json(device), 201)

    def _remove(self, name: str) -> None:
        self.server.state.stop(name)
        device = device_folder.load(device_folder.folder(
            self.server.state.devices, name))
        device_folder.remove(device)

    def _upload(self) -> None:
        name = self._query().get("name", "firmware.bin")
        name = Path(name).name or "firmware.bin"
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return self._json({"error": "empty upload"}, 400)
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
                            bool(event.get("down", True)))
            elif kind == "text":
                bridge.text(str(event.get("text", "")))
            elif kind == "trackball":
                if "roll" in event:
                    writer.roll(str(event["roll"]), int(event.get("steps", 1)))
                else:
                    writer.ball(bool(event.get("press", True)))
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

    def _uart(self, handle: device_folder.RunHandle, after: int) -> None:
        path = handle.outdir / f"{handle.device.name}.uart.log"
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return self._json({"lines": [], "total": 0})
        lines = text.splitlines()
        return self._json({"lines": lines[after:], "total": len(lines)})


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state: ServerState):
        super().__init__(address, Handler)
        self.state = state


def make_server(devices: Path, port: int = 0) -> WebServer:
    """The server, bound and ready — `port 0` picks a free one, which is
    what tests use."""
    state = ServerState(devices)
    return WebServer(("127.0.0.1", port), state)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--dir", type=Path, default=REPO_ROOT / "devices",
                    help="where device folders live")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open a browser tab")
    args = ap.parse_args(argv)

    server = make_server(args.dir, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"BoardEmul web interface: {url}")
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
