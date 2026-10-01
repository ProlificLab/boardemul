#!/usr/bin/env python3
"""A firmware whose Serial is the chip's USB port, heard through it (ADR 0100).

WireScan is the vendor's I2C bus scanner for the T-Deck Max. Its Serial is
Arduino's HWCDC, so until the port was modelled it printed nothing after the
ROM's lines. Hearing its scan proves three things at once: the port carries a
firmware's output to a host, its interrupt reaches the CPU although the driver
enables it before routing it (ADR 0101), and the scan matches the board.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    REPO_ROOT, Run, qemu_available, run_emulator, usb_text,
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
    outdir = tmp_path_factory.mktemp("wirescan")
    return run_emulator(outdir, firmware=WIRESCAN, board="tdeck-max", timeout=120,
                        until=lambda _uart, _frames: SWEPT in usb_text(outdir))


def test_the_sketch_speaks_through_the_usb_port(scanned: Run) -> None:
    assert SWEPT in scanned.usb, scanned.usb[-1500:]


def test_its_serial_output_is_not_on_the_uart(scanned: Run) -> None:
    """The port is not a copy of the UART: the sketch's own lines are only on it."""
    assert SWEPT not in scanned.uart


def test_long_lines_arrive_whole(scanned: Run) -> None:
    """A line longer than the 64-byte FIFO is sent in two packets, not cut."""
    assert "Initialising I2C Master: sda=13 scl=14 freq=100000" in scanned.usb


def test_the_scan_finds_the_boards_parts(scanned: Run) -> None:
    table = scanned.usb[: scanned.usb.index(SWEPT)]
    for address in ADDRESSES:
        assert f" {address} " in table, f"0x{address} missing from:\n{table}"
