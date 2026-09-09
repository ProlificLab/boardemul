#!/usr/bin/env python3
"""The firmware finds a satellite receiver on the board's second UART.

The vendor build probes that port at boot: it silences the module, asks it what
it is, and waits for an answer. With nothing on the line it printed
`Get L76K timeout!` and carried on without one, which is what this emulator did
until the module was modelled (ADR 0052).

The assertion is the firmware's own verdict, not this project's: it says which
module it decided it is talking to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import REPO_ROOT, Run, qemu_available, run_emulator  # noqa: E402

# The build that probes at boot. The later ones do it much further into their
# sequence, which would make this a much longer test for the same fact.
FIRMWARE = REPO_ROOT / "firmware" / "T-Deck_UnitTest_250620.bin"
# Somewhere with a sign on both axes, so a fault in either would show.
WHERE = "51.5074,-0.1278,35"
SUCCESS = "L76K GNSS init succeeded"
FAILURE = "Get L76K timeout!"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="no T-Deck vendor build"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _past_the_gps_stage(uart: str, frames: list) -> bool:
    """The firmware's verdict, and then far enough past it that it has finished
    configuring what it found. It says nothing of its own when it does that, so
    the marker is the first thing it reaches afterwards -- the same one the
    other vendor tests wait for.
    """
    return (SUCCESS in uart or FAILURE in uart) and "ADC_CALI" in uart


@pytest.fixture(scope="module")
def booted(tmp_path_factory) -> Run:
    return run_emulator(tmp_path_factory.mktemp("gps"), firmware=FIRMWARE,
                        board="tdeck", timeout=240,
                        extra=("--psram-octal", "--gps", WHERE),
                        until=_past_the_gps_stage)


def test_the_firmware_finds_the_module(booted: Run) -> None:
    """Its own words. Without the model this line is the timeout instead."""
    assert SUCCESS in booted.uart, booted.uart[-1500:]
    assert FAILURE not in booted.uart, booted.uart[-1500:]


def test_it_goes_on_to_configure_the_module_it_found(booted: Run) -> None:
    """Finding it is not the whole of it: having decided what it is, the
    firmware sets the module's constellations, sentence rates and navigation
    mode, and only a module that answered the query gets that far. The run's
    own bus log records the commands the model was handed."""
    log = next(iter(booted.outdir.glob("*.bus.log")), None)
    assert log is not None, "no bus log; the run did not use --bus"
    seen = log.read_text(errors="replace")
    for command in ("PCAS03", "PCAS04", "PCAS06", "PCAS11"):
        assert command in seen, f"{command} never reached the module: {seen[-1500:]}"
