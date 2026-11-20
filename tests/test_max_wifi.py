#!/usr/bin/env python3
"""The Max launcher's WiFi scan lists the access point (ADRs 0137, 0152).

Slow: boots the factory firmware with an access point on the air, drives
the launcher through the input API — the WiFi icon, "WIFI Scan" in the menu
it opens, then that screen's Scan button — and asserts the network's row
is drawn in the frame that follows. Until ADR 0152 the scan never finished:
the WiFi PHY's temperature read spun on a SENS block the Max did not have,
and this test, pressing a point inside "WIFI Config" instead, passed on any
new frame. Skips without a built QEMU or the firmware.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import FIRMWARE, qemu_available, read_png  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,      # deadlines kept on the host's time (ADR 0167)
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
]

WIFI_ICON = (57, 152)       # the launcher's row 2, column 1
WIFI_SCAN_ENTRY = (120, 133)    # "- WIFI Scan", the menu's second entry
SCAN_BUTTON = (35, 91)      # that screen's Scan button
# Where the scan's first result is drawn: a framed row under the button.
FIRST_ROW = (20, 120, 220, 146)     # x0, y0, x1, y1
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


def settled(client, since_name: str, timeout: float, quiet: float = 10.0) -> str | None:
    """The frame the panel rests on after a change: e-paper redraws in more
    than one pass, so the first new frame can be a pass in between."""
    latest = wait_new_frame(client, since_name, timeout)
    while latest:
        newer = wait_new_frame(client, latest, quiet)
        if not newer:
            break
        latest = newer
    return latest


def dark_pixels(client, name: str, box: tuple[int, int, int, int]) -> int:
    """How many pixels inside `box` of a frame are drawn dark."""
    status, png, _h = call(client, "GET", f"/api/runs/pocket/frames/{name}")
    assert status == 200
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(png)
    width, _height, pixels = read_png(Path(fh.name))
    Path(fh.name).unlink()
    step = len(pixels) // (width * _height)
    x0, y0, x1, y1 = box
    return sum(1 for y in range(y0, y1) for x in range(x0, x1)
               if pixels[(y * width + x) * step] < 128)


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
    if not launcher:
        # Once in a suite it did not draw, and nothing said why: the end of
        # the firmware's own log is what says whether it panicked or waited.
        _s, uart, _h = call(client, "GET", "/api/runs/pocket/uart?after=0")
        lines = uart.get("lines", []) if isinstance(uart, dict) else []
        tail = "\n".join(str(line) for line in lines[-25:])
        pytest.fail(f"the launcher never drew ({len(frames['frames'])} frame(s)); "
                    f"the UART ended:\n{tail}")
    tap(client, *WIFI_ICON)
    menu = settled(client, launcher, 90.0)
    assert menu, "tapping the WiFi icon changed nothing"
    tap(client, *WIFI_SCAN_ENTRY)
    wifi_screen = settled(client, menu, 90.0)
    assert wifi_screen, "tapping WIFI Scan changed nothing"
    assert dark_pixels(client, wifi_screen, FIRST_ROW) == 0

    # Press Scan. The screen redraws first to say it is scanning, then again
    # with the list: wait for the row the access point is drawn in.
    tap(client, *SCAN_BUTTON)
    listed, latest = 0, wifi_screen
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline and listed < 50:
        newer = wait_new_frame(client, latest, 30.0)
        if newer:
            latest = newer
            listed = dark_pixels(client, latest, FIRST_ROW)
    assert listed >= 50, (
        "the scan listed nothing: no row was drawn under the Scan button")

    status, uart, _h = call(client, "GET", "/api/runs/pocket/uart?after=0")
    assert status == 200 and uart["total"] > 0
