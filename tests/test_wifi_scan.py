#!/usr/bin/env python3
"""The firmware finds a network the emulator puts on the air.

This is the last thing on either board that had nothing behind it. The window
was a trace region for months: the driver would start its station and scan, and
there was never anything to find (ADR 0054). What it took was the interrupt bit
the driver's own handler routes to its receive path (ADR 0056), a forty-eight
byte header with the frame's length in it, and a beacon that claims the channel
it arrives on (ADR 0057).

The assertion is the firmware's own verdict, not this project's. Arduino's
WiFiMulti prints `[WIFI] no networks found` when a scan returned nothing at
all, and `no matching wifi found!` when it returned something that was not on
its own list of access points to join. So the second line is expected
throughout -- the network here is not one the firmware wants -- and the first
line *not* being printed for a scan is that scan having heard the beacon.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import REPO_ROOT, Run, qemu_available, run_emulator  # noqa: E402

# The build that brings its station up and scans on a loop, every seven seconds.
FIRMWARE = REPO_ROOT / "firmware" / "T-Deck_UnitTest_250620.bin"
SSID = "BoardEmul"
SCAN_DONE = "Completed scan for access points"
NOTHING_HEARD = "[WIFI] no networks found"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="no T-Deck vendor build"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _heard_the_network(uart: str, frames: list) -> bool:
    """Enough scans to be a pattern, and two of them that heard something.

    Waiting for two rather than one because a single scan hearing a beacon
    could be luck; a long run has every scan hearing it, which is what a real
    access point beaconing ten times a second looks like.
    """
    scans = uart.count(SCAN_DONE)
    return scans >= 4 and scans - uart.count(NOTHING_HEARD) >= 2


@pytest.fixture(scope="module")
def scanned(tmp_path_factory) -> Run:
    return run_emulator(tmp_path_factory.mktemp("wifi"), firmware=FIRMWARE,
                        board="tdeck", timeout=400,
                        extra=("--psram-octal", "--wifi-ap", f"{SSID},100"),
                        until=_heard_the_network)


def test_the_station_keeps_scanning_with_beacons_arriving(scanned: Run) -> None:
    """A frame arriving used to stop the scan dead: the driver took four and
    then never handed a descriptor back, and the station stopped looking. So
    that it goes on scanning at all is worth asserting on its own."""
    assert scanned.uart.count(SCAN_DONE) >= 3, scanned.uart[-1500:]


def test_a_scan_hears_the_beacon(scanned: Run) -> None:
    """The firmware's own words. Every scan said `no networks found` before
    this, and the ones that hear the beacon do not."""
    scans = scanned.uart.count(SCAN_DONE)
    silent = scanned.uart.count(NOTHING_HEARD)
    assert silent < scans, (
        f"all {scans} scan(s) reported nothing: {scanned.uart[-1500:]}")


def test_the_station_transmits_and_the_access_point_answers(scanned: Run) -> None:
    """A scan is not only listening: the station sends probe requests, and this
    access point reads them and replies.

    Both halves are worth asserting because both were missing. Nothing used to
    complete a transmission, so the station's first frame seized its queue and
    it never sent another (ADR 0059); and nothing read what it sent, so there
    was nothing to reply to (ADR 0060). A probe request is subtype 4 -- a first
    octet of 0x40 -- and a probe response is subtype 5, 0x50.
    """
    log = next(iter(scanned.outdir.glob("*.qemu.log")), None)
    assert log is not None, "no QEMU log for this run"
    said = log.read_text(errors="replace")
    assert "the driver transmitted on slot" in said, (
        "the station never transmitted: " + said[-1500:])
    assert re.search(r"the station sent a 0x40 from ([0-9a-f]{2}:){5}[0-9a-f]{2}"
                     r" -- answering with a 0x50", said), (
        "no probe request was read and answered: " + said[-1500:])


def test_the_driver_took_almost_every_beacon(scanned: Run) -> None:
    """And the model's side of it, from the run's own log. A beacon put on the
    air is a descriptor the driver has to consume and re-arm, and a run where
    it stops doing that is one where the ring runs dry -- which is how this
    looked before the interrupt bit was right: four frames and then nothing,
    with the station giving up on scanning too.

    Not all of them: the ring runs dry for a moment here and there while the
    driver is bringing its station up, and the device says so and waits."""
    log = next(iter(scanned.outdir.glob("*.qemu.log")), None)
    assert log is not None, "no QEMU log for this run"
    said = log.read_text(errors="replace")
    assert "the driver handed over a receive ring" in said, said[-1500:]
    counts = re.findall(r"(\d+) beacon\(s\) put on the air, the driver took (\d+)",
                        said)
    assert counts, "the device never reported how it was getting on: " + said[-1500:]
    put, taken = (int(n) for n in counts[-1])
    assert taken > put * 0.9, f"the driver took {taken} of {put} beacon(s)"
