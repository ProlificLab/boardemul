# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Test firmware on emulated boards, from Python (ADR 0182).

    from boardemul import Lab

    with Lab() as lab:
        node = lab.device("node", build=".pio/build/t-deck", board="tdeck")
        node.start()
        node.serial("usb").expect(r"Booted", timeout=120)
        node.tap(120, 80)
        assert node.screen.wait(lambda shot: shot.pixel(2, 2) != (0, 0, 0), 60)

A Lab is a BoardEmul page -- the one at BOARDEMUL_URL, or one it starts --
and the devices it made there. Everything goes through the page's HTTP API;
nothing here imports the emulator. Timeouts are wall seconds.
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from boardemul.build import Build

SERIAL_PORTS = ("uart", "usb", "otg")
START_TIMEOUT = 300.0


@dataclass(frozen=True)
class WiFi:
    """An access point for a device to join (ADRs 0057, 0093, 0097, 0164).

    `password` makes it WPA2; without one it is open. `uplink` lets the
    device reach the internet through the host. `forward` maps ports of the
    host's loopback to the device's own, so a test reaches a server the
    firmware runs: {8080: 80} is the device's port 80 at 127.0.0.1:8080.
    """
    name: str
    password: str | None = None
    uplink: bool = False
    forward: dict[int, int] = field(default_factory=dict)

    def __post_init__(self):
        if not self.name or "," in self.name:
            raise ValueError("a network's name is not empty and has no comma")
        if self.password is not None and not 8 <= len(self.password) <= 63:
            raise ValueError("a WPA2 passphrase is 8 to 63 characters")
        for host, device in self.forward.items():
            if not (1024 <= int(host) <= 65535 and 1 <= int(device) <= 65535):
                raise ValueError(f"forward {host}->{device}: the host port is "
                                 "1024 to 65535, the device's 1 to 65535")

    def args(self) -> list[str]:
        """The run settings that make this network."""
        out = ["--wifi-ap", self.name]
        if self.password is not None:
            out += ["--wifi-wpa2", "--wifi-pass", self.password]
        if self.uplink:
            out.append("--wifi-uplink")
        for host, device in self.forward.items():
            out += ["--wifi-forward", f"{int(host)}:{int(device)}"]
        return out


class LabError(RuntimeError):
    """The page refused a request, or never answered."""


class Timeout(LabError):
    """What a test waited for did not come in time."""


# -- the page's API -----------------------------------------------------------


class Api:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def call(self, method: str, path: str, body=None, raw: bytes | None = None,
             timeout: float = 60.0):
        """JSON in and out; or raw bytes in, with `raw`. Returns (status,
        parsed body or bytes, headers)."""
        data, headers = None, {}
        if raw is not None:
            data, headers["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload, status, hdrs = response.read(), response.status, dict(response.headers)
        except urllib.error.HTTPError as exc:
            payload, status, hdrs = exc.read(), exc.code, dict(exc.headers)
        except (urllib.error.URLError, OSError) as exc:
            raise LabError(f"{method} {path}: {exc}") from exc
        if hdrs.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload or b"null")
        return status, payload, hdrs

    def ok(self, method: str, path: str, body=None, raw: bytes | None = None):
        status, payload, _h = self.call(method, path, body, raw)
        if status >= 300:
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise LabError(f"{method} {path}: {status} {error}")
        return payload


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- the lab ------------------------------------------------------------------


