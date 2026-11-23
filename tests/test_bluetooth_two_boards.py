#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Two boards on one Bluetooth air, and a central that connects to one
(ADR 0173).

Every frame reaches every radio on an air, and both boards advertise on the
same access address. The central hears both, names one in its connection
request, and speaks ATT with it. The other must not take the request -- its
controller would; the radio's address filter is what stops it -- and goes on
advertising, its link layer never seeing the connection's packets.

    python3 -m pytest tests/test_bluetooth_two_boards.py -m slow
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, REPO_ROOT, qemu_available,
)
from tests.test_bluetooth_connect import OVERLAY_AT, bluetooth_filesystem  # noqa: E402,F401
from tools import ble_central  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

RUN_SECONDS = 900
# Two boards, two MACs: their Bluetooth addresses follow (the chip's BT MAC
# is derived from the base one).
BOARDS = {"a": "02:54:44:4D:00:11", "b": "02:54:44:4D:00:21"}


@pytest.fixture(scope="module")
def two(tmp_path_factory, bluetooth_filesystem: Path) -> dict:
    work = tmp_path_factory.mktemp("bletwo")
    air = work / "air"
    air.mkdir()
    procs = {}
    for tag, mac in BOARDS.items():
        (work / tag / "build").mkdir(parents=True)
        procs[tag] = subprocess.Popen(
            [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
             "--board", "tdeck", "--bus", "--psram-octal", "--icount", "--mac", mac,
             "--ble-air", str(air), "--builddir", str(work / tag / "build"),
             "--overlay", f"0x{OVERLAY_AT:x}={bluetooth_filesystem}",
             "--outdir", str(work / tag), "--tag", tag,
             "--timeout", str(RUN_SECONDS), "--stall", str(RUN_SECONDS)],
            cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    seen: dict = {"work": work}
    central = ble_central.Central(air)
    try:
        # Both advertise: two addresses heard.
        heard: dict[bytes, bool] = {}
        deadline = time.monotonic() + RUN_SECONDS - 120
        while time.monotonic() < deadline and len(heard) < 2:
            for aa, pdu in map(ble_central.split, central.air.poll()):
                if aa == ble_central.ADV_AA and len(pdu) >= 7 and not pdu[0] & 0x0F:
                    heard[pdu[1:7]] = True
            time.sleep(0.5)
        seen["addresses"] = sorted(heard)
        if len(heard) < 2:
            return seen
        target = seen["addresses"][0]
        seen["target"] = target
        if not central.connect(wait_s=120, address=target):
            return seen
        try:
            seen["mtu"] = central.exchange_mtu()
            seen["services"] = [s[2] for s in central.services()]
        except (ble_central.LinkLost, TimeoutError, ble_central.AttError) as error:
            seen["error"] = repr(error)
        # The other goes on advertising while the connection lives.
        others = set()
        end = time.monotonic() + 30
        while time.monotonic() < end:
            for aa, pdu in map(ble_central.split, central.air.poll()):
                if aa == ble_central.ADV_AA and len(pdu) >= 7:
                    others.add(pdu[1:7])
            central.step()
        seen["advertising_meanwhile"] = sorted(others)
    finally:
        central.close()
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
    return seen


def _log(seen: dict, tag: str) -> str:
    path = seen["work"] / tag / f"{tag}.qemu.log"
    return path.read_text(errors="replace") if path.exists() else ""


def test_both_boards_are_heard(two) -> None:
    assert len(two.get("addresses", [])) == 2, two


def test_the_central_speaks_with_the_one_it_named(two) -> None:
    assert two.get("mtu") and "6ba1b218-15a8-461f-9fa8-5dcae273eafd" in two.get(
        "services", []), two


def test_only_the_named_board_took_the_connection(two) -> None:
    connected = [tag for tag in BOARDS if "link 1 sent" in _log(two, tag)]
    assert len(connected) == 1, f"boards with a connection: {connected}"
    other = next(tag for tag in BOARDS if tag not in connected)
    assert "request for another advertiser; not taken" in _log(two, other), (
        "the other board was never offered the request, so this proves nothing")


def test_the_other_board_goes_on_advertising(two) -> None:
    target = two.get("target")
    assert any(a != target for a in two.get("advertising_meanwhile", [])), two
