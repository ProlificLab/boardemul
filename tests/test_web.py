#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
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
    monkeypatch.setattr(device_folder.RunHandle, "alive", property(lambda self: True))

    srv = web.make_server(devices, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
    yield client, devices, started
    srv.state.stop_all()
    srv.shutdown()
    thread.join(timeout=5)


def call(client: http.client.HTTPConnection, method: str, path: str,
         body: bytes | dict | None = None, headers: dict | None = None):
    headers = dict(headers or {})
    payload = b""
    if isinstance(body, dict):
        payload = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    elif body is not None:
        payload = body
        headers.setdefault("Content-Type", "application/octet-stream")
    client.request(method, path, body=payload, headers=headers)
    res = client.getresponse()
    data = res.read()
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = data
    return res.status, parsed, dict(res.getheaders())


def test_a_second_server_on_a_taken_port_fails_and_says_so(tmp_path, capsys):
    """On Windows SO_REUSEADDR let a second start bind the same port, and the
    page's requests went to two servers of different ages. A second server
    must fail instead, on every platform."""
    first = web.make_server(tmp_path / "devices", 0)
    port = first.server_address[1]
    try:
        with pytest.raises(OSError):
            web.make_server(tmp_path / "devices", port)
        assert web.main(["--port", str(port), "--no-open",
                         "--dir", str(tmp_path / "devices")]) == 1
        assert f"port {port} is in use" in capsys.readouterr().err
    finally:
        first.server_close()


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


def test_the_tdeck_takes_typing_pressed_and_released(server):
    """ADR 0127's chords through the page: a capital is Shift and the letter,
    and both come back up when the key does, even if Shift was let go first
    and the browser named the release in lower case. Enter and Backspace
    arrive under the browser's names."""
    client, devices, _s = server
    _tdeck(client, devices, name="typist")
    status, run, _h = call(client, "POST", "/api/devices/typist/start")
    events = [
        {"type": "hostkey", "keysym": "N", "char": "N", "code": "KeyN", "down": True},
        {"type": "hostkey", "keysym": "n", "char": "n", "code": "KeyN", "down": False},
        {"type": "hostkey", "keysym": "n", "char": "n", "code": "KeyN", "down": True},
        {"type": "hostkey", "keysym": "n", "char": "n", "code": "KeyN", "down": False},
        {"type": "hostkey", "keysym": "Enter", "char": "Enter", "code": "Enter", "down": True},
        {"type": "hostkey", "keysym": "Enter", "char": "Enter", "code": "Enter", "down": False},
        {"type": "hostkey", "keysym": "Backspace", "char": "Backspace",
         "code": "Backspace", "down": True},
        {"type": "hostkey", "keysym": "Backspace", "char": "Backspace",
         "code": "Backspace", "down": False},
    ]
    status, _b, _h = call(client, "POST", "/api/runs/typist/input", {"events": events})
    assert status == 200
    log = (Path(run["outdir"]) / "input.jsonl").read_text()
    keys = [(e["key"], e["down"]) for e in map(json.loads, log.splitlines())
            if e["type"] == "key"]
    try:
        assert keys == [("LSHIFT", True), ("N", True), ("N", False), ("LSHIFT", False),
                        ("N", True), ("N", False),
                        ("ENTER", True), ("ENTER", False),
                        ("DEL", True), ("DEL", False)], keys
    finally:
        call(client, "POST", "/api/devices/typist/stop")
        import shutil
        shutil.rmtree(Path(run["outdir"]).parent, ignore_errors=True)


def test_text_for_the_tdeck_goes_to_its_keypad(server):
    """A "text" event, which scripts send, reaches the board's keypad as the
    same event: it types each character as its chord (ADR 0127)."""
    client, devices, _s = server
    _tdeck(client, devices, name="writer")
    status, run, _h = call(client, "POST", "/api/devices/writer/start")
    status, body, _h = call(client, "POST", "/api/runs/writer/input",
                            {"events": [{"type": "text", "text": "Hi!"}]})
    try:
        assert status == 200, body
        last = (Path(run["outdir"]) / "input.jsonl").read_text().splitlines()[-1]
        assert json.loads(last) == {"type": "text", "text": "Hi!"}
    finally:
        call(client, "POST", "/api/devices/writer/stop")
        import shutil
        shutil.rmtree(Path(run["outdir"]).parent, ignore_errors=True)


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


def test_the_geometry_names_every_cap_and_the_panel(server):
    client, _d, _s = server
    status, geo, _h = call(client, "GET", "/api/skin/tdeck-max/geometry")
    assert status == 200
    assert len(geo["keys"]) == 35
    assert geo["screen"]["w"] == 240 and geo["screen"]["h"] == 320
    assert geo["trackball"] is None              # the Max has none
    for key in geo["keys"]:
        assert key["x1"] > key["x0"] and key["y1"] > key["y0"]

    status, geo, _h = call(client, "GET", "/api/skin/tdeck/geometry")
    assert status == 200
    assert len(geo["keys"]) == 35
    assert geo["trackball"]["r"] > 0             # the T-Deck has one
    assert geo["screen"]["w"] == 320 and geo["screen"]["h"] == 240


# -- only its own page (ADR 0139) -------------------------------------------


def _pocket(client, devices):
    call(client, "POST", "/api/devices",
         {"name": "pocket", "board": "tdeck-max", "firmware": _up(client, devices)})


def _port(client) -> int:
    return client.port


def test_a_page_from_elsewhere_cannot_start_a_device(server):
    """A cross-origin POST carries its Origin; the server refuses it."""
    client, devices, started = server
    _pocket(client, devices)
    status, body, _h = call(client, "POST", "/api/devices/pocket/start",
                            headers={"Origin": "https://example.com"})
    assert status == 403 and started == []


def test_the_servers_own_page_may(server):
    client, devices, started = server
    _pocket(client, devices)
    for origin in (f"http://127.0.0.1:{_port(client)}", f"http://localhost:{_port(client)}"):
        status, _b, _h = call(client, "POST", "/api/devices/pocket/start",
                              headers={"Origin": origin})
        assert status == 200, origin
    assert started == ["pocket"]          # the second start opened the first


def test_a_body_that_does_not_say_json_is_refused(server):
    """What a no-cors page can send: text/plain. It is not taken as JSON."""
    client, devices, started = server
    _pocket(client, devices)
    status, _b, _h = call(client, "POST", "/api/devices", json.dumps(
        {"name": "other", "board": "tdeck-max", "firmware": "x"}).encode(),
        headers={"Content-Type": "text/plain"})
    assert status == 415
    assert not (devices / "other").exists()


def test_an_upload_must_say_what_it_is(server):
    client, devices, _s = server
    status, _b, _h = call(client, "POST", "/api/firmware?name=x.exe", b"MZ" * 64,
                          headers={"Content-Type": "text/plain"})
    assert status == 415
    assert not (devices / ".uploads").exists()


def test_a_rebound_host_reads_nothing(server):
    """DNS rebinding keeps the attacker's name in Host."""
    client, _d, _s = server
    status, _b, _h = call(client, "GET", "/api/devices",
                          headers={"Host": f"evil.example:{_port(client)}"})
    assert status == 403


def test_a_name_the_server_was_told_is_accepted(tmp_path):
    srv = web.make_server(tmp_path / "devices", 0, names=("deck.local", "box:9000"))
    try:
        port = srv.server_address[1]
        assert f"deck.local:{port}" in srv.hosts and "box:9000" in srv.hosts
        assert f"localhost:{port}" in srv.hosts
    finally:
        srv.server_close()


@pytest.mark.parametrize("args", [
    ["--qemu", "C:/Users/x/evil.exe"],
    ["--outdir", "/tmp/elsewhere"],
    ["--overlay", "0x0=/etc/passwd"],
    ["--air", "/tmp/air"],
    ["--wifi-ap", "--qemu"],
    ["--wifi-ap"],
    ["--qemu=/bin/sh"],
    "--icount",
])
def test_start_refuses_anything_but_run_settings(server, args):
    client, devices, started = server
    _pocket(client, devices)
    status, _b, _h = call(client, "POST", "/api/devices/pocket/start", {"args": args})
    assert status == 400 and started == []


def test_start_passes_the_run_settings_through(server, monkeypatch):
    client, devices, _s = server
    _pocket(client, devices)
    seen = []
    monkeypatch.setattr(device_folder.RunHandle, "start",
                        lambda self: seen.append(self.extra))
    args = ["--wifi-ap", "deck,100,1", "--wifi-wpa2", "--wifi-pass", "secret123",
            "--gps", "48.85,2.35", "--reboot"]
    status, _b, _h = call(client, "POST", "/api/devices/pocket/start", {"args": args})
    assert status == 200 and seen == [tuple(args)]


@pytest.mark.parametrize("args", [
    ["--air", "../devices"], ["--air", "Mesh"], ["--ble-air", "a/b"],
    ["--air", "-x"], ["--air", "m" * 33],
])
def test_an_air_is_a_name_never_a_path(server, args):
    """ADR 0145: the page names an air; where it lives is the run's to say."""
    client, devices, started = server
    _pocket(client, devices)
    status, _b, _h = call(client, "POST", "/api/devices/pocket/start", {"args": args})
    assert status == 400 and started == []


def test_a_device_keeps_its_settings(server):
    client, devices, _s = server
    _pocket(client, devices)
    kept = ["--air", "mesh", "--at", "120,0", "--gps", "48.85,2.35", "--reboot"]
    status, body, _h = call(client, "POST", "/api/devices/pocket/settings",
                            {"settings": kept})
    assert status == 200 and body["settings"] == kept
    manifest = json.loads((devices / "pocket" / "device.json").read_text())
    assert manifest["settings"] == kept
    status, body, _h = call(client, "GET", "/api/devices")
    assert body["devices"][0]["settings"] == kept


def test_settings_are_held_to_the_same_list(server):
    client, devices, _s = server
    _pocket(client, devices)
    status, body, _h = call(client, "POST", "/api/devices/pocket/settings",
                            {"settings": ["--qemu", "/bin/sh"]})
    assert status == 400
    # Left as they were: what a device made from the page starts with.
    assert json.loads((devices / "pocket" / "device.json").read_text())["settings"] \
        == list(device_folder.OPERATOR_SETTINGS)


def test_a_device_made_from_the_page_restarts_after_a_panic(server):
    """ADR 0145, decision 6: as the board does, until it is unticked."""
    client, devices, _s = server
    _pocket(client, devices)
    status, body, _h = call(client, "GET", "/api/devices")
    assert body["devices"][0]["settings"] == ["--reboot"]


def test_settings_for_a_device_that_is_not_there_404(server):
    client, _devices, _s = server
    status, _b, _h = call(client, "POST", "/api/devices/ghost/settings",
                          {"settings": ["--reboot"]})
    assert status == 404


def test_the_speakers_streams_are_listed_and_served(server):
    """ADR 0146: each played stream is a WAV file in the run's audio/."""
    import wave
    client, devices, _s = server
    _pocket(client, devices)
    call(client, "POST", "/api/devices/pocket/start", {"args": ["--audio"]})
    outdir = Path(web_state_run_outdirs(client)[0])
    (outdir / "audio").mkdir()
    with wave.open(str(outdir / "audio" / "i2s0-1.wav"), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x10" * 2 * 8000)
    (outdir / "audio" / "notes.txt").write_text("not a stream")
    status, body, _h = call(client, "GET", "/api/runs/pocket/audio")
    assert status == 200
    assert body["streams"] == [{"name": "i2s0-1.wav", "seconds": 0.5,
                                "rate": 16000, "channels": 2,
                                "bytes": 44 + 32000}]
    status, data, headers = call(client, "GET", "/api/runs/pocket/audio/i2s0-1.wav")
    assert status == 200 and headers["Content-Type"] == "audio/wav"
    assert data[:4] == b"RIFF"
    for name in ("notes.txt", "..%2F..%2Fdevice.json", "i2s0-1.wav.bak"):
        status, _b, _h = call(client, "GET", f"/api/runs/pocket/audio/{name}")
        assert status == 404


def _tdeck(client, devices, name="node"):
    status, _b, _h = call(client, "POST", "/api/devices",
                          {"name": name, "board": "tdeck", "firmware": _up(client, devices)})
    assert status == 201


def test_a_stopped_device_is_snapshotted_and_restored(server):
    """ADR 0149: storage, card and settings come back together."""
    client, devices, _s = server
    _tdeck(client, devices)
    card = devices / "node" / "sdcard.img"
    card.write_bytes(b"A" * card.stat().st_size)
    call(client, "POST", "/api/devices/node/settings", {"settings": ["--reboot"]})
    status, taken, _h = call(client, "POST", "/api/devices/node/snapshots",
                             {"label": "set-up"})
    assert status == 201 and taken["label"] == "set-up"
    card.write_bytes(b"B" * card.stat().st_size)
    call(client, "POST", "/api/devices/node/settings", {"settings": []})
    status, listed, _h = call(client, "GET", "/api/devices/node/snapshots")
    assert [s["label"] for s in listed["snapshots"]] == ["set-up"]
    status, device, _h = call(client, "POST", "/api/devices/node/snapshots/set-up/restore")
    assert status == 200 and device["settings"] == ["--reboot"]
    assert card.read_bytes()[:1] == b"A"
    status, _b, _h = call(client, "DELETE", "/api/devices/node/snapshots/set-up")
    assert status == 200
    assert call(client, "GET", "/api/devices/node/snapshots")[1]["snapshots"] == []


def test_a_running_device_is_not_snapshotted(server):
    client, devices, _s = server
    _tdeck(client, devices)
    call(client, "POST", "/api/devices/node/start", {})
    status, body, _h = call(client, "POST", "/api/devices/node/snapshots")
    assert status == 409 and "stop it first" in body["error"]


def test_a_runs_logs_download_as_one_zip_that_names_its_build(server, tmp_path,
                                                                monkeypatch):
    """ADR 0163: live, and after the run is stopped."""
    import io
    import shutil
    import zipfile
    from emu.version import VERSION
    client, devices, _s = server
    _tdeck(client, devices, name="zipped")
    status, body, _h = call(client, "GET", "/api/devices/zipped/logs.zip")
    assert status == 404 and "has not run" in body["error"]
    monkeypatch.setattr(web, "LOGS", tmp_path / "nowhere")   # nothing else there
    call(client, "POST", "/api/devices/zipped/start", {})
    outdir = Path(call(client, "GET", "/api/runs")[1]["runs"][0]["outdir"])
    try:
        (outdir / "zipped.uart.log").write_text("[  100][I] hello\n")
        (outdir / "zipped.bus.sock").write_bytes(b"")
        (outdir / "frames").mkdir()
        (outdir / "frames" / "frame-001.png").write_bytes(b"\x89PNG")
        status, data, headers = call(client, "GET", "/api/devices/zipped/logs.zip")
        assert status == 200 and headers["Content-Type"] == "application/zip"
        assert f'"zipped-run-{outdir.name}.zip"' in headers["Content-Disposition"]
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        assert {"boardemul.txt", "device.json", "zipped.uart.log",
                "frames/frame-001.png"} <= names
        assert "zipped.bus.sock" not in names
        about = zipfile.ZipFile(io.BytesIO(data)).read("boardemul.txt").decode()
        assert f"BoardEmul {VERSION}" in about and "QEMU sources" in about

        call(client, "POST", "/api/devices/zipped/stop", {})
        monkeypatch.setattr(web, "LOGS", outdir.parent.parent.parent)
        status, data, _h = call(client, "GET", "/api/devices/zipped/logs.zip")
        assert status == 200
        assert "zipped.uart.log" in zipfile.ZipFile(io.BytesIO(data)).namelist()
    finally:
        shutil.rmtree(outdir.parent, ignore_errors=True)


def test_the_latest_run_is_the_highest_number(tmp_path):
    runs = tmp_path / "devices" / "node"
    for n in ("1", "2", "10", "notes"):
        (runs / n).mkdir(parents=True)
    assert web.latest_run_dir("node", tmp_path).name == "10"
    assert web.latest_run_dir("other", tmp_path) is None


def test_the_card_goes_out_and_comes_in(server):
    client, devices, _s = server
    _tdeck(client, devices)
    status, image, headers = call(client, "GET", "/api/devices/node/card")
    assert status == 200 and len(image) == 32 * 1024 * 1024
    assert 'filename="node-card.img"' in headers["Content-Disposition"]
    status, body, _h = call(client, "POST", "/api/devices/node/card",
                            b"\x5a" * len(image))
    assert status == 200 and body["snapshot"].startswith("before-card-")
    assert (devices / "node" / "sdcard.img").read_bytes()[:4] == b"\x5a" * 4
    status, body, _h = call(client, "POST", "/api/devices/node/card", b"too small")
    assert status == 400 and "bytes" in body["error"]


# -- a run that ends says why; the board's state is read where it is written --


def test_a_run_reports_its_end_and_its_last_words(server, monkeypatch):
    client, devices, _s = server
    _pocket(client, devices)
    call(client, "POST", "/api/devices/pocket/start")
    outdir = Path(web_state_run_outdirs(client)[0])
    (outdir / "console.log").write_text("starting\nAttributeError: no AF_UNIX\n")
    monkeypatch.setattr(device_folder.RunHandle, "alive", property(lambda self: False))
    status, body, _h = call(client, "GET", "/api/runs/pocket/console")
    assert status == 200 and body["alive"] is False
    assert body["lines"][-1] == "AttributeError: no AF_UNIX"


def test_the_keyboard_glow_is_read_beside_the_frames(server):
    """The board writes frames/board.json (ADR 0129); the page asked for a
    file that was never there, so the glow never showed in a browser."""
    client, devices, _s = server
    _pocket(client, devices)
    call(client, "POST", "/api/devices/pocket/start")
    outdir = Path(web_state_run_outdirs(client)[0])
    (outdir / "frames").mkdir(parents=True, exist_ok=True)
    (outdir / "frames" / "board.json").write_text('{"keyboard_backlight": 0.5}')
    status, body, _h = call(client, "GET", "/api/runs/pocket/state")
    assert status == 200 and body == {"keyboard_backlight": 0.5}


def test_the_page_can_ask_what_the_machine_lacks(server, monkeypatch):
    """ADR 0153: the doctor's own findings, so page and command line agree."""
    from tools import doctor
    monkeypatch.setattr(doctor, "diagnose", lambda: [
        doctor.Finding(doctor.ERROR, "Docker", "its daemon does not answer",
                       "start Docker Desktop"),
        doctor.Finding(doctor.OK, "Python", "3.12.3")])
    client, _devices, _s = server
    status, body, _h = call(client, "GET", "/api/doctor")
    assert status == 200
    assert body["findings"][0] == {"level": "error", "what": "Docker",
                                   "detail": "its daemon does not answer",
                                   "fix": "start Docker Desktop"}


def test_a_device_is_cloned_with_a_mac_of_its_own(server):
    """ADR 0155: one configured node becomes two."""
    client, devices, _s = server
    _pocket(client, devices)
    status, made, _h = call(client, "POST", "/api/devices/pocket/clone",
                            {"name": "pocket-2"})
    assert status == 201 and made["name"] == "pocket-2"
    assert made["cloned_from"] == "pocket"
    listed = call(client, "GET", "/api/devices")[1]["devices"]
    assert len({d["mac"] for d in listed}) == 2
    status, body, _h = call(client, "POST", "/api/devices/pocket/clone",
                            {"name": "pocket-2"})
    assert status == 400 and "already exists" in body["error"]
    call(client, "POST", "/api/devices/pocket/start", {})
    status, _b, _h = call(client, "POST", "/api/devices/pocket/clone",
                          {"name": "pocket-3"})
    assert status == 409


def test_the_console_reads_either_port_and_takes_typing(server):
    """ADR 0156: the USB port's log beside the UART's, and a serial event."""
    client, devices, _s = server
    _pocket(client, devices)
    call(client, "POST", "/api/devices/pocket/start", {})
    outdir = Path(web_state_run_outdirs(client)[0])
    (outdir / "pocket.uart.log").write_text("rom\n")
    (outdir / "pocket.usb.log").write_text("hwcdc says hi\n")
    assert call(client, "GET", "/api/runs/pocket/uart?port=usb")[1]["lines"] == ["hwcdc says hi"]
    assert call(client, "GET", "/api/runs/pocket/uart")[1]["lines"] == ["rom"]
    assert call(client, "GET", "/api/runs/pocket/uart?port=jtag")[0] == 400
    status, _b, _h = call(client, "POST", "/api/runs/pocket/input",
                          {"events": [{"type": "serial", "port": "usb", "text": "ls\n"}]})
    assert status == 200
    last = (outdir / "input.jsonl").read_text().splitlines()[-1]
    assert json.loads(last) == {"type": "serial", "port": "usb", "text": "ls\n"}


def test_a_device_is_made_from_the_firmware_library(server, monkeypatch, tmp_path):
    """The page names a pinned image; the server knows its file and board."""
    from tools import fetch_firmware
    image = tmp_path / "fw" / "mesh.bin"
    image.parent.mkdir()
    image.write_bytes(FIRMWARE_BYTES)
    monkeypatch.setattr(fetch_firmware, "FIRMWARE", image.parent)
    monkeypatch.setattr(fetch_firmware, "load", lambda *_a: [
        {"name": "mesh", "board": "tdeck", "path": "mesh.bin", "role": "A node. More."},
        {"name": "fs", "path": "fs.bin"}])
    monkeypatch.setattr(fetch_firmware, "state",
                        lambda e, root=None: "ok" if e["name"] == "mesh" else "missing")
    client, _devices, _s = server
    status, body, _h = call(client, "GET", "/api/firmware")
    assert body["images"] == [{"name": "mesh", "board": "tdeck", "role": "A node. More.",
                               "file": "mesh.bin", "present": True}]
    status, made, _h = call(client, "POST", "/api/devices", {"name": "node", "image": "mesh"})
    assert status == 201 and made["board"] == "tdeck"
    status, body, _h = call(client, "POST", "/api/devices", {"name": "x", "image": "fs"})
    assert status == 400
