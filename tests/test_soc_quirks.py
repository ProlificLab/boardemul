#!/usr/bin/env python3
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


def test_only_the_tdeck_enables_the_fpu_at_reset() -> None:
    """ADR 0013 turned this on for one board and argued the case there. The Max
    never runs a float before the scheduler, so if it ever acquires this line
    someone has applied a divergence that was never justified for it."""
    assert board_tdeck.SOC_DEVICES == ["esp-coproc-enable,mask=0x1"]
    assert board.SOC_DEVICES == []


def test_the_claim_is_the_narrowest_one_the_evidence_supports() -> None:
    """Only CP0, the FPU, was ever shown to matter. A wider mask would be
    claiming something about silicon that ADR 0013 explicitly could not."""
    (spec,) = board_tdeck.SOC_DEVICES
    kind, _, mask = spec.partition(",mask=")
    assert kind == "esp-coproc-enable"
    assert int(mask, 0) == 0x1
