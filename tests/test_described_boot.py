#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The described T-Deck boots the vendor firmware as the hand-written one does
(ADR 0184): through the FPU wall, to its card, its battery read and an
interface that keeps drawing, with the touch controller, the keyboard and the
microphone codec answering where the description puts them.

    python3 -m pytest tests/test_described_boot.py -m slow
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_TDECK, FIRMWARE_TDECK_BUILDS, Run, last_boot, qemu_available,
    run_emulator, unforgiven,
)

TDECK = Path(__file__).resolve().parent.parent / "emu" / "boards" / "tdeck.toml"
FIRMWARE = (FIRMWARE_TDECK_BUILDS or [FIRMWARE_TDECK])[0]

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE.exists(), reason="T-Deck vendor firmware not present"),
]


def _shown(uart: str, frames: list) -> bool:
    last = last_boot(uart)
    return "ADC_CALI" in last and len(frames) > 100


@pytest.fixture(scope="module")
def booted(tmp_path_factory) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("described"), firmware=FIRMWARE,
                        board=str(TDECK), timeout=300, extra=("--reboot",),
                        until=_shown)


def _manifest(run: Run) -> dict:
    found = sorted(run.outdir.glob("*.manifest.json"))
    assert found, "the run wrote no manifest"
    return json.loads(found[-1].read_text())


def test_it_boots_to_its_interface(booted: Run) -> None:
    last = last_boot(booted.uart)
    assert not unforgiven(booted.uart), unforgiven(booted.uart)
    assert "SD Card Size" in last or "sd_diskio" in last or "SPIFFS" in last, last[-400:]
    assert "ADC_CALI" in last, last[-400:]
    assert len(booted.frames) > 100, f"{len(booted.frames)} frame(s)"


def test_its_parts_answer_where_the_description_puts_them(booted: Run) -> None:
    """The touch controller, the keyboard and the codec are on i2c0 at their
    catalogue addresses: the firmware asked for each and none is unwired."""
    not_wired = _manifest(booted)["not_wired"]
    for address in ("0x5d", "0x55", "0x40"):
        assert f"i2c0 {address}" not in not_wired, not_wired


# -- the T-Deck Max, described ----------------------------------------------

from tests.support import FIRMWARE as FIRMWARE_MAX, region_digest  # noqa: E402

TDECK_MAX = TDECK.with_name("tdeck-max.toml")
GOLDEN = Path(__file__).resolve().parent / "golden"
SETTING_ICON = (120, 75)    # as tests/test_boot.py taps it


@pytest.fixture(scope="module")
def max_tapped(tmp_path_factory) -> Run:
    if not FIRMWARE_MAX.exists():
        pytest.skip("T-Deck Max vendor firmware not present")
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("described-max"),
                        firmware=FIRMWARE_MAX, board=str(TDECK_MAX),
                        tap=SETTING_ICON, wait_frames=3)


def test_the_described_max_draws_its_launcher_and_opens_settings(max_tapped: Run) -> None:
    """The hand-written Max's golden frames, on the described one: the e-paper
    draws the launcher, and a tap through the CST328 on its described line
    opens settings (tests/test_boot.py)."""
    assert "ictype:[328]" in max_tapped.uart
    assert "Guru Meditation" not in max_tapped.uart
    digests = max_tapped.digests()
    assert region_digest(GOLDEN / "launcher.png") in digests, digests
    assert region_digest(GOLDEN / "settings.png") in digests, digests
