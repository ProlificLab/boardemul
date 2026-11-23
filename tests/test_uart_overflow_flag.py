#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A UART whose FIFO is full loses a byte, on every run (ADRs 0122, 0138).

Espressif took the fix as the UART's `rx-overflow` property, off by default,
so a run that forgot to ask for it would queue a serial line without bound
again, which is the defect ADR 0122 recorded.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import run  # noqa: E402


def test_every_run_turns_the_uart_overflow_on() -> None:
    argv = run.build_argv(Path("q"), Path("f"), Path("e"), Path("u"), Path("l"),
                          traced=False, psram_mb=8)
    at = argv.index("driver=esp_soc.uart,property=rx-overflow,value=on")
    assert argv[at - 1] == "-global"
