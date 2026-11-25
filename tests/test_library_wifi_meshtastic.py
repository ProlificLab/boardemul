#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Meshtastic's own network API through the test library, end to end
(ADRs 0164, 0182).

Slow: a Meshtastic node made and started by a Lab with `WiFi(...)`, told
through its serial API (tools/mesh_api.py, as the apps do) its region, the
network to join, and the classic screen -- the colour interface starts no
server on WiFi -- then asked, through the host port the library forwards to
its TCP API on 4403, for its configuration, and through another to its web
server on 80, for its page: the same node, on that network,
on the classic screen.

    python3 -m pytest tests/test_library_wifi_meshtastic.py -m slow
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import Lab, WiFi  # noqa: E402
from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, qemu_available,
)
from tests.test_library_ble_boot import _build  # noqa: E402
from tools import mesh_api  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

API_PORT = 4403


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _change(node, port: int, *command: str) -> None:
    """One change through the node's serial API, then its restart. The node
    answers "Reboot in 7 seconds" at once, so the next change waits for it
    to have booted again: two changes inside those seconds kept only the
    last."""
    deadline = time.monotonic() + 600
    while True:
        try:
            if mesh_api.main(["--timeout", "120", f"127.0.0.1:{port}", *command]) == 0:
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"the node never took {command}")
        time.sleep(10)
    node.serial("otg").expect(r"Booted, wake cause", timeout=600)


def _configuration(address: str) -> dict:
    deadline = time.monotonic() + 600
    while True:
        try:
            api = mesh_api.Node(address, timeout=120)
            try:
                state = api.configuration()
            finally:
                api.sock.close()
            if "my_node_num" in state:
                return state
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"nothing answered on {address}")
        time.sleep(10)


def test_meshtastic_answers_on_its_network_api_through_the_library(tmp_path) -> None:
    serial_port, host_port, web_port = _free_port(), _free_port(), _free_port()
    wifi = WiFi("boardemul-lab", password="correct horse battery",
                forward={host_port: API_PORT, web_port: 80})
    with Lab() as lab:
        node = lab.device("meshwifi", build=_build(tmp_path / "pio", MESHTASTIC_LITTLEFS),
                          board="tdeck", settings=("--serial-tcp", f"{serial_port}:otg"))
        node.start(wifi=wifi)
        node.serial("otg").expect(r"SX126x init result 0", timeout=600)
        _change(node, serial_port, "region", "us")
        _change(node, serial_port, "wifi", wifi.name, wifi.password)
        # The colour interface reads its configuration after each boot and
        # writes its display setting back when it has: a classic screen set
        # before that was colour again on the next boot. So after it, and
        # after the join, as a person would set it.
        node.serial("otg").expect(r"\[PacketAPI\].*Send config: display", timeout=600)
        node.serial("otg").expect(r"Obtained IP address", timeout=600)
        _change(node, serial_port, "display-mode", "default")
        try:
            node.serial("otg").expect(r"API server listen on TCP port 4403", timeout=1800)
            over_serial = _configuration(f"127.0.0.1:{serial_port}")
            over_wifi = _configuration(f"127.0.0.1:{host_port}")
            page_status, _page = node.http("/", port=80, timeout=300, expect_status=None)
        finally:
            node.save_logs(tmp_path / "meshwifi.zip")
        assert over_wifi["my_node_num"] == over_serial["my_node_num"]
        assert over_wifi["network"]["wifi_ssid"] == wifi.name
        assert over_wifi["display_mode"] == "default", over_wifi.get("display_mode")
        assert page_status == 200, page_status
