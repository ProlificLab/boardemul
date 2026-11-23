#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A central connects to the board over Bluetooth, and keeps the link.

This is the peer the firmware was built for. Meshtastic never scans (ADR 0045),
so it cannot meet another board; what it waits for is something connecting to
it, which is what this does.

The connection request is what the specification says one is, and what the ROM
checks it against: an initiator address, an advertiser address, and connection
data whose interval, latency and timeout the controller validates before it
will accept them. Nothing here is accepted on this emulator's say-so -- the
firmware's own link layer decides, and what it decides is visible in the
schedule it then keeps.

That schedule is the point of these tests. A connection the controller has
accepted but cannot service dies after about six intervals, which is what the
specification gives a link to establish itself (ADR 0049); one that is being
serviced runs on the interval it was given for as long as its master keeps
speaking (ADR 0051). The difference between those two is what is measured
here, and neither of them is something this emulator supplies.
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
from tools.ble_central import ADV_AA, CONN_AA, on, split  # noqa: E402
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
RUN_SECONDS = 480
# How long to wait for the board to start advertising, in wall time.
ADVERTISING_SECONDS = 240
# The central goes on speaking until the link has run for as long as the
# assertions need, in the guest's own clock -- they want 40 events spanning
# ALIVE_MS, and this waits for some margin over both. It used to speak for a
# fixed 20 wall seconds, which a slower host turns into less link: under a full
# suite that bought twelve events and the test failed (ADR 0040's rule: stop on
# the evidence, not on a budget). The ceiling is only for a link that has died.
ENOUGH_EVENTS = 50
ENOUGH_MS = 2500
KEEPALIVE_CEILING_SECONDS = 180
PDU_CONNECT_IND = 0x05
# The first word of an exchange-table entry, as the core logs it. These are the
# kinds a board schedules while it is only advertising; the controller has
# several for a connection and they were not all seen at once, so anything
# that is not one of these is taken as the connection's. They are the
# controller's constants, not this project's.
ADVERTISING_EVENTS = ("0228", "0230")
# What the request asks for, and so what the controller's own events should be
# spaced by. A connection interval is counted in units of 1.25 ms.
INTERVAL_UNITS = 24
INTERVAL_MS = INTERVAL_UNITS * 1.25
# The core logs its timestamps in half-microseconds.
HUS_PER_MS = 2000
# Comfortably past the six intervals the specification gives a connection to
# establish itself, and low enough that a guest crawling under a loaded suite
# still reaches it.
ALIVE_MS = 2000


def connect_request(advertiser: bytes) -> bytes:
    """Six bytes of initiator address, six of advertiser, twenty-two of
    connection data -- the interval, latency and timeout are inside the ranges
    the ROM insists on, which is why it accepts this and not any 34 bytes.
    The advertiser is the address its advertisement gave: a board takes only
    a request that names it (ADR 0173). Sent on the advertising address."""
    initiator = bytes.fromhex("ccbbaa286f24")
    data = (struct.pack("<I", CONN_AA)           # access address
            + bytes.fromhex("555555")            # CRC seed
            + bytes([3])                         # window size
            + struct.pack("<H", 1)               # window offset
            + struct.pack("<H", INTERVAL_UNITS)  # interval
            + struct.pack("<H", 0)               # latency
            + struct.pack("<H", 72)              # timeout, 720 ms
            + bytes.fromhex("ffffffff1f")        # channel map
            + bytes([0x0D]))                     # hop and clock accuracy
    payload = initiator + advertiser + data
    assert len(payload) == 34, len(payload)
    return on(ADV_AA, bytes([PDU_CONNECT_IND]) + payload)


def advertiser(frames: list[bytes]) -> bytes | None:
    """The address an advertisement heard gives, if one was (ADR 0171)."""
    for aa, pdu in map(split, frames):
        if aa == ADV_AA and len(pdu) >= 7 and not pdu[0] & 0x0F:
            return pdu[1:7]
    return None


def empty_packet() -> bytes:
    """What a master sends when it has nothing to say: a data packet whose
    LLID is one and whose payload is empty. It carries nothing, and hearing it
    is what tells a peripheral its master is still there."""
    return on(CONN_AA, bytes([0x01]))


