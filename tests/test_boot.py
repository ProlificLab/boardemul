#!/usr/bin/env python3
"""End-to-end tests for the T-Deck Max: real firmware, real QEMU, real pixels.

These are minutes rather than milliseconds, so they are marked slow and skipped
unless asked for:

    python3 -m pytest tests/ -m slow

They exist because everything below them can pass while the thing as a whole
does nothing. The golden frames are the only assertion in this project that the
emulator draws the right picture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE, Run, qemu_available, region_digest, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present"),
]

GOLDEN = Path(__file__).resolve().parent / "golden"
# The middle icon of the launcher's top row, on a 240x320 panel.
SETTING_ICON = (120, 75)
LAUNCHER_FRAMES = 3         # clear, splash, then the menu


@pytest.fixture(scope="module")
def booted(tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("boot"),
                        wait_frames=LAUNCHER_FRAMES)


@pytest.fixture(scope="module")
def tapped(tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("tap"), tap=SETTING_ICON,
                        wait_frames=LAUNCHER_FRAMES)


# -- boot -------------------------------------------------------------------


def test_rom_loads_the_bootloader_segments(booted: Run) -> None:
    """These lines are computable from the image header before running
    anything, so a mismatch means the flash image is wrong -- not the hardware.
    """
    for expected in ("load:0x3fce3808,len:0x44c",
                     "load:0x403c9700,len:0xbd8",
                     "load:0x403cc700,len:0x2a80",
                     "entry 0x403c98d0"):
        assert expected in booted.uart, expected


def test_psram_is_detected(booted: Run) -> None:
    """It only enumerates in quad line mode under this fork (ADR 0007), and
    without it LVGL allocates nothing and there is no picture at all."""
    assert "PSRAM enabled" in booted.uart
    assert "PSRAM ID read error" not in booted.uart


def test_the_firmware_does_not_panic(booted: Run) -> None:
    assert "Guru Meditation" not in booted.uart
    assert "Rebooting" not in booted.uart


def test_touch_controller_identifies_itself(booted: Run) -> None:
    assert "ictype:[328]" in booted.uart


def test_launcher_is_drawn(booted: Run) -> None:
    golden = region_digest(GOLDEN / "launcher.png")
    assert golden in booted.digests(), (
        "no frame matched the golden launcher; "
        f"got {booted.digests()}")


# -- interaction ------------------------------------------------------------


def test_a_tap_changes_the_screen(tapped: Run) -> None:
    launcher = region_digest(GOLDEN / "launcher.png")
    digests = tapped.digests()
    assert launcher in digests, "never reached the launcher, so the tap proves nothing"
    after = digests[digests.index(launcher) + 1:]
    assert any(d != launcher for d in after), "the screen never changed"


def test_a_tap_opens_settings(tapped: Run) -> None:
    assert region_digest(GOLDEN / "settings.png") in tapped.digests()
