#!/usr/bin/env python3
"""What a machine reset does to the models outside it (ADR 0035)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.board import TraceRegion  # noqa: E402
from emu.bus import OP_READ, OP_RESET, OP_WRITE, Region  # noqa: E402
from emu.devices.sdcard import SdCard  # noqa: E402
from emu.peripherals.adc import SarAdc  # noqa: E402
from emu import sdimage  # noqa: E402


def test_the_operation_is_distinct_from_a_read_or_a_write() -> None:
    assert len({OP_READ, OP_WRITE, OP_RESET}) == 3


def test_a_window_that_holds_nothing_needs_no_code() -> None:
    """The base call exists and does nothing, so a region with no state is
    already correct."""
    region = Region("plain", 0x1000, 0x100)
    region.reset()


def test_stored_registers_are_cleared() -> None:
    """A guest that reboots must not find its own leftovers."""
    region = TraceRegion("window", 0x60000000)
    region.write(0x10, 0xDEADBEEF, 4)
    assert region.read(0x10, 4) == 0xDEADBEEF
    region.reset()
    assert region.read(0x10, 4) == 0


def test_the_converter_forgets_the_run_but_not_the_board() -> None:
    """Its registers and counters are state from this run; the sample it
    answers with is a property of the board and survives."""
    adc = SarAdc("sens", 0x60008800)
    adc.write(0x10, 0x1234, 4)
    adc.conversions = 7
    sample = adc.raw

    adc.reset()
    assert adc.read(0x10, 4) == 0
    assert adc.conversions == 0
    assert adc.raw == sample


def test_a_part_on_the_board_is_not_a_region() -> None:
    """Which is how it escapes being reset. A card does not forget what is on
    it because the processor beside it restarted."""
    card = SdCard(sdimage.build(size_mb=8))
    assert not isinstance(card, Region)
    assert not hasattr(card, "reset")