class Lab:
    """A page, and the devices made on it."""

    def __init__(self, url: str | None = None, start_timeout: float = START_TIMEOUT):
        self._server: subprocess.Popen | None = None
        url = url or os.environ.get("BOARDEMUL_URL")
        if not url:
            port = _free_port()
            url = f"http://127.0.0.1:{port}"
            self._server = subprocess.Popen(
                [sys.executable, "-m", "boardemul", "--port", str(port), "--no-open"],
                cwd=Path(__file__).resolve().parent.parent,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.api = Api(url)
        self.url = url
        self.devices: dict[str, Device] = {}
        self._wait_ready(start_timeout)

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server is not None and self._server.poll() is not None:
                raise LabError("the page stopped while starting: run `python "
                               "boardemul.py --check` to see what this machine lacks")
            try:
                status, _p, _h = self.api.call("GET", "/api/version", timeout=5)
                if status == 200:
                    return
            except LabError:
                pass
            time.sleep(0.5)
        raise Timeout(f"no page answered at {self.url} in {timeout:.0f} s")

    def device(self, name: str, build: str | Path | Build, board: str | Path = "tdeck",
               settings: tuple[str, ...] = ()) -> "Device":
        """A device made from `build`, replacing one of that name.

        `board` is tdeck, tdeck-max, or the path of a board description,
        NAME.toml (ADR 0184)."""
        build = build if isinstance(build, Build) else Build.from_dir(build)
        existing = {d["name"] for d in self.api.ok("GET", "/api/devices")["devices"]}
        if name in existing:
            self.api.call("POST", f"/api/devices/{_q(name)}/stop", {})
            self.api.ok("POST", f"/api/devices/{_q(name)}/remove", {})
        application = self._upload(build.application)
        overlays = []
        if build.filesystem is not None:
            path = self._upload(build.filesystem)
            overlays.append(f"0x{build.filesystem_offset:x}={path}")
        made = self.api.ok("POST", "/api/devices", {"name": name, **_board(board),
                                                     "firmware": application,
                                                     "overlays": overlays})
        if settings:
            # Beside what a device is made with (it restarts after a panic,
            # ADR 0145), not instead of it.
            kept = list(made.get("settings", [])) + [s for s in settings
                                                     if s not in made.get("settings", [])]
            self.api.ok("POST", f"/api/devices/{_q(name)}/settings", {"settings": kept})
        device = Device(self, name)
        self.devices[name] = device
        return device

    def _upload(self, path: Path) -> str:
        reply = self.api.ok("POST", f"/api/firmware?name={_q(path.name)}",
                            raw=Path(path).read_bytes())
        return reply["path"]

    def close(self) -> None:
        for device in self.devices.values():
            try:
                device.stop()
            except LabError:
                pass
        if self._server is not None:
            self._server.terminate()
            try:
                self._server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._server.kill()
            self._server = None

    def __enter__(self) -> "Lab":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _q(text: str) -> str:
    return urllib.parse.quote(text, safe="")


# -- a device -----------------------------------------------------------------


class Device:
    def __init__(self, lab: Lab, name: str):
        self.lab, self.name = lab, name
        self.screen = Screen(self)
        self._serial: dict[str, Serial] = {}
        self._forward: dict[int, int] = {}      # device port -> host port

    def start(self, air: str | None = None, args: tuple[str, ...] = (),
              wifi: WiFi | None = None, ble: bool = False) -> None:
        """Run it; devices started on the same air hear each other's LoRa
        radios, with `wifi` there is an access point to join, and with `ble`
        a Bluetooth air of its own that `ble()` reaches (ADR 0188)."""
        extra = list(args) + (["--air", air] if air else [])
        if wifi is not None:
            extra += wifi.args()
        if ble:
            extra += ["--ble-air", _air_name(self.name)]
        self.lab.api.ok("POST", f"/api/devices/{_q(self.name)}/start", {"args": extra})
        self._serial.clear()
        self._forward = ({device: host for host, device in wifi.forward.items()}
                         if wifi is not None else {})

    def ble(self, *ops: dict) -> list[dict]:
        """Bluetooth operations on the running device, over one connection
        (ADR 0188): {"op": "services"}, {"op": "characteristics", "uuid": U},
        {"op": "read", "uuid": U}, {"op": "write", "uuid": U, "hex": H},
        {"op": "pair", "passkey": N}. Each one's result, in order."""
        status, body, _h = self.lab.api.call(
            "POST", f"/api/runs/{_q(self.name)}/ble", {"ops": list(ops)}, timeout=660)
        reply = body if isinstance(body, dict) else json.loads(body or b"{}")
        if status != 200 or not reply.get("ok"):
            raise LabError(f"{self.name}: Bluetooth: "
                           f"{reply.get('error', f'status {status}')}")
        return reply["results"]

    def ble_services(self) -> list[str]:
        """The UUIDs of the services the device serves over Bluetooth."""
        return [s["uuid"] for s in self.ble({"op": "services"})[0]["services"]]

    def ble_read(self, uuid: str, passkey: int | None = None) -> bytes:
        """A characteristic's value, pairing first with `passkey` if given."""
        ops = ([{"op": "pair", "passkey": passkey}] if passkey is not None else [])
        return bytes.fromhex(self.ble(*ops, {"op": "read", "uuid": uuid})[-1]["hex"])

    def ble_write(self, uuid: str, data: bytes, passkey: int | None = None) -> None:
        """Write a characteristic, pairing first with `passkey` if given."""
        ops = ([{"op": "pair", "passkey": passkey}] if passkey is not None else [])
        self.ble(*ops, {"op": "write", "uuid": uuid, "hex": bytes(data).hex()})

    def http(self, path: str = "/", port: int = 80, timeout: float = 60.0,
             expect_status: int | None = 200) -> tuple[int, bytes]:
        """GET `path` from a server the firmware runs on its `port`, through
        the host port `WiFi.forward` gave it; tried until it answers with
        `expect_status` (any status, with None) or `timeout` passes."""
        host_port = self._forward.get(int(port))
        if host_port is None:
            raise LabError(f"{self.name}: no host port reaches its port {port}; "
                           f"start it with WiFi(forward={{HOSTPORT: {port}}})")
        url = f"http://127.0.0.1:{host_port}/{path.lstrip('/')}"
        deadline = time.monotonic() + timeout
        last = "nothing answered"
        while True:
            try:
                with urllib.request.urlopen(url, timeout=10) as reply:
                    status, body = reply.status, reply.read()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
            except (urllib.error.URLError, OSError) as exc:
                status, body, last = None, b"", str(getattr(exc, "reason", exc))
            if status is not None:
                if expect_status is None or status == expect_status:
                    return status, body
                last = f"status {status}"
            if time.monotonic() >= deadline:
                raise Timeout(f"{self.name}: GET {path} on port {port}: {last} "
                              f"in {timeout:.0f} s")
            time.sleep(1.0)

    def stop(self) -> None:
        self.lab.api.call("POST", f"/api/devices/{_q(self.name)}/stop", {})

    def serial(self, port: str = "uart") -> "Serial":
        if port not in SERIAL_PORTS:
            raise ValueError(f"a serial port is one of {', '.join(SERIAL_PORTS)}")
        return self._serial.setdefault(port, Serial(self, port))

    def _input(self, *events: dict) -> None:
        self.lab.api.ok("POST", f"/api/runs/{_q(self.name)}/input",
                        {"events": list(events)})

    def tap(self, x: int, y: int, hold: float = 0.3) -> None:
        self._input({"type": "touch", "x": int(x), "y": int(y), "down": True})
        time.sleep(hold)
        self._input({"type": "touch", "down": False})

    def key(self, name: str, hold: float = 0.15) -> None:
        self._input({"type": "key", "key": name, "down": True})
        time.sleep(hold)
        self._input({"type": "key", "key": name, "down": False})

    def type(self, text: str) -> None:
        self._input({"type": "text", "text": text})

    def save_logs(self, path: str | Path) -> Path:
        """The device's latest run's logs, as one zip (ADR 0163)."""
        status, body, _h = self.lab.api.call("GET", f"/api/devices/{_q(self.name)}/logs.zip")
        if status != 200:
            raise LabError(f"no logs for {self.name}: {status}")
        path = Path(path)
        path.write_bytes(body)
        return path


class Serial:
    """One serial port of a running device: its lines, and what to send it."""

    def __init__(self, device: Device, port: str):
        self.device, self.port = device, port
        self.seen = 0               # lines already looked at by expect()

    def lines(self) -> list[str]:
        """Every line the port has carried in this run."""
        reply = self.device.lab.api.ok(
            "GET", f"/api/runs/{_q(self.device.name)}/uart?port={self.port}&after=0")
        return reply["lines"]

    def send(self, text: str) -> None:
        self.device._input({"type": "serial", "port": self.port, "text": text})

    def expect(self, pattern: str, timeout: float = 60.0) -> re.Match:
        """The first line after the last one matched that matches `pattern`."""
        wanted = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while True:
            reply = self.device.lab.api.ok(
                "GET", f"/api/runs/{_q(self.device.name)}/uart"
                       f"?port={self.port}&after={self.seen}")
            for line in reply["lines"]:
                self.seen += 1
                found = wanted.search(line)
                if found:
                    return found
            if time.monotonic() >= deadline:
                raise Timeout(f"{self.device.name} {self.port}: no line matching "
                              f"{pattern!r} in {timeout:.0f} s")
            time.sleep(0.25)


class Shot:
    """One frame of a device's screen."""

    def __init__(self, png: bytes):
        self.png = png
        self.width, self.height, self.channels, self.data = decode_png(png)

    def pixel(self, x: int, y: int) -> tuple[int, ...]:
        at = (y * self.width + x) * self.channels
        value = tuple(self.data[at:at + self.channels])
        return value * 3 if self.channels == 1 else value

    def same_as(self, other: "Shot | bytes | str | Path", tolerance: int = 0) -> bool:
        """Whether two frames match, each channel within `tolerance`."""
        if isinstance(other, (str, Path)):
            other = Path(other).read_bytes()
        if isinstance(other, bytes):
            other = Shot(other)
        if (self.width, self.height, self.channels) != (other.width, other.height,
                                                        other.channels):
            return False
        return all(abs(a - b) <= tolerance for a, b in zip(self.data, other.data))


class Screen:
    def __init__(self, device: Device):
        self.device = device

    def shot(self, newer_than: int = 0) -> tuple[Shot | None, int]:
        """The latest frame newer than `newer_than` (the page's own stamp),
        waiting up to a quarter-minute; (None, newer_than) if none came."""
        status, body, headers = self.device.lab.api.call(
            "GET", f"/api/runs/{_q(self.device.name)}/frame?since={newer_than}", timeout=30)
        if status == 204:
            return None, newer_than
        if status != 200:
            raise LabError(f"{self.device.name}: no screen ({status})")
        return Shot(body), int(headers.get("X-Frame-Mtime", newer_than))

    def wait(self, predicate, timeout: float = 60.0) -> Shot:
        """The first frame `predicate(shot)` is true of."""
        deadline = time.monotonic() + timeout
        stamp = 0
        while time.monotonic() < deadline:
            shot, stamp = self.shot(stamp)
            if shot is not None and predicate(shot):
                return shot
        raise Timeout(f"{self.device.name}: no frame as wanted in {timeout:.0f} s")


# -- PNG, as the emulator writes it --------------------------------------------


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Width, height, channels and pixel bytes of an 8-bit greyscale or RGB
    PNG, unfiltered by each row's filter type."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise LabError("not a PNG")
    at, idat, width = 8, b"", 0
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        chunk = data[at + 8:at + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", chunk[:10])
            if depth != 8 or colour not in (0, 2):
                raise LabError("only 8-bit greyscale or RGB PNGs")
            channels = 1 if colour == 0 else 3
        elif kind == b"IDAT":
            idat += chunk
        at += 12 + length
    raw = zlib.decompress(idat)
    stride = width * channels
    out = bytearray()
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                pred = left if pa <= pb and pa <= pc else up if pb <= pc else corner
                line[i] = (line[i] + pred) & 0xFF
        out += line
        previous = line
    return width, height, channels, bytes(out)


def _board(board: str | Path) -> dict:
    """The board as the page's API takes it: a name, or a description's text."""
    if isinstance(board, Path) or str(board).endswith(".toml"):
        path = Path(board)
        try:
            return {"board_description": path.read_text(encoding="utf-8")}
        except OSError as exc:
            raise LabError(f"no board description at {path}: {exc.strerror or exc}") from None
    return {"board": str(board)}


def _air_name(name: str) -> str:
    """A device's own air, as the page takes an air's name: lower-case
    letters, digits and dashes."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:28] or "device"
    return f"{cleaned}-ble"[:32]
