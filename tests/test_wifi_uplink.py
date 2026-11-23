#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The station on the real internet, through --wifi-uplink (ADR 0097).

The one test in the suite that is not reproducible by construction: it uses the
host's real resolver and real NTP servers, so it skips when the host has no
internet. Everything the NAT promises is also checked hermetically, against
localhost stand-ins, in test_uplink.py and test_wifi_ap.py; this is the proof
that the firmware itself gets across.

What it asserts is the difference the uplink makes to this firmware: its NTP
server names, which the island LAN answers with the gateway, are looked up
through the host and come back as real addresses; its NTP request goes out to
one of them and a real server answers; and it sets its clock from that. Its
factory report still goes to the LAN's own service on lilygo.local.
"""

from __future__ import annotations

import re
import shutil
import socket
import sys
import tempfile
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    REPO_ROOT, Run, last_boot, qemu_available, reset_trouble, run_emulator,
)

FIRMWARE = REPO_ROOT / "firmware" / "T-Deck_UnitTest_250620.bin"
SSID, PASSPHRASE, BEACON_MS = "xinyuandianzi", "AA15994823428", 20

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,      # deadlines kept on the host's time (ADR 0167)
    pytest.mark.skipif(not FIRMWARE.exists(), reason="no T-Deck vendor build"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _host_has_internet() -> bool:
    """Whether the host can resolve an NTP pool name, within a few seconds."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        lookup = pool.submit(socket.getaddrinfo, "pool.ntp.org", 123,
                             socket.AF_INET)
        try:
            return bool(lookup.result(timeout=5))
        except Exception:
            return False


def _reported(uart: str, frames: list) -> bool:
    return "----------REMOVE TASK----------" in last_boot(uart)


@pytest.fixture(scope="module")
def online(tmp_path_factory) -> Run:
    if not _host_has_internet():
        pytest.skip("the host has no internet to uplink to")
    # Run locally and copy the logs, as test_wifi_connect does and for the same
    # reason: output across the build container's mount changes guest timing.
    scratch = Path(tempfile.mkdtemp(prefix="boardemul-uplink-"))
    try:
        # A ceiling, not a plan: the run stops once the firmware has reported.
        # Joining took until 288 s of 300 once, leaving no time for NTP.
        run = run_emulator(scratch, firmware=FIRMWARE, board="tdeck", timeout=600,
                           extra=("--psram-octal", "--wifi-ap",
                                  f"{SSID},{BEACON_MS}", "--wifi-wpa2",
                                  "--wifi-pass", PASSPHRASE, "--wifi-uplink",
                                  # It may reboot from the firmware's ipc1
                                  # overflow, and is judged on the boot that
                                  # came back (ADR 0125).
                                  "--reboot"),
                           env={"BOARDEMUL_TCG_THREADS": "multi"},
                           until=_reported)
        keep = tmp_path_factory.mktemp("uplink")
        for log in list(scratch.glob("*.uart.log")) + list(scratch.glob("*.bus.log")):
            shutil.copy2(log, keep / log.name)
        if run.boots > 1:
            warnings.warn(f"build 250620 booted {run.boots} times (ADR 0125)")
        return Run(outdir=keep, frames=run.frames, uart=run.uart)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_it_reboots_only_from_the_forgiven_panic(online: Run) -> None:
    """Any panic but ADR 0099's ipc1 canary, or a reset for any other reason,
    fails here with the boot it ended (ADR 0125)."""
    assert not reset_trouble(online.uart), reset_trouble(online.uart)


def _peer(run: Run) -> str:
    log = next(iter(run.outdir.glob("*.bus.log")), None)
    return log.read_text(errors="replace") if log else ""


def test_the_ntp_names_are_real_addresses(online: Run) -> None:
    """Looked up through the host, not answered with the gateway."""
    peer = _peer(online)
    answers = re.findall(r"dns A (pool\.ntp\.org|time\.nist\.gov) -> ([0-9., ]+) "
                         r"for [0-9a-f:]+ \(through the host\)", peer)
    assert answers, "no NTP name was looked up through the host: " + peer[-2000:]
    for _, ips in answers:
        assert "192.168.4.1" not in ips, "an NTP name still points at the gateway"


def test_a_real_ntp_server_answers_and_the_firmware_sets_its_clock(online: Run) -> None:
    peer = _peer(online)
    assert re.search(r"uplink: udp 192\.168\.4\.\d+:\d+ -> [0-9.]+:123", peer), (
        "no NTP request went out through the uplink: " + peer[-2000:])
    assert re.search(r"\d+ uplink udp in", peer), (
        "nothing came back from the internet: " + peer[-2000:])
    assert "ntp reply" not in peer, "the gateway's own NTP answered instead"
    assert "Got time adjustment from NTP!" in online.last_uart, online.last_uart[-2000:]


def test_the_factory_report_still_goes_to_the_lan(online: Run) -> None:
    assert "factory test: T-Deck" in _peer(online)
