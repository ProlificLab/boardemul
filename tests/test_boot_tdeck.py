#!/usr/bin/env python3
"""End-to-end tests for the T-Deck: the vendor firmware reaches its interface.

There are no golden frames here, and deliberately: LVGL animates the launcher
and the status bar carries a clock, so no single frame is stable. What is
stable is the sequence of walls the firmware used to stop at, and each test
below is one of them no longer stopping it (ADR 0013, 0016, 0019, 0020).

    python3 -m pytest tests/ -m slow
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_TDECK, FIRMWARE_TDECK_BUILDS, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_TDECK.exists(),
                       reason="T-Deck vendor firmware not present"),
]


@pytest.fixture(scope="module", params=FIRMWARE_TDECK_BUILDS or [FIRMWARE_TDECK],
                ids=lambda p: p.stem.replace("T-Deck_UnitTest_", ""))
def booted(request, tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("tdeck"),
                        firmware=request.param, board="tdeck", timeout=120)


def test_the_fpu_is_enabled_before_the_scheduler_starts(booted: Run) -> None:
    """The regression this board exists to guard (ADR 0013).

    Without esp-coproc-enable this firmware dies about a second in, inside a
    C++ global constructor, on a float that no task yet owns the FPU state for.
    The panic prints before anything else the firmware would say, so its absence
    is the assertion; the two below only show it kept going afterwards.
    """
    assert "Coprocessor exception" not in booted.uart
    assert "Guru Meditation" not in booted.uart


def test_it_reaches_the_arduino_layer(booted: Run) -> None:
    """The constructor that used to kill it runs before any of this, so getting
    here at all means startup completed."""
    assert "[E][" in booted.uart or "[I][" in booted.uart, booted.uart[-400:]


def test_it_gets_far_enough_to_mount_storage(booted: Run) -> None:
    """Reaching storage at all means the app's own setup() is running.

    What that looks like differs by build and has changed. There is a card in
    the slot now (ADR 0033), so a build that mounts it says how big it is,
    where it used to fail; a build whose internal filesystem is empty still
    says so. Either is evidence of the same thing, which is why this asks for
    storage to have been reached rather than for it to have failed.
    """
    reached = ("SD Card Size" in booted.uart or "SPIFFS" in booted.uart
               or "sd_diskio" in booted.uart)
    assert reached, booted.uart[-400:]


# Builds whose setup() starts the WiFi station, and so meet the four radio
# waits of ADR 0019 during boot. 251113 initialises WiFi (WIFI_READY) but does
# not start the station until asked from its interface, so at boot it never
# reaches the gates -- the claim below is not false for it, it is not made.
STARTS_STATION_AT_BOOT = {"250620"}


def test_it_gets_through_the_radio_bring_up(booted: Run, request) -> None:
    """esp_wifi_start() returns only once four hardware waits are answered
    (ADR 0019), and Arduino's own event handler announces it with STA_START:
    "WiFi client started"."""
    build = request.node.callspec.id
    if build not in STARTS_STATION_AT_BOOT:
        pytest.skip(f"build {build} does not start the station during setup")
    after = booted.uart.split("WiFi interface ready", 1)[-1][:600]
    assert "WiFi client started" in booted.uart, after


def test_it_reads_its_battery_and_keeps_drawing(booted: Run) -> None:
    """The ADC conversion completing is what lets LVGL keep its turn (ADR
    0020). A stalled interface stops at a couple of dozen frames; a live one
    draws about ten a second for as long as it runs."""
    assert "ADC_CALI" in booted.uart
    assert len(booted.frames) > 100, len(booted.frames)
