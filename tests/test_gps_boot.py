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
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    REPO_ROOT, Run, last_boot, qemu_available, reset_trouble, run_emulator,
)

# The build that probes at boot. The later ones do it much further into their
# sequence, which would make this a much longer test for the same fact.
FIRMWARE = REPO_ROOT / "firmware" / "T-Deck_UnitTest_250620.bin"
# Somewhere with a sign on both axes, so a fault in either would show.
WHERE = "51.5074,-0.1278,35"
SUCCESS = "L76K GNSS init succeeded"
FAILURE = "Get L76K timeout!"
# The other way it can give up: it commands silence first and waits for the
# line to go quiet. Both are terminal, so the run stops on either.
NO_QUIET = "Wait L76K stop NMEA timeout!"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="no T-Deck vendor build"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _past_the_gps_stage(uart: str, frames: list) -> bool:
    """The firmware's verdict, and then far enough past it that it has finished
    configuring what it found. It says nothing of its own when it does that, so
    the marker is the first thing it reaches afterwards -- the same one the
    other vendor tests wait for. Read from the boot in progress: the board may
    reboot (ADR 0125).
    """
    uart = last_boot(uart)
    verdict = SUCCESS in uart or FAILURE in uart or NO_QUIET in uart
    return verdict and "ADC_CALI" in uart


@pytest.fixture(scope="module")
def booted(tmp_path_factory) -> Run:
    """The board may reboot from the firmware's ipc1 overflow; this is the
    build ADR 0099 traced it on (ADR 0125). The module is a part on the board,
    so its log spans both boots; the firmware is judged on the last."""
    run = run_emulator(tmp_path_factory.mktemp("gps"), firmware=FIRMWARE,
                       board="tdeck", timeout=240,
                       extra=("--psram-octal", "--gps", WHERE, "--reboot"),
                       until=_past_the_gps_stage)
    if run.boots > 1:
        warnings.warn(f"build 250620 booted {run.boots} times (ADR 0125)")
    return run


def test_it_reboots_only_from_the_forgiven_panic(booted: Run) -> None:
    """Any panic but ADR 0099's ipc1 canary, or a reset for any other reason,
    fails here with the boot it ended (ADR 0125)."""
    assert not reset_trouble(booted.uart), reset_trouble(booted.uart)


def test_the_firmware_finds_the_module(booted: Run) -> None:
    """Its own words. Without the model this line is the timeout instead."""
    said = booted.last_uart
    assert SUCCESS in said, said[-1500:]
    assert FAILURE not in said, said[-1500:]
    assert NO_QUIET not in said, (
        "the module would not go quiet when told to: " + said[-1500:])


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


def test_it_silenced_a_module_that_was_already_talking(booted: Run) -> None:
    """The module streams from power-on, as the part does (ADR 0116), so the
    firmware's "Wait stop" step had a live line to quiet and drain, not an
    empty one, before it asked its question."""
    log = next(iter(booted.outdir.glob("*.bus.log")), None)
    assert log is not None, "no bus log; the run did not use --bus"
    seen = log.read_text(errors="replace")
    first_report = seen.find("first report, at")
    first_command = seen.find("asked for PCAS03")
    assert first_report != -1, f"the module never spoke unprompted: {seen[-1500:]}"
    assert first_report < first_command, (
        "the firmware spoke before the module had: " + seen[-1500:])
