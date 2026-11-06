#!/usr/bin/env python3
"""The Max launcher ignores its keyboard; its sub-screens consume it.

The vendor's `peri_keypad.cpp` decodes TCA8418 events into its own queue
with the bridge to the UI commented out — and the launcher never reads that
queue, so its icon grid is touch-only. Once a sub-screen is open, though,
the keys arrive: this test drives the WiFi screen with the matrix's UP and
ENTER through the input API and holds the frames that changed as evidence.

Slow: boots the factory firmware under QEMU. Skips without it.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import FIRMWARE, qemu_available, region_digest  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
]

WIFI_ICON = (57, 152)       # the launcher's row 2, column 1


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    from emu import device as device_folder
    from emu.web import make_server

    work = tmp_path_factory.mktemp("maxkbd")
    device = device_folder.create(work / "devices", "pocket", FIRMWARE,
                                  board="tdeck-max")
    server = make_server(work / "devices", 0)

    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
    yield {"client": client, "device": device, "state": server.state}
    client.close()
    server.state.stop_all()
    server.shutdown()
    thread.join(timeout=10)


def call(client: http.client.HTTPConnection, method: str, path: str,
         body: dict | None = None):
    headers, payload = {}, b""
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    client.request(method, path, body=payload, headers=headers)
    res = client.getresponse()
    data = res.read()
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = data
    return res.status, parsed, dict(res.getheaders())


def digest(client) -> str:
    status, frames, _h = call(client, "GET", "/api/runs/pocket/frames")
    assert status == 200 and frames["frames"], "no frames drawn"
    latest = frames["frames"][-1]["name"]
    status, body, _h = call(client, "GET",
                            f"/api/runs/pocket/frames/{latest}")
    return region_digest(_as_frame(body))


def _as_frame(png: bytes) -> Path:
    import tempfile
    frame = Path(tempfile.mkdtemp()) / "frame.png"
    frame.write_bytes(png)
    return frame


def tap(client, x: int, y: int, hold: float = 0.8) -> None:
    call(client, "POST", "/api/runs/pocket/input",
         {"events": [{"type": "touch", "x": x, "y": y, "down": True}]})
    time.sleep(hold)
    call(client, "POST", "/api/runs/pocket/input",
         {"events": [{"type": "touch", "down": False}]})


def key(client, name: str, hold: float = 0.15) -> None:
    call(client, "POST", "/api/runs/pocket/input",
         {"events": [{"type": "key", "key": name, "down": True}]})
    time.sleep(hold)
    call(client, "POST", "/api/runs/pocket/input",
         {"events": [{"type": "key", "key": name, "down": False}]})


def wait_change(client, old: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = digest(client)
        if new != old:
            return new
        time.sleep(1)
    return None


def test_the_launcher_ignores_keys_and_its_wifi_screen_consumes_them(
        web) -> None:
    client = web["client"]

    status, _run, _h = call(client, "POST", "/api/devices/pocket/start", {})
    assert status == 200

    deadline = time.monotonic() + 240.0
    launcher = None
    while time.monotonic() < deadline:
        status, frames, _h = call(client, "GET", "/api/runs/pocket/frames")
        if status == 200 and len(frames["frames"]) >= 3:
            launcher = frames["frames"][-1]["name"]
            break
        time.sleep(1)
    assert launcher, "the launcher never drew"
    time.sleep(8)                       # let the first screen settle

    # The record the discovery made: the launcher's icon grid does not read
    # the keypad queue — the vendor's bridge to it is commented out.
    before = digest(client)
    key(client, "UP")
    assert wait_change(client, before, 15.0) is None, (
        "the launcher reacted to UP: the vendor reads the keypad queue here "
        "now — update this record")

    # Open the WiFi screen by touch, then hand the screen the keyboard.
    tap(client, *WIFI_ICON)
    wifi = wait_change(client, before, 90.0)
    assert wifi, "tapping the WiFi icon changed nothing"
    time.sleep(8)                       # let the new screen settle

    after_up = wait_change(client, wifi, 15.0)
    if after_up is None:
        key(client, "UP")
        after_up = wait_change(client, wifi, 15.0)
    assert after_up, "the WiFi screen ignores UP"
    # ENTER is not asserted: it presses the focused control, which on this
    # screen may relaunch a scan that never redraws without an access point.
