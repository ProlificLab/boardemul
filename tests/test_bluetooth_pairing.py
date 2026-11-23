#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Meshtastic over Bluetooth on a PIN, as a phone pairs with it (ADR 0171).

A node set to a fixed PIN through its serial API (the USB OTG port, ADR
0166) restarts; a central connects, pairs by LE Secure Connections passkey
entry with that PIN, starts the link's encryption, and reads the node's
configuration -- every packet after the start encrypted on the air and
checked by its MIC, the controller's session key computed on the core's own
AES unit. The node's answer is its own: its node number, and the stream
closed with the nonce asked with.

    python3 -m pytest tests/test_bluetooth_pairing.py -m slow
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, qemu_available, run_emulator,
)
from tests.test_bluetooth_connect import OVERLAY_AT, bluetooth_filesystem  # noqa: E402,F401
from tests.test_bluetooth_meshtastic import _boots, _free_port  # noqa: E402
from tools import ble_central, mesh_api  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

NONCE = 4343
PIN = 314159


@pytest.fixture(scope="module")
def paired(tmp_path_factory, bluetooth_filesystem: Path) -> tuple[Run, dict]:
    port = _free_port()
    outdir = tmp_path_factory.mktemp("btpair")
    air = outdir / "air"
    air.mkdir()
    seen: dict = {"messages": 0, "errors": []}

    def drive(outdir: Path) -> None:
        deadline = time.monotonic() + 1500
        while _boots(outdir) < 1 and time.monotonic() < deadline:
            time.sleep(2)
        while "pin_sent" not in seen and time.monotonic() < deadline:
            time.sleep(10)
            try:
                node = mesh_api.Node(f"127.0.0.1:{port}", timeout=120)
                state = node.configuration()
                if "my_node_num" in state:
                    seen["serial_node"] = state["my_node_num"]
                    node.send(mesh_api.set_pairing_mode(
                        state["my_node_num"], mesh_api.PAIRING_MODES["fixed-pin"],
                        state.get("raw_bluetooth", b""), pin=PIN))
                    seen["pin_sent"] = True
                node.sock.close()
            except OSError:
                pass
        while _boots(outdir) < 2 and time.monotonic() < deadline:
            time.sleep(2)
        central = ble_central.Central(air)
        try:
            for _ in range(3):
                if not central.connect(wait_s=max(1, deadline - time.monotonic())):
                    # Once in a run nothing was heard and nothing said why.
                    seen["errors"].append(
                        f"no advertisement heard; {_boots(outdir)} boot(s), "
                        f"{deadline - time.monotonic():.0f} s of the deadline left")
                    return
                try:
                    central.exchange_mtu()
                    services = central.services()
                    mesh = next(s for s in services
                                if s[2] == ble_central.MESHTASTIC_SERVICE)
                    chars = central.characteristics(mesh[0], mesh[1])
                    toradio = chars[ble_central.TORADIO][0]
                    # Unpaired, the node refuses, as it refuses a phone.
                    try:
                        central.write(toradio, mesh_api.want_config(NONCE))
                    except ble_central.AttError as error:
                        seen["unpaired_refusal"] = error.code
                    ltk = central.pair(PIN)
                    seen["paired"] = True
                    central.encrypt(ltk)
                    seen["encrypted"] = True
                    # FromNum, as a phone subscribes to it: the node's way of
                    # saying FromRadio has something new.
                    fromnum = chars[ble_central.FROMNUM][0]
                    central.subscribe(fromnum, mesh[1])
                    seen["subscribed"] = True
                    central.write(toradio, mesh_api.want_config(NONCE))
                    fromradio = chars[ble_central.FROMRADIO][0]
                    for _ in range(300):
                        value = central.read(fromradio)
                        if not value:
                            break
                        seen["messages"] += 1
                        try:
                            described = mesh_api.describe(value)
                        except ValueError as error:      # a message that came in wrong
                            seen["errors"].append(f"{error}: {value.hex()}")
                            return
                        seen.update({k: v for k, v in described.items()
                                     if not k.startswith("raw_")})
                    # Something new, unasked for by a read: the node's answer
                    # to an admin request, announced on FromNum, then read.
                    central.notifications.clear()
                    central.write(toradio, mesh_api.get_owner(seen["my_node_num"]))
                    seen["notified"] = central.notified(fromnum, wait_s=120) is not None
                    for _ in range(20):
                        value = central.read(fromradio)
                        if not value:
                            break
                        seen["owner"] = mesh_api.owner(value) or seen.get("owner")
                    return
                except (ble_central.LinkLost, TimeoutError,
                        ble_central.PairingError) as error:
                    seen["errors"].append(repr(error))
                    continue
        finally:
            seen["sent_encrypted"] = central.tx_count
            seen["heard_encrypted"] = central.rx_count
            seen["mic_failures"] = central.mic_failures
            central.close()

    run = run_emulator(outdir, firmware=FIRMWARE_MESHTASTIC, board="tdeck",
                       timeout=2000,
                       overlays=(f"0x{OVERLAY_AT:x}={bluetooth_filesystem}",),
                       extra=("--psram-octal", "--icount", "--reboot",
                              "--ble-air", str(air), "--serial-tcp", f"{port}:otg"),
                       script=drive)
    return run, seen


def test_the_pin_is_set_through_the_serial_api(paired) -> None:
    run, seen = paired
    assert seen.get("pin_sent"), seen
    assert _boots(run.outdir) >= 2, "the node did not restart on the setting"


def test_unpaired_the_node_refuses_to_be_written(paired) -> None:
    _, seen = paired
    assert seen.get("unpaired_refusal") == 0x05, seen      # insufficient authentication


def test_the_central_pairs_with_the_pin(paired) -> None:
    _, seen = paired
    assert seen.get("paired"), seen


def test_the_link_is_encrypted_both_ways(paired) -> None:
    _, seen = paired
    assert seen.get("encrypted"), seen
    assert seen["sent_encrypted"] > 0 and seen["heard_encrypted"] > 0, seen
    assert seen["mic_failures"] == 0, seen


def test_the_node_streams_its_configuration_encrypted(paired) -> None:
    _, seen = paired
    assert seen.get("config_complete_id") == NONCE, seen
    assert seen["messages"] > 10, seen
    assert seen.get("my_node_num") == seen.get("serial_node"), seen


def test_fromnum_announces_what_the_node_has_for_its_client(paired) -> None:
    """A phone reads FromRadio when FromNum says there is something: the
    node's answer to an owner request is announced, then read (ADR 0171)."""
    _, seen = paired
    assert seen.get("subscribed"), seen
    assert seen.get("notified"), seen
    assert seen.get("owner"), seen


def test_no_packet_failed_its_mic_in_the_core(paired) -> None:
    run, _ = paired
    log = run.outdir / "t.qemu.log"
    text = log.read_text(errors="replace") if log.is_file() else ""
    assert "MIC failed" not in text
