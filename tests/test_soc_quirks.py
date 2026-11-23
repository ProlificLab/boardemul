#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""What each board claims about the SoC it is built around.

These are declarations, not models, so a test can only check that they say what
they mean to say. That is worth doing anyway: a deliberate divergence from the
hardware is only defensible while it stays where it was argued for, and the
failure mode this guards against is it quietly spreading to boards that never
needed it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import board, board_tdeck  # noqa: E402

BOARDS = {"tdeck-max": board, "tdeck": board_tdeck}


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_every_board_declares_its_soc_quirks(name: str) -> None:
    """Absent is not the same as empty. run.py reads this on every board, so a
    board that simply forgot the attribute would fail at launch."""
    assert isinstance(BOARDS[name].SOC_DEVICES, list)


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_no_board_diverges_from_the_soc(name: str) -> None:
    """The T-Deck enabled the FPU at reset with a device of ours (ADR 0013).
    QEMU now resets CPENABLE to 0xff, the value upstream measured on silicon,
    on every board and every reset path, and the device is gone (ADR 0143).
    A board that lists one again is diverging from silicon, and needs an ADR
    of its own that says why."""
    assert BOARDS[name].SOC_DEVICES == []
