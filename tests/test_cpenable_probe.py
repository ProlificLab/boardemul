#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""CPENABLE is 0xff out of every reset, as on silicon (ADR 0143).

The probe reads CPENABLE in a C++ global constructor on the PRO CPU, before
FreeRTOS starts and before anything writes the register: upstream's method on
silicon, which read 0xff (espressif/qemu#154). It does so after a power-on,
after esp_restart() and after a digital system reset. ADR 0013's device made
it 0x1 after each of them; QEMU's own reset makes it 0xff.

    python tools/cpenable_probe/build.py
    python3 -m pytest tests/test_cpenable_probe.py -m slow
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_CPENABLE_PROBE, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_CPENABLE_PROBE.exists(),
                       reason="probe not built; run tools/cpenable_probe/build.py"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

READING = re.compile(r"probe: boot (\d+), reset reason (\d+), "
                     r"CPENABLE at the constructors (0x[0-9a-f]{8}) on core (\d+)")


def _done(uart: str, frames: list) -> bool:
    return "probe: done" in uart


@pytest.fixture(scope="module")
def probed(tmp_path_factory) -> Run:
    return run_emulator(tmp_path_factory.mktemp("cpenable"),
                        firmware=FIRMWARE_CPENABLE_PROBE, board="tdeck",
                        timeout=120, extra=("--reboot",), until=_done)


def test_it_booted_once_from_each_kind_of_reset(probed: Run) -> None:
    """Power-on, then esp_restart()'s CPU reset, then SW_SYS_RESET."""
    resets = re.findall(r"rst:0x[0-9a-f]+ \(\w+\)", probed.uart)
    assert resets == ["rst:0x1 (POWERON)", "rst:0xc (RTC_SW_CPU_RST)",
                      "rst:0x3 (RTC_SW_SYS_RST)"], probed.uart[-1500:]


def test_the_fpu_is_on_at_the_constructors_after_every_reset(probed: Run) -> None:
    """0xff on the PRO CPU, where ESP-IDF runs the constructors, on each boot.
    With ADR 0013's device it read 0x00000001 on all three."""
    readings = READING.findall(probed.uart)
    assert [boot for boot, *_ in readings] == ["1", "2", "3"], probed.uart[-1500:]
    for boot, _reason, cpenable, core in readings:
        assert (cpenable, core) == ("0x000000ff", "0"), f"boot {boot}: {cpenable} on core {core}"
