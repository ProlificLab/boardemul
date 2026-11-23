#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A firmware whose Serial is the chip's USB port, heard through it (ADR 0100).

WireScan is the vendor's I2C bus scanner for the T-Deck Max. Its Serial is
Arduino's HWCDC, so until the port was modelled it printed nothing after the
ROM's lines. Hearing its scan proves three things at once: the port carries a
firmware's output to a host, its interrupt reaches the CPU although the driver
enables it before routing it (ADR 0101), and the scan matches the board.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    REPO_ROOT, Run, last_boot, qemu_available, reset_trouble, run_emulator,
    usb_text,
)

WIRESCAN = REPO_ROOT / "firmware" / "examples" / "WireScan.bin"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not WIRESCAN.exists(), reason="no WireScan image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

# The sketch's own summary line, printed once a sweep is complete.
SWEPT = "Detected 8 I2C devices"
# The parts the T-Deck Max board model puts on its I2C bus.
ADDRESSES = ("18", "1a", "20", "28", "34", "55", "5a", "6a")


@pytest.fixture(scope="module")
def scanned(tmp_path_factory) -> Run:
    """The board may reboot from the firmware's ipc1 overflow, and is judged on
    the boot that came back (ADR 0125)."""
    outdir = tmp_path_factory.mktemp("wirescan")

    def swept(_uart, _frames) -> bool:
        return SWEPT in last_boot(usb_text(outdir))

    run = run_emulator(outdir, firmware=WIRESCAN, board="tdeck-max", timeout=120,
                       extra=("--reboot",), until=swept)
    if run.boots > 1:
        warnings.warn(f"WireScan booted {run.boots} times (ADR 0125)")
    return run


def test_it_reboots_only_from_the_forgiven_panic(scanned: Run) -> None:
    """Any panic but ADR 0099's ipc1 canary, or a reset for any other reason,
    fails here with the boot it ended (ADR 0125)."""
    assert not reset_trouble(scanned.uart), reset_trouble(scanned.uart)


def test_the_sketch_speaks_through_the_usb_port(scanned: Run) -> None:
    assert SWEPT in scanned.last_usb, scanned.last_usb[-1500:]


def test_its_serial_output_is_not_on_the_uart(scanned: Run) -> None:
    """The port is not a copy of the UART: the sketch's own lines are only on it."""
    assert SWEPT not in scanned.uart


def test_long_lines_arrive_whole(scanned: Run) -> None:
    """A line longer than the 64-byte FIFO is sent in two packets, not cut."""
    assert "Initialising I2C Master: sda=13 scl=14 freq=100000" in scanned.last_usb


def test_the_scan_finds_the_boards_parts(scanned: Run) -> None:
    said = scanned.last_usb
    table = said[: said.index(SWEPT)]
    for address in ADDRESSES:
        assert f" {address} " in table, f"0x{address} missing from:\n{table}"
