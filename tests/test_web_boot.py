#!/usr/bin/env python3
"""The web front door against a real guest: boot, touch, see the frame move.

Slow: starts a device through the server's own registry, long-polls the
panel until the launcher draws, taps an icon through the input API, and
holds the next frame up as evidence — the same trip a browser makes, minus
the pixels' trip to your screen. Skips without a built QEMU or the firmware.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import FIRMWARE, qemu_available  # noqa: E402
from tests.support import region_digest  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
]

SETTING_ICON = (120, 75)            # the launcher's top-row middle icon


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    from emu import device as device_folder
    from emu.web import make_server

    work = tmp_path_factory.mktemp("web")
    device = device_folder.create(
        work / "devices", "pocket", FIRMWARE, board="tdeck-max")
    server = make_server(work / "devices", 0)
    thread = threading_start(server)
    client = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
    yield {"client": client, "device": device, "state": server.state}
    client.close()
    server.state.stop_all()
    server.shutdown()
    thread.join(timeout=10)


def threading_start(server):
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


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


def wait_frame(client: http.client.HTTPConnection, since: int, timeout: float):
    """One long-poll, with the client-side patience the browser has."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.request("GET", f"/api/runs/pocket/frame?since={since}")
        res = client.getresponse()
        body = res.read()
        if res.status == 200:
            return body, int(res.headers["X-Frame-Mtime"])
        time.sleep(0.5)
    return None, since


def test_boot_touch_and_the_frame_moves(web) -> None:
    client = web["client"]

    # -- start through the registry, the way the page's button does ---------
    status, run, _h = call(client, "POST", "/api/devices/pocket/start")
    assert status == 200 and run["board"] == "tdeck-max"
    assert "pocket" in web["state"].runs

    # -- the launcher draws, and the long poll hands it over ---------------
    body, mtime = wait_frame(client, 0, 300.0)
    assert body, "the launcher never drew"
    launcher = region_digest(_as_frame(body))
    status, listing, _h = call(client, "GET", "/api/devices")
    assert listing["devices"][0]["running"] is True

    # -- a tap through the input API, then the panel moves ------------------
    status, _accepted, _h = call(client, "POST", "/api/runs/pocket/input",
                                 {"events": [
                                     {"type": "touch", "x": SETTING_ICON[0],
                                      "y": SETTING_ICON[1], "down": True},
                                     {"type": "touch", "down": False}]})
    assert status == 200

    body2, mtime2 = wait_frame(client, mtime, 120.0)
    assert body2, "the tap drew nothing"
    assert region_digest(_as_frame(body2)) != launcher, \
        "the tap changed nothing on the panel"

    # -- the serial console endpoint has the boot in it ---------------------
    status, uart, _h = call(client, "GET", "/api/runs/pocket/uart?after=0")
    assert status == 200 and uart["total"] > 0

    # -- stop through the API, the way the Stop button does -----------------
    status, _stopped, _h = call(client, "POST", "/api/devices/pocket/stop")
    assert status == 200
    assert "pocket" not in web["state"].runs


def _as_frame(png: bytes) -> Path:
    import tempfile
    frame = Path(tempfile.mkdtemp()) / "frame.png"
    frame.write_bytes(png)
    return frame
