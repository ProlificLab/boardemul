#!/usr/bin/env python3
"""Meshtastic on the T-Deck: the flash reads back what is in it.

Every SPI1 read used to arrive two bytes late (ADR 0022), which no firmware
survives once it touches storage: LittleFS called a correct image corrupt,
NVS re-initialised itself on every boot, and the coredump check read a size
of -65536 from erased flash. Each test here is one of those no longer
happening. The last one is the goal itself: a first boot that reaches the
device-ui's setup dialog without rebooting into programming mode (ADR 0026),
on the instruction-counted clock the link layer needs (ADR 0027).

    python3 -m pytest tests/ -m slow
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (FIRMWARE_MESHTASTIC.exists() and MESHTASTIC_LITTLEFS.exists()),
                       reason="Meshtastic firmware or its filesystem image not present"),
]


def _boot(tmp_path_factory, overlays: tuple[str, ...]) -> Run:
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    return run_emulator(tmp_path_factory.mktemp("mesh"), firmware=FIRMWARE_MESHTASTIC,
                        board="tdeck", timeout=240, overlays=overlays, icount=True,
                        until=lambda uart, frames: ("/littlefs/prefs/" in uart
                                                    and len(frames) >= 5),
                        extra=("--psram-octal",))   # the T-Deck has 8 MB of octal PSRAM


@pytest.fixture(scope="module")
def with_vendor_image(tmp_path_factory) -> Run:
    return _boot(tmp_path_factory, (f"0xc90000={MESHTASTIC_LITTLEFS}",))


@pytest.fixture(scope="module")
def blank_partition(tmp_path_factory) -> Run:
    return _boot(tmp_path_factory, ())


def test_the_vendor_filesystem_image_mounts(with_vendor_image: Run) -> None:
    """A valid LittleFS read back shifted is 'corrupted'; read back right, it
    mounts, and the firmware goes looking for its preference files in it."""
    uart = with_vendor_image.uart
    assert "Corrupted dir pair" not in uart
    assert "File system is not mounted" not in uart
    assert "/littlefs/prefs/" in uart, uart[-600:]


def test_a_blank_partition_formats_on_first_boot(blank_partition: Run) -> None:
    """The mount of erased flash fails, as it should; what must not fail is the
    format that follows, which writes a superblock and reads it back."""
    uart = blank_partition.uart
    assert "Failed to format" not in uart
    assert "File system is not mounted" not in uart
    assert "/littlefs/prefs/" in uart, uart[-600:]


def test_erased_flash_reads_as_erased(with_vendor_image: Run) -> None:
    """The coredump partition is blank; a size of -65536 was 00 00 FF FF, the
    two-byte shift in one number."""
    assert "Incorrect size of core dump image" not in with_vendor_image.uart


def test_the_first_boot_reaches_the_interface_without_rebooting(with_vendor_image: Run) -> None:
    """One reset banner, the firmware's own; and frames drawn well after the
    three boot-screen ones, which is the setup dialog (region and names)."""
    uart = with_vendor_image.uart
    assert uart.count("rst:") == 1, uart[-400:]
    assert len(with_vendor_image.frames) >= 5, with_vendor_image.frames

