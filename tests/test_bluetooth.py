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

from emu.board_tdeck import (  # noqa: E402
    BLE_CORE_BASE, EXTRA_REGIONS, IRQ_RWBLE, UNMODELLED, build,
)


def test_the_exchange_memory_reaches_up_to_internal_sram() -> None:
    """The ROM copies into it from 0x3FC00100; the machine maps SRAM from
    0x3FC80000, so the window is everything below that."""
    base, size = EXTRA_REGIONS["bt_em"]
    assert base == 0x3FC00000 and base + size == 0x3FC80000


def test_the_core_page_is_a_device_and_not_a_window(tmp_path) -> None:
    """The core's clock and timers run in C on QEMU's clock (ADR 0025); the
    bridge must not also claim the page, or two answers race."""
    assert BLE_CORE_BASE == 0x60031000 and IRQ_RWBLE == 8
    assert all(b != BLE_CORE_BASE for b, _ in EXTRA_REGIONS.values())
    assert all(b != BLE_CORE_BASE for b in UNMODELLED.values())
    board = build(tmp_path)
    assert any(d.startswith("esp-ble-core,base=0x60031000,irq=8") for d in board.devices)
