#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
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
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    BOOT_BANNER, FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, last_boot,
    qemu_available, reset_trouble, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (FIRMWARE_MESHTASTIC.exists() and MESHTASTIC_LITTLEFS.exists()),
                       reason="Meshtastic firmware or its filesystem image not present"),
]


class _ReachedTheInterface:
    """Waits for the setup dialog in the boot in progress: the firmware looking
    for its preference files, and five frames drawn since that boot began --
    three of boot screen, then the dialog.

    The panel is on the board, so its frames run on across a reboot (ADR
    0114). Which frames belong to the boot in progress is noted here, as the
    run goes: the count when its banner was first seen.
    """

    def __init__(self) -> None:
        self.boots = 0
        self.frames_before = 0

    def __call__(self, uart: str, frames: list) -> bool:
        boots = uart.count(BOOT_BANNER)
        if boots != self.boots:
            self.boots, self.frames_before = boots, len(frames)
        return ("/littlefs/prefs/" in last_boot(uart)
                and len(frames) - self.frames_before >= 5)


def _boot(tmp_path_factory, overlays: tuple[str, ...]) -> Run:
    """The board may reboot from the firmware's ipc1 overflow, as a T-Deck in a
    hand would, and is judged on the boot that came back (ADR 0125)."""
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    reached = _ReachedTheInterface()
    run = run_emulator(tmp_path_factory.mktemp("mesh"), firmware=FIRMWARE_MESHTASTIC,
                       board="tdeck", timeout=240, overlays=overlays,
                       until=reached,
                       # the T-Deck has 8 MB of octal PSRAM
                       extra=("--psram-octal", "--reboot"))
    if run.boots > 1:
        warnings.warn(f"Meshtastic booted {run.boots} times (ADR 0125)")
    # Kept on the run for the test below: the last boot's own frames.
    run.frames_this_boot = run.frames[reached.frames_before:]
    return run


@pytest.fixture(scope="module")
def with_vendor_image(tmp_path_factory) -> Run:
    return _boot(tmp_path_factory, (f"0xc90000={MESHTASTIC_LITTLEFS}",))


@pytest.fixture(scope="module")
def blank_partition(tmp_path_factory) -> Run:
    return _boot(tmp_path_factory, ())


def test_the_vendor_filesystem_image_mounts(with_vendor_image: Run) -> None:
    """A valid LittleFS read back shifted is 'corrupted'; read back right, it
    mounts, and the firmware goes looking for its preference files in it."""
    uart = with_vendor_image.last_uart
    assert "Corrupted dir pair" not in uart
    assert "File system is not mounted" not in uart
    assert "/littlefs/prefs/" in uart, uart[-600:]


def test_a_blank_partition_formats_on_first_boot(blank_partition: Run) -> None:
    """The mount of erased flash fails, as it should; what must not fail is the
    format that follows, which writes a superblock and reads it back."""
    uart = blank_partition.last_uart
    assert "Failed to format" not in uart
    assert "File system is not mounted" not in uart
    assert "/littlefs/prefs/" in uart, uart[-600:]


def test_erased_flash_reads_as_erased(with_vendor_image: Run) -> None:
    """The coredump partition is blank; a size of -65536 was 00 00 FF FF, the
    two-byte shift in one number."""
    assert "Incorrect size of core dump image" not in with_vendor_image.last_uart


def test_the_first_boot_reaches_the_interface_without_rebooting(with_vendor_image: Run) -> None:
    """No reboot into programming mode (ADR 0026), nor for anything else but
    ADR 0099's ipc1 canary (ADR 0125); and frames drawn well after the boot
    screen's, which is the setup dialog (region and names)."""
    run = with_vendor_image
    assert not reset_trouble(run.uart), reset_trouble(run.uart)
    assert len(run.frames_this_boot) >= 5, run.frames


def test_a_blank_first_boot_reboots_only_from_the_forgiven_panic(blank_partition: Run) -> None:
    """The blank partition's boot, held to the same rule (ADR 0125)."""
    assert not reset_trouble(blank_partition.uart), reset_trouble(blank_partition.uart)

