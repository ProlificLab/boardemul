#!/usr/bin/env python3
"""Whether a guest reset ends the run or restarts the chip (ADR 0099).

A board that panics resets itself and boots again. A run ends at that reset
unless it asks for the reboot, because single-board tests use the reset to see
a restart happen. Both must stay true, so both are checked.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import run  # noqa: E402


def _argv(**kwargs) -> list[str]:
    return run.build_argv(Path("q"), Path("f"), Path("e"), Path("u"), Path("l"),
                          traced=False, psram_mb=8, **kwargs)


def test_a_reset_ends_the_run_by_default() -> None:
    assert "-no-reboot" in _argv()


def test_a_run_that_asks_lets_the_chip_reboot() -> None:
    argv = _argv(reboot=True)
    assert "-no-reboot" not in argv
    # Nothing else moves: the machine and its drives are as they were.
    assert [a for a in _argv() if a != "-no-reboot"] == argv
