#!/usr/bin/env python3
"""A phone's view of Meshtastic over Bluetooth (ADRs 0157, 0168, 0169).

A Meshtastic node with Bluetooth on is set to pair without a PIN through its
own serial API (the USB OTG port, ADR 0166), as its apps' Bluetooth page
does, and restarts. A central then does what a phone app does: connects,
finds Meshtastic's service and characteristics, writes ToRadio asking for the
configuration, and reads FromRadio until it is empty. The node's answer is
its own: its node number, and the configuration stream closed with the
nonce the central asked with.

    python3 -m pytest tests/test_bluetooth_meshtastic.py -m slow
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, qemu_available, run_emulator,
)
from tests.test_bluetooth_connect import OVERLAY_AT, bluetooth_filesystem  # noqa: E402,F401
from tools import ble_central, mesh_api  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

NONCE = 4242


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _boots(outdir: Path) -> int:
    path = outdir / "t.otg.log"
    text = path.read_text(errors="replace") if path.is_file() else ""
    return text.count("Booted")


@pytest.fixture(scope="module")
def phone(tmp_path_factory, bluetooth_filesystem: Path) -> tuple[Run, dict]:
    port = _free_port()
    outdir = tmp_path_factory.mktemp("btmesh")
    air = outdir / "air"
    air.mkdir()
    seen: dict = {"messages": 0}

    def drive(outdir: Path) -> None:
        deadline = time.monotonic() + 900
        while _boots(outdir) < 1 and time.monotonic() < deadline:
            time.sleep(2)
        # The pairing mode, through the node's own API; it saves and restarts.
        while "pairing_sent" not in seen and time.monotonic() < deadline:
            time.sleep(10)
            try:
                node = mesh_api.Node(f"127.0.0.1:{port}", timeout=120)
                state = node.configuration()
                if "my_node_num" in state:
                    seen["serial_node"] = state["my_node_num"]
                    node.send(mesh_api.set_pairing_mode(
                        state["my_node_num"], mesh_api.PAIRING_MODES["no-pin"],
                        state.get("raw_bluetooth", b"")))
                    seen["pairing_sent"] = True
                node.sock.close()
            except OSError:
                pass
        while _boots(outdir) < 2 and time.monotonic() < deadline:
            time.sleep(2)
        central = ble_central.Central(air)
        try:
            for _ in range(3):
                if not central.connect(wait_s=max(1, deadline - time.monotonic())):
                    return
                try:
                    central.exchange_mtu()
                    services = central.services()
                    mesh = next(s for s in services
                                if s[2] == ble_central.MESHTASTIC_SERVICE)
                    chars = central.characteristics(mesh[0], mesh[1])
                    central.write(chars[ble_central.TORADIO][0],
                                  mesh_api.want_config(NONCE))
                    for _ in range(300):
                        value = central.read(chars[ble_central.FROMRADIO][0])
                        if not value:
                            break
                        seen["messages"] += 1
                        seen.update({k: v for k, v in mesh_api.describe(value).items()
                                     if not k.startswith("raw_")})
                    return
                except (ble_central.LinkLost, TimeoutError):
                    continue
        finally:
            central.close()

    run = run_emulator(outdir, firmware=FIRMWARE_MESHTASTIC, board="tdeck",
                       timeout=1500,
                       overlays=(f"0x{OVERLAY_AT:x}={bluetooth_filesystem}",),
                       extra=("--psram-octal", "--icount", "--reboot",
                              "--ble-air", str(air), "--serial-tcp", f"{port}:otg"),
                       script=drive)
    return run, seen


def test_the_pairing_mode_is_set_through_the_serial_api(phone) -> None:
    run, seen = phone
    assert seen.get("pairing_sent"), seen
    assert _boots(run.outdir) >= 2, "the node did not restart on the setting"


def test_the_node_streams_its_configuration_over_bluetooth(phone) -> None:
    _, seen = phone
    assert seen.get("config_complete_id") == NONCE, seen
    assert seen["messages"] > 10, seen


def test_the_node_over_bluetooth_is_the_node_on_the_cable(phone) -> None:
    _, seen = phone
    assert seen.get("my_node_num") and seen.get("my_node_num") == seen.get("serial_node"), seen
