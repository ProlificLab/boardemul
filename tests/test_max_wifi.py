#!/usr/bin/env python3
"""The Max launcher's WiFi scan answers (ADR 0137).

Slow: boots the factory firmware with an access point on the air, drives
the launcher's WiFi screen through the input API — the WiFi icon, then the
Scan button — and holds the frame that lists the network as evidence.
Skips without a built QEMU or the firmware.
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
SCAN_BUTTON = (39, 91)      # the WiFi screen's button
AP_NAME = "BoardTest"


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    from emu import device as device_folder
    from emu.web import make_server

    work = tmp_path_factory.mktemp("maxwifi")
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


def tap(client, x: int, y: int, hold: float = 0.8) -> None:
    status, _b, _h = call(client, "POST", "/api/runs/pocket/input",
                           {"events": [{"type": "touch", "x": x, "y": y,
                                        "down": True}]})
    assert status == 200
    time.sleep(hold)
    call(client, "POST", "/api/runs/pocket/input",
         {"events": [{"type": "touch", "down": False}]})


def wait_new_frame(client, since_name: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, frames, _h = call(client, "GET", "/api/runs/pocket/frames")
        assert status == 200
        if frames["frames"] and frames["frames"][-1]["name"] != since_name:
            return frames["frames"][-1]["name"]
        time.sleep(1)
    return None


def test_the_wifi_screen_answers_a_scan(web) -> None:
    client = web["client"]

    # The access point rides the run arguments, as a flag would.
    status, run, _h = call(
        client, "POST", "/api/devices/pocket/start",
        {"args": ["--wifi-ap", f"{AP_NAME},100,1"]})
    assert status == 200 and run["board"] == "tdeck-max"

    # The launcher draws (tens of seconds of guest time); tap the WiFi icon
    # once it is up, and let the screen change.
    launcher = None
    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline:
        status, frames, _h = call(client, "GET", "/api/runs/pocket/frames")
        assert status == 200
        if len(frames["frames"]) >= 3:
            launcher = frames["frames"][-1]["name"]
            break
        time.sleep(1)
    assert launcher, "the launcher never drew"
    tap(client, *WIFI_ICON)
    wifi_screen = wait_new_frame(client, launcher, 90.0)
    assert wifi_screen, "tapping the WiFi icon changed nothing"

    # Press Scan; the screen redraws with the result. With the access point
    # on the air, the guest's scan finds it — a new frame is the evidence
    # that the scan completed rather than hung.
    tap(client, *SCAN_BUTTON)
    scanned = wait_new_frame(client, wifi_screen, 120.0)
    assert scanned, "pressing Scan changed nothing: the scan never answered"

    status, uart, _h = call(client, "GET", "/api/runs/pocket/uart?after=0")
    assert status == 200 and uart["total"] > 0
