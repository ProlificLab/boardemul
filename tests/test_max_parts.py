#!/usr/bin/env python3
"""The T-Deck Max's factory demo checks its own parts, and says so.

The demo brings each part up in turn and reports on it over its USB serial
port (ADR 0100). Its verdicts are the assertions here, part by part: they are
the firmware's, not this project's. A part joins this file when the board
models it well enough to pass the firmware's own check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE, Run, qemu_available, radio_busy, run_emulator, usb_text,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

# The touch controller is the last part the demo checks.
CHECKED = "[[HYN]] Touch init"


@pytest.fixture(scope="module")
def checked(tmp_path_factory) -> Run:
    outdir = tmp_path_factory.mktemp("maxparts")
    return run_emulator(outdir, firmware=FIRMWARE, board="tdeck-max", timeout=300,
                        until=lambda _uart, _frames: CHECKED in usb_text(outdir))


def test_the_demo_got_through_its_checks(checked: Run) -> None:
    assert CHECKED in checked.usb, checked.usb[-2000:]


def test_the_radio_is_found(checked: Run) -> None:
    """On chip select 3, where the vendor's pin map and the firmware put it,
    not 9, where it was once inferred to be (ADR 0102)."""
    assert "[SX1262] Initializing ... success!" in checked.usb, checked.usb[-2000:]


def test_the_radio_held_its_busy_line_and_was_waited_for(checked: Run) -> None:
    """BUSY on pin 6 and reset on pin 4, kept on the guest's clock (ADR 0111).
    A reset holds it for the datasheet's 3.5 ms start-up, and the demo found
    the radio after it, so it waited."""
    busy = radio_busy(checked.outdir)
    assert busy is not None, "the controller never reported the radio's BUSY line"
    holds, longest = busy
    assert holds >= 1 and longest >= 3500, busy


def test_the_fuel_gauge_is_trusted_and_provisioned(checked: Run) -> None:
    """It answers its device number, unseals, takes its configuration and
    leaves configuration mode, each step of which the driver waits on."""
    assert "(244) Checking data memory" in checked.usb, checked.usb[-2000:]
    for failure in ("Invalid Device Number", "INITCOMP timeout", "timeout after"):
        assert failure not in checked.usb, checked.usb[-2000:]


def test_the_receiver_answers_its_configuration(checked: Run) -> None:
    """A u-blox on the third UART, answering the CFG-RATE poll the demo waits
    for (ADR 0105)."""
    assert "GPS Task Create...!" in checked.usb, checked.usb[-2000:]
    assert "GPS Connect failed" not in checked.usb


def test_the_modem_answers(checked: Run) -> None:
    """An A7682E on the second UART: it answers AT, so the demo does not give
    up on it (ADR 0105)."""
    assert "Start modem..." in checked.usb, checked.usb[-2000:]
    assert "[A7682E] Init Fail" not in checked.usb, checked.usb[-2000:]


def test_the_sensor_hub_boots_its_firmware(checked: Run) -> None:
    """The demo uploads Bosch's firmware, boots it and finds its sensors
    (ADR 0108)."""
    assert "Boot successful." in checked.usb, checked.usb[-2000:]
    assert "Total 6 Sensor online" in checked.usb, checked.usb[-2000:]
    assert "Failed to init BHI260AP" not in checked.usb
