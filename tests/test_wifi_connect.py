#!/usr/bin/env python3
"""The station authenticates, associates, and gets an IP address.

Two 802.11 exchanges that had never happened on either board. The scan came
first (ADR 0057), then a transmit engine that completed (ADR 0059), then an
access point that read what the station sent and answered it (ADR 0060). What
stood between the answers and the station accepting them was a receive detail:
`wdevProcessRxSucDataAll` walks the driver's descriptor list counting how many
it examined to reach a frame, and `wDev_IndicateFrame` passes a frame to the
802.11 stack only when that count is one. The model used to fill whichever
descriptor the driver had most recently handed back, so a frame landed at an
arbitrary position and was usually thrown away unread -- which a scan survives,
because beacons come many a second and enough of them draw a one, and a single
authentication reply does not (ADR 0063).

So this asserts the frames, in both directions, out of the model's own log:
the station's authentication and the reply it accepted, then the association
request it only sends because that reply was accepted, and the response to it.

It no longer stops there. With the cross-thread TLB flush fixed (ADR 0066) and
the GDMA able to reach PSRAM (ADR 0083), the WPA2 four-way handshake completes,
and with ARP and a one-station DHCP in the peer (ADR 0084) the firmware gets an
address: it prints "Obtained IP address" and stays connected. So the run goes
all the way, under `thread=multi` (the handshake needs it), and the last test
asserts the address.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import REPO_ROOT, Run, qemu_available, run_emulator  # noqa: E402

FIRMWARE = REPO_ROOT / "firmware" / "T-Deck_UnitTest_250620.bin"
# The network this firmware actually wants, with the passphrase it carries, at
# an SSID and beacon rate it will try to join. A name it does not know gets as
# far as a scan result and no further: `no matching wifi found`.
SSID = "xinyuandianzi"
# And the passphrase it carries beside the name, near offset 0x11dd9 of the
# image. The four-way handshake derives its keys from the two together, so a
# peer given the name and not the passphrase can answer an association request
# and nothing after it.
PASSPHRASE = "AA15994823428"
# Twenty milliseconds. A scan that hears nothing cannot start a connect, and
# the firmware scans in short bursts while it retries -- at a tenth of a second
# five scans in six heard nothing and the first association took two minutes;
# at twenty milliseconds none of them did and it took forty-eight seconds.
BEACON_MS = 20

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="no T-Deck vendor build"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _device_said(outdir: Path, tail: int = 0) -> str:
    """Read again each time, and forgive a failure: a run being waited on still
    has the file open, and reading one mid-write over the build container's
    bind mount raises ENODATA rather than a short read. The UART helper in
    `support` guards its own read for the same reason.

    `tail` reads only the end of it. The waiting predicate is called twice a
    second for the length of the run, and this log grows to most of a megabyte
    -- re-reading the whole thing six hundred times is enough host I/O to shift
    the guest's own timing, which is not something a test should be doing to
    the thing it is measuring."""
    log = next(iter(outdir.glob("*.qemu.log")), None)
    if not log:
        return ""
    try:
        if not tail:
            return log.read_text(errors="replace")
        with log.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _peer_said(outdir: Path) -> str:
    """The access-point peer's own log, where the handshake and DHCP are
    recorded -- a different file from the device/QEMU log `_device_said` reads."""
    log = next(iter(outdir.glob("*.bus.log")), None)
    try:
        return log.read_text(errors="replace") if log else ""
    except OSError:
        return ""


def _got_ip(outdir: Path):
    """The whole point now: a run can stop once the firmware has an address.
    That line is on the UART, unlike association, which only the model's log
    witnesses -- so this reads what the guest printed."""
    def check(uart: str, frames: list) -> bool:
        return "Obtained IP address" in uart
    return check


@pytest.fixture(scope="module")
def connected(tmp_path_factory) -> Run:
    """Run somewhere local, then copy the evidence to where the suite keeps it.

    `pytest.ini` puts every slow test's run directory under `logs/` so its
    artefacts survive, which is the right default and wrong for this one. On a
    checkout mounted into the build container -- this project's usual shape on
    Windows -- that directory is across the mount, and the panel frames and
    logs a run writes there cost enough to change the guest's timing. Measured,
    the same command with nothing changed but the output directory: **nine
    scans and none of them silent** writing locally, **seventy-four scans and
    seventy-one silent** writing across the mount, and no connection at all in
    the second case. A scan that hears nothing never starts a connect, so the
    test would be measuring the filesystem.
    """
    scratch = Path(tempfile.mkdtemp(prefix="boardemul-connect-"))
    try:
        run = run_emulator(scratch, firmware=FIRMWARE, board="tdeck",
                           timeout=300,
                           extra=("--psram-octal", "--wifi-ap",
                                  f"{SSID},{BEACON_MS}", "--wifi-wpa2",
                                  "--wifi-pass", PASSPHRASE),
                           # The four-way handshake deadlocks under one
                           # translation thread and completes under two
                           # (ADR 0066); the full connection needs multi.
                           env={"BOARDEMUL_TCG_THREADS": "multi"},
                           until=_got_ip(scratch))
        keep = tmp_path_factory.mktemp("connect")
        for log in (list(scratch.glob("*.uart.log"))
                    + list(scratch.glob("*.qemu.log"))
                    + list(scratch.glob("*.bus.log"))):
            shutil.copy2(log, keep / log.name)
        # The assertions read the copy, so the scratch directory can go.
        return Run(outdir=keep, frames=run.frames, uart=run.uart)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_station_authenticates(connected: Run) -> None:
    """An authentication frame, and the reply going back. Subtype eleven is a
    first octet of 0xb0 both ways: the station's request is Open System
    transaction one, and the answer is transaction two, status zero."""
    said = _device_said(connected.outdir)
    assert re.search(r"the driver transmitted on slot \d+ \(\d+ so far\) "
                     r"a 0xb0 to ([0-9a-f]{2}:){5}[0-9a-f]{2}", said), (
        "the station never authenticated: " + said[-2000:])
    assert "the answer, a 0xb0" in said, (
        "the authentication was never answered: " + said[-2000:])


def test_the_station_associates(connected: Run) -> None:
    """And the frame that says the authentication was accepted.

    This is the assertion that needed the descriptor order to be right. A
    station sends an association request only after an authentication response
    it believes; every run before this one went quiet instead, and the reply
    was being discarded by the ROM before the stack saw it.
    """
    said = _device_said(connected.outdir)
    assert re.search(r"the driver transmitted on slot \d+ \(\d+ so far\) "
                     r"a 0x00 to ([0-9a-f]{2}:){5}[0-9a-f]{2}", said), (
        "the station never asked to associate: " + said[-2000:])
    assert "the answer, a 0x10" in said, (
        "the association request was never answered: " + said[-2000:])


def test_it_authenticates_before_it_associates(connected: Run) -> None:
    """In that order, and close together. The station will not ask to associate
    until it is authenticated, so an association request that came first would
    mean this test is reading something other than what it thinks."""
    said = _device_said(connected.outdir)
    auth = [int(m) for m in re.findall(
        r"at (\d+) ms the driver transmitted on slot \d+ \(\d+ so far\) a 0xb0", said)]
    assoc = [int(m) for m in re.findall(
        r"at (\d+) ms the driver transmitted on slot \d+ \(\d+ so far\) a 0x00", said)]
    assert auth and assoc, f"auth {auth}, assoc {assoc}"
    # Pair the first association with the authentication it followed, not with
    # the first of the run: the station authenticates, fails at the handshake,
    # and tries the whole thing again, so those two can be minutes apart and
    # comparing them says nothing. What matters is that the association had an
    # authentication immediately in front of it.
    before = [t for t in auth if t <= assoc[0]]
    assert before, (f"associated at {assoc[0]} ms with no authentication "
                    f"before it; authentications at {auth}")
    gap = assoc[0] - before[-1]
    # Milliseconds apart in practice -- ten, in the run this was written from.
    assert gap < 5000, (f"{gap} ms between authenticating at {before[-1]} and "
                        f"associating at {assoc[0]}, which is not one exchange")


def test_the_station_gets_an_ip(connected: Run) -> None:
    """The whole stack, end to end: the four-way handshake completes and the
    firmware's DHCP client gets the address the peer offers (ADRs 0083, 0084).
    The handshake completing is what the peer's log records as a done handshake;
    the address is what the firmware prints when its DHCP client has it."""
    peer = _peer_said(connected.outdir)
    assert "completed the four-way handshake" in peer, (
        "the WPA2 four-way handshake never completed: " + peer[-2000:])
    assert "dhcp offer" in peer and "dhcp ack" in peer, (
        "the DHCP exchange did not finish (offer and ack): " + peer[-2000:])
    assert "Obtained IP address" in connected.uart, (
        "the firmware never got an IP: " + connected.uart[-2000:])
