#!/usr/bin/env python3
"""The radio bring-up gates, pinned to what was read off the silicon's code.

Each gate answers one polling loop in the vendor firmware's esp_wifi_start()
path. The register and the bits are not tunable: every one was taken from a
disassembled loop and the literal it loads, so a change here is either a
transcription error or a new finding, and either way the ADR has to move too.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.board_tdeck import UNMODELLED, PhyGates  # noqa: E402

# (address, bits) -> (where it was read, the instruction that waits on it)
DOCUMENTED = {
    0x6000E050: (0x07000000, "ROM 0x40036a49: bnei extui(a8,24,3), 7"),
    0x6000E04C: (0x01000000, "app 0x420da611: bnone a12, 0x01000000"),
    0x60006174: (0x00010000, "ROM 0x40036606: bnone a3, 0x00010000"),
    0x60033D14: (0x00000001, "app 0x420d4832: bbci a3, 0"),
}


def test_the_gate_table_is_exactly_the_documented_one() -> None:
    assert {a: b for a, (b, _) in DOCUMENTED.items()} == PhyGates.GATES


def test_a_gated_page_answers_only_its_gate(tmp_path: Path, monkeypatch) -> None:
    """Forcing bits anywhere else on the page corrupted values early boot reads
    back and hung the guest at reset; one address at a time is the rule."""
    monkeypatch.setenv("BOARDEMUL_PHY_GATES", "1")
    page = PhyGates("fe", 0x60006000)
    page.write(0x174, 0x0000ABCD, 4)
    page.write(0x170, 0x0000ABCD, 4)
    assert page.read(0x174, 4) == 0x0000ABCD | 0x00010000
    assert page.read(0x170, 4) == 0x0000ABCD


def test_gates_are_on_by_default_and_zero_turns_them_off(monkeypatch) -> None:
    monkeypatch.delenv("BOARDEMUL_PHY_GATES", raising=False)
    assert PhyGates("m", 0x60033000).read(0xD14, 4) & 1 == 1
    monkeypatch.setenv("BOARDEMUL_PHY_GATES", "0")
    assert PhyGates("m", 0x60033000).read(0xD14, 4) == 0


def test_the_rng_page_is_never_claimed() -> None:
    """The fork's catch-all serves WDEV_RND_REG at 0x6003507C. A trace window
    over that page starved the bootloader of entropy and stopped the boot at
    'entry' (ADR 0008, amended). This is the regression test for that lesson."""
    for base in UNMODELLED.values():
        assert not (base <= 0x6003507C < base + 0x1000), hex(base)


def test_pages_are_gated_by_content_not_by_name() -> None:
    """Every page with a gate in it goes through PhyGates, no page without one
    does, so adding a gate can never be forgotten in the wiring."""
    for name, base in UNMODELLED.items():
        has_gate = any(base <= a < base + 0x1000 for a in PhyGates.GATES)
        assert PhyGates.covers(base) == has_gate, name
