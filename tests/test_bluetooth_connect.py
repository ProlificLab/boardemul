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

The connection that follows is the firmware's own: it stops advertising, starts
its link layer controller, tells the host, and begins scheduling connection
events on the interval the request asked for. That last part is what the tests
below look at, because it is the controller keeping a schedule this emulator
never gave it.

The link then ends, and correctly: the central on the air says nothing after
its request, and a peripheral that hears nothing from its master gives up
after about six intervals (ADR 0049). Carrying data over the link needs a
channel-hopping medium that does not exist yet.
"""

from __future__ import annotations

import re
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
REQUESTS = 6
PDU_CONNECT_IND = 0x05
# The first word of an exchange-table entry, as the core logs it. The board
# schedules the first kind continuously, whatever else is happening; the second
# it schedules only while it has a connection to service.
ADVERTISING_EVENT = "0228"
CONNECTION_EVENT = "02f8"
# What the request asks for, and so what the controller's own events should be
# spaced by. A connection interval is counted in units of 1.25 ms.
INTERVAL_UNITS = 24
INTERVAL_MS = INTERVAL_UNITS * 1.25
# The core logs its timestamps in half-microseconds.
HUS_PER_MS = 2000


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
            + struct.pack("<H", INTERVAL_UNITS)  # interval
            + struct.pack("<H", 0)              # latency
            + struct.pack("<H", 72)             # timeout, 720 ms
            + bytes.fromhex("ffffffff1f")       # channel map
            + bytes([0x0D]))                    # hop and clock accuracy
    payload = initiator + advertiser + data
    assert len(payload) == 34, len(payload)
    return bytes([PDU_CONNECT_IND]) + payload


def connection_events(log: str) -> list[int]:
    """When each connection event was kicked, in half-microseconds.

    The core logs every exchange-table entry it is handed, with the kind in the
    entry's first word and the time it was kicked, so the controller's own
    schedule can be read back without tracing the ROM.
    """
    return [int(m.group(1)) for m in re.finditer(
        r"kick entry \d+ at \S+ start \d+ hus, now (\d+) hus, "
        rf"entry {CONNECTION_EVENT} ", log)]


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
        # A handful of requests, spaced far enough apart that each connection
        # has run its course before the next arrives. A barrage would work too
        # but the events of one link would run into the next, and the interval
        # between them is the thing being measured.
        for _ in range(REQUESTS):
            central.send(connect_request())
            time.sleep(3)
        time.sleep(10)
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


def test_the_controller_accepts_and_starts_a_connection(connected) -> None:
    """The firmware's own link layer decides, and it says so by scheduling a
    kind of event it never schedules while merely advertising."""
    assert f"entry {CONNECTION_EVENT}" in connected, (
        "the link layer never scheduled a connection event; it accepted "
        "nothing:\n" + connected[-1500:])


def test_the_connection_runs_at_the_interval_it_was_asked_for(connected) -> None:
    """The strongest thing available without tracing the ROM: the controller
    keeps a schedule of its own, and it is the one the request named. Nothing
    in this emulator supplies that cadence -- it comes out of the connection
    parameters the firmware accepted (ADR 0049)."""
    events = connection_events(connected)
    assert len(events) >= 3, f"only {len(events)} connection event(s): {events}"
    gaps = [(b - a) / HUS_PER_MS for a, b in zip(events, events[1:])]
    # Consecutive events within one connection. A gap much larger than the
    # interval is the boundary between two connections, and says nothing
    # against the ones inside.
    within = [g for g in gaps if g < INTERVAL_MS * 3]
    assert within, f"no two connection events ran close together: {gaps}"
    assert all(abs(g - INTERVAL_MS) < 1.0 for g in within), (
        f"connection events were not {INTERVAL_MS} ms apart: {within}")