def connection_events(log: str) -> list[int]:
    """When each connection event was kicked, in half-microseconds.

    The core logs every exchange-table entry it is handed, with the kind in the
    entry's first word and the time it was kicked, so the controller's own
    schedule can be read back without tracing the ROM.
    """
    return [int(at) for at, kind in re.findall(
        r"kick entry \d+ at \S+ start \d+ hus, now (\d+) hus, entry ([0-9a-f]{4}) ",
        log) if kind not in ADVERTISING_EVENTS]


class _LinkWatch:
    """The connection events in a growing QEMU log, read a piece at a time.

    Only what was appended since the last look is read -- re-reading the whole
    log every second is enough host I/O to slow the guest being measured -- and
    only whole lines are parsed, so an event half-written at the moment of
    looking is counted next time rather than lost.
    """

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.carry = ""
        self.count = 0
        self.first: int | None = None
        self.last: int | None = None

    def lasted(self, events: int, span_ms: float) -> bool:
        """Whether at least `events` connection events spanning `span_ms` of
        guest time have been logged."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return False
        self.offset += len(chunk)
        text = self.carry + chunk.decode("utf-8", errors="replace")
        text, _, self.carry = text.rpartition("\n")
        for at in connection_events(text):
            self.count += 1
            self.first = at if self.first is None else self.first
            self.last = at
        if self.first is None or self.last is None:
            return False
        return (self.count >= events
                and (self.last - self.first) / HUS_PER_MS >= span_ms)


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
    """Boot the board, wait to hear it, connect, then keep speaking to it."""
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
        deadline = time.monotonic() + ADVERTISING_SECONDS
        address = None
        while time.monotonic() < deadline and address is None:
            address = advertiser(central.poll())
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        central.send(connect_request(address or bytes(6)))
        # One request, then talk. A real central sends exactly one, and the
        # link then lives or dies on whether it is spoken to -- for as long as
        # it takes the link to show what the assertions ask of it.
        link = _LinkWatch(work / "con.qemu.log")
        until = time.monotonic() + KEEPALIVE_CEILING_SECONDS
        next_look = 0.0
        while time.monotonic() < until and proc.poll() is None:
            central.send(empty_packet())
            if time.monotonic() >= next_look:
                next_look = time.monotonic() + 1.0
                if link.lasted(ENOUGH_EVENTS, ENOUGH_MS):
                    break
            time.sleep(0.01)
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
    assert f"entry {ADVERTISING_EVENTS[0]}" in connected, connected[-1500:]


def test_the_controller_accepts_and_starts_a_connection(connected) -> None:
    """The firmware's own link layer decides, and it says so by scheduling a
    kind of event it never schedules while merely advertising."""
    assert connection_events(connected), (
        "the link layer never scheduled a connection event; it accepted "
        "nothing:\n" + connected[-1500:])


def test_the_link_stays_up_while_it_is_spoken_to(connected) -> None:
    """The difference between a connection that was accepted and one that is
    being serviced. Unserviced it dies after about six intervals; here it
    should last for as long as the central keeps talking (ADR 0051)."""
    events = connection_events(connected)
    assert len(events) >= 40, f"only {len(events)} connection event(s)"
    lived = (events[-1] - events[0]) / HUS_PER_MS
    assert lived >= ALIVE_MS, (
        f"the link ran for {lived:.0f} ms over {len(events)} events, which is "
        f"not past the window a connection is given to establish itself")


def test_the_connection_runs_at_the_interval_it_was_asked_for(connected) -> None:
    """The controller keeps a schedule of its own, and it is the one the
    request named. Nothing in this emulator supplies that cadence -- it comes
    out of the connection parameters the firmware accepted.

    The middle gap is the measure, not every gap: an event whose packet did
    not arrive in time is skipped, and the next one lands an interval later,
    which is the link working rather than failing.
    """
    events = connection_events(connected)
    gaps = sorted((b - a) / HUS_PER_MS for a, b in zip(events, events[1:]))
    assert gaps, f"only {len(events)} connection event(s)"
    middle = gaps[len(gaps) // 2]
    assert abs(middle - INTERVAL_MS) < 1.0, (
        f"connection events ran {middle:.2f} ms apart, not {INTERVAL_MS}; "
        f"the shortest was {gaps[0]:.2f} and the longest {gaps[-1]:.2f}")
