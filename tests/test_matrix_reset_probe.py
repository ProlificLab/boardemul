#!/usr/bin/env python3
"""An interrupt held across a digital reset is not lost (ADR 0142).

The probe raises a GPIO edge interrupt with the CPU not taking it, resets the
digital system through SW_SYS_RESET, and after the reset takes a new edge on
the same pin. The reset clears the interrupt matrix's record of every source
(ADR 0138); a GPIO block that forwarded only changes still believed it had
told the matrix, and the new edge never reached the CPU.

    python tools/matrix_reset_probe/build.py
    python3 -m pytest tests/test_matrix_reset_probe.py -m slow
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MATRIX_RESET_PROBE, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MATRIX_RESET_PROBE.exists(),
                       reason="probe not built; run tools/matrix_reset_probe/build.py"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _done(uart: str, frames: list) -> bool:
    return "probe: done" in uart


@pytest.fixture(scope="module")
def probed(tmp_path_factory) -> Run:
    return run_emulator(tmp_path_factory.mktemp("matrix-reset"),
                        firmware=FIRMWARE_MATRIX_RESET_PROBE, board="tdeck",
                        timeout=120, extra=("--reboot",), until=_done)


def test_the_reset_was_a_digital_system_reset(probed: Run) -> None:
    """Not a CPU reset, which keeps the matrix's levels (rst:0xc)."""
    assert "probe: edge raised with the CPU masked" in probed.uart, probed.uart[-1500:]
    assert "rst:0x3 (RTC_SW_SYS_RST)" in probed.uart, probed.uart[-1500:]


def test_an_edge_after_the_reset_reaches_the_cpu(probed: Run) -> None:
    """Taken without going through the driver, on the configuration the GPIO
    block kept from before the reset. Before ADR 0142 it was not: "no
    (before 0, after 0)"."""
    assert "probe: an edge after the reset was taken: yes" in probed.uart, (
        probed.uart[-1500:])


def test_the_driver_clears_a_pins_status_as_it_enables_it(probed: Run) -> None:
    """Why ESP-IDF firmware does not meet the lost interrupt: gpio_intr_enable
    clears the pin's status first, which lowers the source and puts the
    device and the matrix back in step (ADR 0142)."""
    assert "probe: latched again with the CPU masked, status 0x00000010" in probed.uart
    assert "probe: after gpio_intr_enable, status 0x00000000" in probed.uart
