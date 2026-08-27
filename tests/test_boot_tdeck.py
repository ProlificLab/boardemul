#!/usr/bin/env python3
"""End-to-end tests for the T-Deck, which has no panel model yet.

So these assert on the UART rather than on pixels. That is a weaker claim than
the T-Deck Max's golden frames, and deliberately so: the point of this board at
this stage is that the vendor's own firmware gets far enough to be worth
modelling for, not that it draws anything.

    python3 -m pytest tests/ -m slow
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_TDECK, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_TDECK.exists(),
                       reason="T-Deck vendor firmware not present"),
]


@pytest.fixture(scope="module")
def booted(tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("tdeck"),
                        firmware=FIRMWARE_TDECK, board="tdeck", timeout=60)


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
    """It fails, because the image carries no filesystem data and we model no
    card -- but reaching the attempt means the app's own setup() is running."""
    assert "SPIFFS" in booted.uart or "sd_diskio" in booted.uart, booted.uart[-400:]
