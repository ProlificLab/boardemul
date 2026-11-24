# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""WiFi in the test library and in scenarios (ADR 0182, amended): the
network a device joins, and a server the firmware runs, reached through a
forwarded port."""

from __future__ import annotations

import http.server
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import WiFi  # noqa: E402
from boardemul import scenario  # noqa: E402
from boardemul.lab import Device, LabError, Timeout  # noqa: E402
from boardemul.scenario import ScenarioError, parse  # noqa: E402
from emu import device as device_folder  # noqa: E402


def test_a_network_is_the_run_settings_the_page_takes():
    wifi = WiFi("lab", password="correct horse", uplink=True, forward={8080: 80})
    assert wifi.args() == ["--wifi-ap", "lab", "--wifi-wpa2", "--wifi-pass",
                           "correct horse", "--wifi-uplink", "--wifi-forward", "8080:80"]
    assert WiFi("open").args() == ["--wifi-ap", "open"]
    # What the page holds every setting to accepts them (ADR 0145).
    assert device_folder.check_settings(wifi.args())


@pytest.mark.parametrize("kwargs", [
    {"name": ""}, {"name": "a,b"}, {"name": "x", "password": "short"},
    {"name": "x", "forward": {80: 80}}, {"name": "x", "forward": {8080: 0}},
])
def test_a_wrong_network_is_refused(kwargs):
    with pytest.raises(ValueError):
        WiFi(**kwargs)


class _Api:
    def __init__(self):
        self.calls = []

    def ok(self, method, path, body=None, raw=None):
        self.calls.append((method, path, body))
        return {}


class _Lab:
    def __init__(self):
        self.api = _Api()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def firmware_server():
    """What a firmware's web server would answer, on a port of the host."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/status":
                body = b'{"mesh": "up"}'
                self.send_response(200)
            else:
                body = b"no"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", _free_port()), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def test_a_started_device_is_reached_through_its_forwarded_port(firmware_server):
    lab = _Lab()
    node = Device(lab, "node")
    node.start(wifi=WiFi("lab", forward={firmware_server: 80}))
    assert lab.api.calls[0][2]["args"] == ["--wifi-ap", "lab", "--wifi-forward",
                                           f"{firmware_server}:80"]
    assert node.http("/status") == (200, b'{"mesh": "up"}')
    assert node.http("/absent", expect_status=None)[0] == 404
    with pytest.raises(Timeout, match="status 404"):
        node.http("/absent", timeout=0)
    with pytest.raises(LabError, match="no host port reaches its port 443"):
        node.http("/", port=443)


def test_a_scenario_gives_a_device_a_network_and_asks_its_server(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "firmware.bin").write_bytes(b"app")
    text = ('[[device]]\nbuild = "build"\n'
            '[device.wifi]\nname = "lab"\npassword = "correct horse"\n'
            'forward = [[8080, 80]]\n'
            '[[step]]\nhttp = "/status"\nbody = "mesh.*up"\ntimeout = 5\n')
    s = parse(text, tmp_path / "s.toml")
    assert s.devices[0].wifi == WiFi("lab", "correct horse", False, {8080: 80})

    class Node:
        name = "dut"

        def __init__(self):
            self.started = None

        def start(self, air=None, wifi=None):
            self.started = wifi

        def http(self, path, port, timeout, status):
            return 200, b'{"mesh": "up"}'

        def stop(self):
            pass

        def save_logs(self, path):
            return path

    class Lab:
        node = Node()

        def device(self, name, build, board="tdeck", settings=()):
            return self.node

    said = []
    lab = Lab()
    assert scenario.run(s, lab, out=said.append)
    assert lab.node.started == s.devices[0].wifi
    assert said[-1].startswith("ok   step 1, dut: http /status")


@pytest.mark.parametrize("wifi, says", [
    ('[device.wifi]\nname = "x"\nchannel = 6\n', "'channel'"),
    ('[device.wifi]\nname = "x"\nforward = [8080, 80]\n', "[[HOSTPORT, DEVICEPORT]"),
    ('[device.wifi]\nname = "x"\npassword = "short"\n', "8 to 63"),
])
def test_a_wrong_network_in_a_scenario_is_refused(tmp_path, wifi, says):
    (tmp_path / "build").mkdir()
    with pytest.raises(ScenarioError, match=None) as caught:
        parse('[[device]]\nbuild = "build"\n' + wifi + '[[step]]\nwait = 1\n',
              tmp_path / "s.toml")
    assert says in str(caught.value)
