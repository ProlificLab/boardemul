#!/usr/bin/env python3
"""The Bluetooth windows on the T-Deck: what the controller needed, and no more.

Each is one wall the ROM link layer or the controller blob stopped at, read
off the code that stopped there (ADR 0023, open items, and the record after
it). Nothing here is Bluetooth; it is the least that lets the controller
proceed past its own checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.board_tdeck import EXTRA_CLASSES, EXTRA_REGIONS, BleCore  # noqa: E402


def test_the_exchange_memory_reaches_up_to_internal_sram() -> None:
    """The ROM copies into it from 0x3FC00100; the machine maps SRAM from
    0x3FC80000, so the window is everything below that."""
    base, size = EXTRA_REGIONS["bt_em"]
    assert base == 0x3FC00000 and base + size == 0x3FC80000


def test_the_core_page_is_served_by_the_core_class() -> None:
    assert EXTRA_REGIONS["ble_core"] == (0x60031000, 0x1000)
    assert EXTRA_CLASSES["ble_core"] == "BleCore"


def test_the_reset_bit_is_consumed_by_the_write() -> None:
    """ROM 0x4002bd73 spins while the register reads negative after writing
    bit 31; everything else it wrote must still be there."""
    core = BleCore("ble_core", 0x60031000)
    core.write(0x1C, 0x80000123, 4)
    assert core.read(0x1C, 4) == 0x00000123
    core.write(0x1C, 0x40000000, 4)          # not a bit read off any loop
    assert core.read(0x1C, 4) == 0x40000000
    core.write(0x00, 0x8010070f, 4)          # blob 0x42145bac: extui 31,1; bnez
    assert core.read(0x00, 4) == 0x0010070f


def test_the_core_reports_the_version_the_link_layer_was_built_for() -> None:
    """lld.c:318 prints what it wanted next to what it got: 0x09001b00."""
    core = BleCore("ble_core", 0x60031000)
    core.write(0x04, 0, 4)
    assert core.read(0x04, 4) == 0x09001B00


def test_the_exchange_memory_base_registers_read_back() -> None:
    """emi.c:164 compares each base register's top fourteen bits, times four,
    with the page offset it expects; a store-and-return register passes."""
    core = BleCore("ble_core", 0x60031000)
    core.write(0x210, 0x1000 << 16, 4)
    assert core.read(0x210, 4) == 0x1000 << 16
