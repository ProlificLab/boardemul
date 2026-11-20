#!/usr/bin/env python3
"""The Bluetooth link carries control procedures both ways (ADR 0168).

A central connects, as tests/test_bluetooth_connect.py's does, and then does
what a central's link layer does: it answers the peripheral's feature request
with its own features, and asks for the peripheral's version. What comes back
is the controller's, not this project's: the feature request the peripheral
opened the link with, and an LL_VERSION_IND carrying Espressif's company
identifier, which only the ROM's link layer knows to send.

    python3 -m pytest tests/test_bluetooth_link_layer.py -m slow
"""

from __future__ import annotations

import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.air import AirMedium  # noqa: E402
from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, REPO_ROOT, qemu_available,
)
from tests.test_bluetooth_connect import (  # noqa: E402,F401
    ADVERTISING_SECONDS, OVERLAY_AT, advertiser, bluetooth_filesystem, connect_request,
)
from tools.ble_central import CONN_AA, on, split  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

RUN_SECONDS = 480
EXCHANGE_SECONDS = 180
LLID_EMPTY, LLID_CONTROL = 1, 3
LL_FEATURE_RSP, LL_VERSION_IND = 0x09, 0x0C
LL_PERIPHERAL_FEATURE_REQ, LL_FEATURE_REQ = 0x0E, 0x08
ESPRESSIF = 0x02E5          # Bluetooth SIG company identifier


class Central:
    """A central's link layer, as far as these procedures go: sequence bits
    on what it sends, and answers to what the peripheral asks."""

    def __init__(self, air: AirMedium):
        self.air = air
        self.sn = self.nesn = 0
        self.queue: list[tuple[int, bytes]] = []
        self.control: list[bytes] = []          # every control PDU heard

    def send(self, llid: int, payload: bytes = b"") -> None:
        self.air.send(on(CONN_AA, bytes([llid | self.nesn << 2 | self.sn << 3]) + payload))
        self.sn ^= 1

    def listen(self) -> None:
        for aa, frame in map(split, self.air.poll()):
            if aa != CONN_AA:                # advertising, or another link's
                continue
            llid = frame[0] & 3
            self.nesn ^= 1
            body = frame[1:]
            if llid == LLID_CONTROL and body:
                self.control.append(body)
                if body[0] in (LL_PERIPHERAL_FEATURE_REQ, LL_FEATURE_REQ):
                    self.queue.append((LLID_CONTROL, bytes([LL_FEATURE_RSP]) + bytes(8)))

    def step(self) -> None:
        self.listen()
        self.send(*(self.queue.pop(0) if self.queue else (LLID_EMPTY,)))

    def opcodes(self) -> list[int]:
        return [body[0] for body in self.control]


@pytest.fixture(scope="module")
def exchanged(tmp_path_factory, bluetooth_filesystem: Path) -> list[bytes]:
    work = tmp_path_factory.mktemp("bleln")
    air = work / "air"
    air.mkdir()
    (work / "build").mkdir()
    proc = subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal", "--icount",
         "--ble-air", str(air), "--builddir", str(work / "build"),
         "--overlay", f"0x{OVERLAY_AT:x}={bluetooth_filesystem}",
         "--outdir", str(work), "--tag", "ll",
         "--timeout", str(RUN_SECONDS), "--stall", str(RUN_SECONDS)],
        cwd=REPO_ROOT, stdout=(work / "run.log").open("w"), stderr=subprocess.STDOUT)
    medium = AirMedium(air, "central")
    central = Central(medium)
    try:
        started = time.monotonic()
        deadline = started + ADVERTISING_SECONDS
        address = None
        while time.monotonic() < deadline and address is None:
            address = advertiser(medium.poll())
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        # Said here rather than left to the tests: without an advertiser
        # there is nobody to connect to, and an empty exchange says nothing
        # of why (the run's own log is beside it, in run.log).
        if address is None:
            pytest.fail(f"no advertising in {time.monotonic() - started:.0f} s; "
                        f"the run {'ended' if proc.poll() is not None else 'was still going'}"
                        f" ({work})")
        medium.send(connect_request(address))
        asked = False
        deadline = time.monotonic() + EXCHANGE_SECONDS
        # Stopped on the evidence: the peripheral's version, once asked for.
        while time.monotonic() < deadline and proc.poll() is None:
            central.step()
            if not asked and LL_FEATURE_RSP not in [p for p, _ in central.queue] \
                    and LL_PERIPHERAL_FEATURE_REQ in central.opcodes():
                central.queue.append((LLID_CONTROL, bytes([LL_VERSION_IND, 0x09])
                                      + struct.pack("<HH", 0x0059, 1)))
                asked = True
            if LL_VERSION_IND in central.opcodes():
                break
            time.sleep(0.03)
    finally:
        medium.close()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    return central.control


def test_the_peripheral_opens_with_its_feature_request(exchanged) -> None:
    requests = [body for body in exchanged if body[0] == LL_PERIPHERAL_FEATURE_REQ]
    assert requests, [body.hex() for body in exchanged]
    assert len(requests[0]) == 9, requests[0].hex()        # opcode + 8 octets


def test_the_peripheral_answers_with_espressifs_version(exchanged) -> None:
    versions = [body for body in exchanged if body[0] == LL_VERSION_IND]
    assert versions, [body.hex() for body in exchanged]
    _opcode, version, company, _sub = struct.unpack("<BBHH", versions[0][:6])
    assert company == ESPRESSIF, versions[0].hex()
    assert version >= 6, versions[0].hex()                  # Bluetooth 4.0 or later
