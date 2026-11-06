#!/usr/bin/env python3
"""The Max launcher's keyboard: decoded, queued, and ignored — recorded.

The vendor's own `peri_keypad.cpp` decodes TCA8418 events into its queue
but the bridge to the UI is commented out (`keypad_listener` is never
invoked), so the launcher is touch-only by LilyGO's code, not by a gap in
the emulator. This test holds that record: touches move the panel, keys do
not — and if a firmware starts acting on keys, the second assertion fails
and the record updates itself.

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

WIFI_ICON = (57, 152)       # a launcher target that demonstrably navigates


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
    url = f"/api/runs/pocket/frames/{latest}"
    status, body, _h = call(client, "GET", url)
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


def wait_change(client, old: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if digest(client) != old:
            return True
        time.sleep(1)
    return False


def test_touch_moves_the_panel_keys_do_not(web) -> None:
    client = web["client"]

    status, run, _h = call(client, "POST", "/api/devices/pocket/start", {})
    assert status == 200

    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline:
        status, frames, _h = call(client, "GET", "/api/runs/pocket/frames")
        if status == 200 and len(frames["frames"]) >= 3:
            break
        time.sleep(1)
    else:
        pytest.fail("the launcher never drew")
    time.sleep(8)                       # let the first screen settle

    # The guard: touches demonstrably navigate — tap the WiFi icon.
    before = digest(client)
    tap(client, *WIFI_ICON)
    assert wait_change(client, before, 90.0), "even the touch did nothing"

    # The record: the matrix's keys are decoded into the vendor's queue and
    # the launcher ignores them — UP, ENTER, SPACE and a letter alike.
    for name in ("UP", "ENTER", "SPACE", "Q"):
        settle = digest(client)
        key(client, name)
        assert not wait_change(client, settle, 20.0), (
            f"the launcher reacted to {name}: the vendor's keypad-to-UI "
            "bridge is no longer commented out in peri_keypad.cpp — update "
            "this record to what the keys now do")
