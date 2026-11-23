#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Each board's hardware RNG seed (ADR 0098).

The seed has two jobs that pull in opposite directions, and both are checked:
the same board must get the same seed every run (ADR 0004 -- a run is
determined by its inputs), and two boards must not get the same one, or they
draw identical "random" numbers -- which is how two mesh nodes came to transmit
as one node.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import run  # noqa: E402


def test_the_same_board_always_gets_the_same_seed() -> None:
    assert run.rng_seed("24:6F:28:00:00:11") == run.rng_seed("24:6F:28:00:00:11")


def test_two_boards_get_two_seeds() -> None:
    """The two MACs test_mesh gives its nodes, which shared one seed before."""
    assert run.rng_seed("24:6F:28:00:00:11") != run.rng_seed("24:6F:28:00:00:22")


def test_the_seed_is_the_base_and_the_mac() -> None:
    seed = run.rng_seed("02:54:44:4D:00:01")
    assert seed == (run.RNG_SEED << 48) | 0x0254444D0001
    assert seed < 1 << 64, "QEMU's -seed takes an unsigned 64-bit number"


def test_the_seed_reaches_qemu() -> None:
    argv = run.build_argv(Path("q"), Path("f"), Path("e"), Path("u"), Path("l"),
                          traced=False, psram_mb=8, seed=12345)
    assert argv[argv.index("-seed") + 1] == "12345"
