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

from emu.board_tdeck import EXTRA_REGIONS, UNMODELLED, PhyGates  # noqa: E402

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


# Pages the fork models at the pinned tag (hw/xtensa/esp32s3.c), plus the one
# the catch-all serves specially. A claim overlapping any of these replaces a
# real device with a store-and-return stub, which has broken the boot (RNG),
# misattributed a fix (GDMA) and swallowed IO_MUX writes (SENS sized 0x1000).
FORK_MODELLED = {
    "uart0": (0x60000000, 0x1000), "spi1": (0x60002000, 0x1000),
    "gpio": (0x60004000, 0x1000), "efuse": (0x60007000, 0x1000),
    "rtc_cntl": (0x60008000, 0x200), "io_mux": (0x60009000, 0x2000),
    "uart1": (0x60010000, 0x1000), "rmt": (0x60016000, 0x1000),
    "timg0": (0x6001F000, 0x1000), "timg1": (0x60020000, 0x1000),
    "systimer": (0x60023000, 0x1000), "apb_ctrl": (0x60026000, 0x1000),
    "sdmmc": (0x60028000, 0x1000), "twai": (0x6002B000, 0x1000),
    "uart2": (0x6002E000, 0x1000), "rng_wdev": (0x60035000, 0x1000),
    "usb_serial_jtag": (0x60038000, 0x1000), "aes": (0x6003A000, 0x1000),
    "sha": (0x6003B000, 0x1000), "rsa": (0x6003C000, 0x1000),
    "ds": (0x6003D000, 0x1000), "hmac": (0x6003E000, 0x1000),
    "gdma": (0x6003F000, 0x1000), "system": (0x600C0000, 0x1000),
    "sensitive": (0x600C1000, 0x1000), "interrupt": (0x600C2000, 0x1000),
    "extmem": (0x600C4000, 0x1000),
}


def claimed() -> dict[str, tuple[int, int]]:
    out = {n: (b, 0x1000) for n, b in UNMODELLED.items()}
    out.update(EXTRA_REGIONS)
    return out


def test_no_claim_overlaps_a_page_the_fork_models() -> None:
    for name, (base, size) in claimed().items():
        for fork, (fb, fs) in FORK_MODELLED.items():
            assert base + size <= fb or fb + fs <= base, (
                f"{name} 0x{base:08x}+0x{size:x} overlaps the fork's {fork}")


def test_sens_is_exactly_the_sens_page() -> None:
    """0x400, because IO_MUX starts 0x800 later and the fork models it."""
    assert EXTRA_REGIONS["sens"] == (0x60008800, 0x400)


def test_pages_are_gated_by_content_not_by_name() -> None:
    """Every page with a gate in it goes through PhyGates, no page without one
    does, so adding a gate can never be forgotten in the wiring."""
    for name, base in UNMODELLED.items():
        has_gate = any(base <= a < base + 0x1000 for a in PhyGates.GATES)
        assert PhyGates.covers(base) == has_gate, name
