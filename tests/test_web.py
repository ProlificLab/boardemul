#!/usr/bin/env python3
"""The web front door's API, over real HTTP on a loopback port.

Fast: the assembler is a stub and `RunHandle.start` is recorded rather than
spawned, so no QEMU and no guest is involved. What is exercised is the
contract the page leans on: devices in, storage uploaded, a run started,
input accepted, frames long-polled.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import device as device_folder  # noqa: E402
from emu import run, web  # noqa: E402


FIRMWARE_BYTES = bytes([0xE9]) + b"\x00" * 64


@pytest.fixture()
def server(tmp_path, monkeypatch):
    devices = tmp_path / "devices"

    def build(firmware, outdir, mac, overlays):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "flash.bin").write_bytes(b"flash" * 1024)
        (outdir / "efuse.bin").write_bytes(b"efuse")
        return outdir / "flash.bin", outdir / "efuse.bin"

    monkeypatch.setattr(run, "build_images", build)
    started = []
    monkeypatch.setattr(device_folder.RunHandle, "start",
                        lambda self: started.append(self.device.name))

    srv = web.make_server(devices, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
    yield client, devices, started
    srv.state.stop_all()
    srv.shutdown()
    thread.join(timeout=5)


def call(client: http.client.HTTPConnection, method: str, path: str,
         body: bytes | dict | None = None):
    headers = {}
    payload = b""
    if isinstance(body, dict):
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif body is not None:
        payload = body
        headers["Content-Type"] = "application/octet-stream"
    client.request(method, path, body=payload, headers=headers)
    res = client.getresponse()
    data = res.read()
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = data
    return res.status, parsed, dict(res.getheaders())


def test_the_page_serves(server):
    client, _d, _s = server
    status, body, headers = call(client, "GET", "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"BoardEmul" in body


def test_no_devices_yet(server):
    client, _d, _s = server
    status, body, _h = call(client, "GET", "/api/devices")
    assert status == 200 and body == {"devices": []}


def test_firmware_upload_then_create_then_list(server):
    client, devices, _s = server
    status, body, _h = call(client, "POST",
                            "/api/firmware?name=demo.bin", FIRMWARE_BYTES)
    assert status == 200 and body["bytes"] == len(FIRMWARE_BYTES)
    assert Path(body["path"]).is_file()

    status, made, _h = call(client, "POST", "/api/devices", {
        "name": "pocket", "board": "tdeck-max", "firmware": body["path"]})
    assert status == 201 and made["name"] == "pocket"
    assert made["storage"]  # "32 MB flash · 32 MB card"

    status, listing, _h = call(client, "GET", "/api/devices")
    assert [d["name"] for d in listing["devices"]] == ["pocket"]


def test_creating_a_duplicate_conflicts(server):
    client, devices, _s = server
    path = _up(client, devices)
    call(client, "POST", "/api/devices",
         {"name": "pocket", "board": "tdeck-max", "firmware": path})
    status, body, _h = call(client, "POST", "/api/devices",
                            {"name": "pocket", "board": "tdeck-max",
                             "firmware": path})
    assert status == 409


def _up(client, devices):
    status, body, _h = call(client, "POST", "/api/firmware?name=demo.bin",
                            FIRMWARE_BYTES)
    assert status == 200
    return body["path"]


def _fw_path(devices):
    return str(devices / ".uploads" / "seeded.bin")


def test_start_run_and_send_input(server, tmp_path):
    client, devices, started = server
    path = _up(client, devices)
    call(client, "POST", "/api/devices",
         {"name": "pocket", "board": "tdeck-max", "firmware": path})

    status, run, _h = call(client, "POST", "/api/devices/pocket/start")
    assert status == 200 and run["id"] == "pocket" and run["board"] == "tdeck-max"
    assert started == ["pocket"]

    status, events, _h = call(client, "POST", "/api/runs/pocket/input", {"events": [
        {"type": "touch", "x": 120, "y": 75, "down": True},
        {"type": "touch", "down": False},
        {"type": "key", "key": "Q", "down": True},
        {"type": "hostkey", "keysym": "a", "char": "a", "down": True},
    ]})
    assert status == 200

    log = (Path(run["outdir"]) / "input.jsonl").read_text()
    kinds = [json.loads(line) for line in log.splitlines()]
    assert {"type": "touch", "x": 120, "y": 75, "down": True} in kinds
    assert {"type": "key", "key": "Q", "down": True} in kinds
    # The Max's plainer host mapping: a letter is its key.
    assert {"type": "key", "key": "A", "down": True} in kinds

    status, listing, _h = call(client, "GET", "/api/devices")
    assert listing["devices"][0]["running"] is True

    status, body, _h = call(client, "POST", "/api/devices/pocket/stop")
    assert status == 200


def test_input_to_a_run_that_is_not_there_404s(server):
    client, _d, _s = server
    status, body, _h = call(client, "POST", "/api/runs/ghost/input",
                            {"events": [{"type": "touch", "x": 1, "y": 1}]})
    assert status == 404


def test_a_quiet_frame_long_poll_times_out_with_204(server, monkeypatch):
    client, devices, _s = server
    path = _up(client, devices)
    call(client, "POST", "/api/devices",
         {"name": "pocket", "board": "tdeck-max", "firmware": path})
    call(client, "POST", "/api/devices/pocket/start")
    monkeypatch.setattr(web, "FRAME_POLL_SECONDS", 0.3)
    status, _body, _h = call(client, "GET", "/api/runs/pocket/frame?since=0")
    assert status == 204


def test_a_new_frame_comes_back_from_the_long_poll(server, monkeypatch):
    import os
    import time as _time
    client, devices, _s = server
    path = _up(client, devices)
    call(client, "POST", "/api/devices",
         {"name": "pocket", "board": "tdeck-max", "firmware": path})
    call(client, "POST", "/api/devices/pocket/start")

    run_dir = Path(web_state_run_outdirs(client)[0])
    frames = run_dir / "frames"
    frames.mkdir(parents=True)
    latest = frames / "latest.png"
    latest.write_bytes(b"the old frame")
    since = os.stat(latest).st_mtime_ns

    def write_later():
        _time.sleep(0.4)
        latest.write_bytes(b"a fresh frame")

    monkeypatch.setattr(web, "FRAME_POLL_SECONDS", 5.0)
    threading.Thread(target=write_later, daemon=True).start()
    status, body, headers = call(client, "GET",
                                 f"/api/runs/pocket/frame?since={since}")
    assert status == 200 and body == b"a fresh frame"
    assert "X-Frame-Mtime" in headers


def web_state_run_outdirs(client):
    """The registered run's outdir, read back through the API."""
    status, runs, _h = call(client, "GET", "/api/runs")
    assert status == 200
    return [r["outdir"] for r in runs["runs"]]


def test_remove_stops_and_deletes(server):
    client, devices, started = server
    path = _up(client, devices)
    call(client, "POST", "/api/devices",
         {"name": "pocket", "board": "tdeck-max", "firmware": path})
    call(client, "POST", "/api/devices/pocket/start")
    status, _body, _h = call(client, "DELETE", "/api/devices/pocket")
    assert status == 200
    assert not (devices / "pocket").exists()
    status, listing, _h = call(client, "GET", "/api/devices")
    assert listing["devices"] == []


def test_the_detail_portrait_serves_the_drawn_face(server):
    client, _d, _s = server
    status, body, headers = call(client, "GET", "/api/skin/tdeck-max.png")
    assert status == 200
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
