#!/usr/bin/env python3
"""A central speaks to the board over Bluetooth.

This is the peer the firmware was built for. Meshtastic never scans (ADR 0045),
so it cannot meet another board; what it waits for is something connecting to
it, which is what this sends.

The connection request is what the specification says one is, and what the ROM
checks it against: an initiator address, an advertiser address, and connection
data whose interval, latency and timeout the controller validates before it
will look at them. Nothing here is accepted on this emulator's say-so -- the
firmware's own link layer decides.

What these tests do *not* show is a connection. ADR 0048 measured what becomes
of the request: the ROM validates it, `r_lld_con_start` runs, and the
controller library tears the connection down inside the same call. The board
goes on advertising throughout. So the last test below asserts only what it
can see -- that the request reaches the link layer and changes what the
controller schedules -- and claims nothing about a link being up.
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

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

BLUETOOTH_FS = REPO_ROOT / "build" / "littlefs-bluetooth.bin"
OVERLAY_AT = 0xC90000
RUN_SECONDS = 330
PDU_CONNECT_IND = 0x05
# The first word of an exchange-table entry, as the core logs it. The board
# schedules the first kind continuously; the second appears only after a
# connection request and, despite the name it was first given, is an
# advertising event too -- it keeps the advertising cadence and carries
# `r_lld_adv_frm_cbk` (ADR 0048).
ADVERTISING_EVENT = "0228"
REQUESTED_EVENT = "0230"


def connect_request() -> bytes:
    """Six bytes of initiator address, six of advertiser, twenty-two of
    connection data -- the interval, latency and timeout are inside the ranges
    the ROM insists on, which is why it accepts this and not any 34 bytes."""
    initiator = bytes.fromhex("ccbbaa286f24")
    advertiser = bytes.fromhex("0100000000c0")
    data = (struct.pack("<I", 0xAF9AB4C3)       # access address
            + bytes.fromhex("555555")           # CRC seed
            + bytes([3])                        # window size
            + struct.pack("<H", 1)              # window offset
            + struct.pack("<H", 24)             # interval, 30 ms
            + struct.pack("<H", 0)              # latency
            + struct.pack("<H", 72)             # timeout, 720 ms
            + bytes.fromhex("ffffffff1f")       # channel map
            + bytes([0x0D]))                    # hop and clock accuracy
    payload = initiator + advertiser + data
    assert len(payload) == 34, len(payload)
    return bytes([PDU_CONNECT_IND]) + payload


@pytest.fixture(scope="module")
def bluetooth_filesystem() -> Path:
    if not BLUETOOTH_FS.exists():
        done = subprocess.run(
            [sys.executable, "tools/make_configured_fs.py", "--bluetooth",
             "-o", str(BLUETOOTH_FS)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=900)
        if done.returncode != 0 or not BLUETOOTH_FS.exists():
            pytest.fail("could not build a Bluetooth filesystem:\n"
                        + done.stdout[-2000:] + done.stderr[-2000:])
    return BLUETOOTH_FS


@pytest.fixture(scope="module")
def connected(tmp_path_factory, bluetooth_filesystem: Path) -> str:
    """Boot the board, wait to hear it, connect to it, and report the trace."""
    work = tmp_path_factory.mktemp("blecon")
    air = work / "air"
    air.mkdir()
    (work / "build").mkdir()

    proc = subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal", "--icount",
         "--ble-air", str(air), "--builddir", str(work / "build"),
         "--overlay", f"0x{OVERLAY_AT:x}={bluetooth_filesystem}",
         "--outdir", str(work), "--tag", "con",
         "--timeout", str(RUN_SECONDS), "--stall", str(RUN_SECONDS)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    central = AirMedium(air, "central")
    try:
        # Wait until it is actually advertising; connecting to silence proves
        # nothing, and the link layer takes a while to come up.
        deadline = time.monotonic() + RUN_SECONDS * 0.7
        while time.monotonic() < deadline and not central.poll():
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        for _ in range(60):
            central.send(connect_request())
            time.sleep(0.25)
        time.sleep(20)
    finally:
        central.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    log = work / "con.qemu.log"
    return log.read_text(errors="replace") if log.exists() else ""


def test_the_request_reaches_the_link_layer(connected) -> None:
    """Delivered into the descriptor the ROM reads next, with the packet in the
    buffer that descriptor already named (ADR 0046)."""
    assert "delivered a type 5" in connected, connected[-1500:]


def test_it_was_advertising_first(connected) -> None:
    """Otherwise there was nothing to connect to and nothing is proven."""
    assert f"entry {ADVERTISING_EVENT}" in connected, connected[-1500:]


def test_the_request_changes_what_the_controller_schedules(connected) -> None:
    """The link layer acts on the request: it schedules a kind of event it
    never schedules while merely advertising. That is the whole claim -- the
    connection does not survive `r_lld_con_start` (ADR 0048)."""
    assert f"entry {REQUESTED_EVENT}" in connected, (
        "the link layer scheduled nothing new; the request changed "
        "nothing:\n" + connected[-1500:])
