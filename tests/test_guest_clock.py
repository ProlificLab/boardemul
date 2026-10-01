#!/usr/bin/env python3
"""The guest's clock, as the board's parts read it (ADR 0104)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import board, board_tdeck  # noqa: E402
from emu.guest_clock import FRAME, GuestClock  # noqa: E402


def test_the_clock_keeps_the_newest_whole_tick() -> None:
    clock = GuestClock("unused.sock")
    assert clock.seconds() is None
    rest = clock.feed(FRAME.pack(1_000_000_000) + FRAME.pack(2_500_000_000) + b"\x01\x02")
    assert clock.seconds() == 2.5
    assert rest == b"\x01\x02", "a partial frame waits for the rest of it"


def test_both_boards_carry_a_clock(tmp_path) -> None:
    for module in (board, board_tdeck):
        args = module.build(tmp_path / module.__name__).qemu_args()
        assert any(a.startswith("esp-guest-clock,socket=") for a in args), module.__name__
