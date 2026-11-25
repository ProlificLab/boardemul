# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Bluetooth through the library (ADR 0188): the central's operations, the
command that places it beside the run, the page's route, and the calls."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul.lab import Device, LabError, _air_name  # noqa: E402
from emu import device as device_folder  # noqa: E402
from tools import ble_central as bc  # noqa: E402
from tests.test_web import _pocket, call, server  # noqa: E402,F401

SERVICE = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO = "f75c76d2-129e-4dad-a1dd-7866124401e7"


class FakeCentral:
    def __init__(self):
        self.written = []
        self.paired = None

    def services(self):
        return [(0x0001, 0x0009, "1800"), (0x000a, 0x0014, SERVICE)]

    def characteristics(self, first, last):
        if first == 0x000a:
            return {TORADIO: (0x000c, 0x08), "2c55e69e-4993-11ed-b878-0242ac120002": (0x000e, 0x02)}
        return {"2a00": (0x0003, 0x02)}

    def read(self, handle):
        return {0x000e: b"\x01\x02", 0x0003: b"node"}[handle]

    def write(self, handle, value):
        self.written.append((handle, value))

    def pair(self, passkey):
        self.paired = passkey
        return b"k" * 16

    def encrypt(self, ltk):
        pass


def test_the_central_does_each_operation_in_order():
    central = FakeCentral()
    results = bc.run_ops(central, bc.check_ops([
        {"op": "pair", "passkey": 123456},
        {"op": "services"},
        {"op": "characteristics", "uuid": SERVICE.upper()},
        {"op": "write", "uuid": TORADIO, "hex": "0a0b"},
        {"op": "read", "uuid": "2A00"},
    ]))
    assert central.paired == 123456 and results[0] == {"op": "pair", "encrypted": True}
    assert [s["uuid"] for s in results[1]["services"]] == ["1800", SERVICE]
    assert {c["uuid"] for c in results[2]["characteristics"]} >= {TORADIO}
    assert central.written == [(0x000c, b"\x0a\x0b")]
    assert results[4]["hex"] == b"node".hex()
    with pytest.raises(bc.OpError, match="no characteristic"):
        bc.run_ops(central, [{"op": "read", "uuid": "ffff"}])


@pytest.mark.parametrize("ops, says", [
    (None, "a list"), ([], "a list"), ([{"op": "dance"}], "op is one of"),
    ([{"op": "read"}], "needs a uuid"), ([{"op": "write", "uuid": "x", "hex": "zz"}], "needs hex"),
    ([{"op": "pair", "passkey": 10 ** 7}], "passkey"),
])
def test_a_wrong_operation_is_refused_before_connecting(ops, says):
    with pytest.raises(bc.OpError, match=says):
        bc.check_ops(ops)


def test_the_central_goes_where_the_run_is(tmp_path, monkeypatch):
    root = tmp_path / "devices" / "node"
    monkeypatch.setattr(device_folder, "runs_natively", lambda: True)
    native = device_folder.central_command(root, "node-ble", "[]")
    assert native[-4:] == [f"{device_folder.local_air_base(root)}/ble/node-ble", "ops",
                           "--ops", "[]"]
    monkeypatch.setattr(device_folder, "runs_natively", lambda: False)
    boxed = device_folder.central_command(root, "node-ble", "[]")
    assert boxed[:2] == ["docker", "run"]
    assert f"{device_folder.AIR_VOLUME}:{device_folder.AIR_MOUNT}" in boxed
    assert boxed[-4:] == ["/air/ble/node-ble", "ops", "--ops", "[]"]
    assert device_folder.ble_air(("--reboot", "--ble-air", "x")) == "x"
    assert device_folder.ble_air(("--reboot",)) is None


def test_the_page_runs_the_central_for_a_device_on_a_bluetooth_air(server, monkeypatch):
    client, devices, _started = server
    _pocket(client, devices)
    ran = []

    def fake_run(cmd, capture_output, text, timeout):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            {"ok": True, "results": [{"op": "services", "services": []}]}) + "\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(device_folder, "runs_natively", lambda: True)

    status, body, _h = call(client, "POST", "/api/runs/pocket/ble", {"ops": [{"op": "services"}]})
    assert status == 404                                    # not running
    call(client, "POST", "/api/devices/pocket/start", {})
    status, body, _h = call(client, "POST", "/api/runs/pocket/ble", {"ops": [{"op": "services"}]})
    assert status == 400 and "--ble-air" in body["error"]   # running, no air
    call(client, "POST", "/api/devices/pocket/stop", {})
    call(client, "POST", "/api/devices/pocket/start", {"args": ["--ble-air", "pocket-ble"]})
    status, body, _h = call(client, "POST", "/api/runs/pocket/ble", {"ops": [{"op": "dance"}]})
    assert status == 400 and "op is one of" in body["error"]
    status, body, _h = call(client, "POST", "/api/runs/pocket/ble", {"ops": [{"op": "services"}]})
    assert status == 200 and body["ok"] and body["results"][0]["op"] == "services"
    assert ran[-1][-4].endswith("/ble/pocket-ble") and ran[-1][-3] == "ops"


class _Api:
    def __init__(self, reply, status=200):
        self.reply, self.status, self.calls = reply, status, []

    def ok(self, method, path, body=None, raw=None):
        self.calls.append(body)
        return {}

    def call(self, method, path, body=None, raw=None, timeout=60.0):
        self.calls.append(body)
        return self.status, self.reply, {}


class _Lab:
    def __init__(self, api):
        self.api = api


def test_the_library_starts_on_an_air_and_asks_the_page():
    api = _Api({"ok": True, "results": [{"op": "pair", "encrypted": True},
                                        {"op": "read", "uuid": "2a00", "hex": "6869"}]})
    node = Device(_Lab(api), "Node 1")
    node.start(ble=True)
    assert api.calls[0]["args"] == ["--ble-air", "node-1-ble"]
    assert node.ble_read("2a00", passkey=123456) == b"hi"
    assert api.calls[1]["ops"] == [{"op": "pair", "passkey": 123456},
                                   {"op": "read", "uuid": "2a00"}]
    refused = Device(_Lab(_Api({"ok": False, "error": "nothing advertised on the air"}, 422)), "x")
    with pytest.raises(LabError, match="nothing advertised"):
        refused.ble_services()
    assert _air_name("A Very Long Name With Spaces And More Words") .endswith("-ble")
