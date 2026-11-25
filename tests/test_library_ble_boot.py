#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Bluetooth through the test library, end to end (ADR 0188).

Slow: a Meshtastic node made and started by a Lab with `ble=True`, set to
pair without a PIN through its serial API (as tests/test_bluetooth_meshtastic.py
does), then asked over Bluetooth with `Device.ble()`: its services, and
Meshtastic's own API -- ToRadio written, FromRadio read -- which answers
with the node's number, the one its serial API gave.

    python3 -m pytest tests/test_library_ble_boot.py -m slow
"""

from __future__ import annotations

import shutil
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import Lab  # noqa: E402
from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, qemu_available,
)
from tests.test_bluetooth_connect import OVERLAY_AT, bluetooth_filesystem  # noqa: E402,F401
from tools import ble_central, mesh_api  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

NONCE = 4242


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _entry(kind: int, subtype: int, offset: int, size: int, label: bytes) -> bytes:
    return struct.pack("<2sBBII16sI", b"\xaa\x50", kind, subtype, offset, size,
                       label.ljust(16, b"\0"), 0)


def _build(folder: Path, filesystem: Path) -> Path:
    """Meshtastic with a filesystem whose Bluetooth is on (the factory
    one's is not), placed where Meshtastic's own goes."""
    assert OVERLAY_AT == 0xC90000
    folder.mkdir(parents=True)
    shutil.copy(FIRMWARE_MESHTASTIC, folder / "firmware.bin")
    shutil.copy(filesystem, folder / "littlefs.bin")
    (folder / "partitions.bin").write_bytes(
        _entry(1, 0x02, 0x9000, 0x5000, b"nvs")
        + _entry(0, 0x10, 0x10000, 0x640000, b"app0")
        + _entry(1, 0x82, 0xC90000, 0x370000, b"spiffs")
        + b"\xff" * 32)
    return folder


def _pair_without_pin(port: int) -> int:
    """Through the node's serial API, as its apps' Bluetooth page does; the
    node saves and restarts. Its number."""
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            node = mesh_api.Node(f"127.0.0.1:{port}", timeout=120)
            state = node.configuration()
            if "my_node_num" in state:
                node.send(mesh_api.set_pairing_mode(
                    state["my_node_num"], mesh_api.PAIRING_MODES["no-pin"],
                    state.get("raw_bluetooth", b"")))
                node.sock.close()
                return state["my_node_num"]
            node.sock.close()
        except OSError:
            pass
        time.sleep(10)
    raise AssertionError("the node never answered on its serial API")


def test_a_library_device_answers_over_bluetooth(tmp_path, bluetooth_filesystem) -> None:
    port = _free_port()
    with Lab() as lab:
        node = lab.device("blenode", build=_build(tmp_path / "pio", bluetooth_filesystem),
                          board="tdeck",
                          settings=("--serial-tcp", f"{port}:otg"))
        node.start(ble=True)
        node.serial("otg").expect(r"SX126x init result 0", timeout=600)
        number = _pair_without_pin(port)
        node.serial("otg").expect(r"Booted, wake cause", timeout=600)
        node.serial("otg").expect(r"SX126x init result 0", timeout=600)

        assert ble_central.MESHTASTIC_SERVICE in node.ble_services()
        reads = [{"op": "read", "uuid": ble_central.FROMRADIO}] * 60
        results = node.ble({"op": "write", "uuid": ble_central.TORADIO,
                            "hex": mesh_api.want_config(NONCE).hex()}, *reads)
        seen: dict = {}
        for result in results[1:]:
            if result["hex"]:
                seen.update(mesh_api.describe(bytes.fromhex(result["hex"])))
        node.save_logs(tmp_path / "blenode.zip")
        assert seen.get("my_node_num") == number, sorted(seen)
