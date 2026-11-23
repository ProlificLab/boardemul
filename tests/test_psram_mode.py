#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The PSRAM line mode is the image's own, unless a person forces one (ADR 0144).

ESP-IDF links one of two ESP32-S3 PSRAM drivers, and each logs under its own
tag. An image enumerates its PSRAM only in the line mode of the driver it
carries, so that driver's tag decides the mode a run presents.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.run import choose_psram_mode, image_psram_mode  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware"


@pytest.mark.parametrize("image, mode", [
    (b"..\x00opi psram\x00..", "octal"),      # IDF 4.4's octal driver
    (b"..\x00octal_psram\x00..", "octal"),    # IDF 5's
    (b"..\x00psram\x00..", "quad"),           # IDF 4.4's quad driver
    (b"..\x00quad_psram\x00..", "quad"),      # IDF 5's
    (b"no driver here", None),
    (b"\x00psram\x00 and \x00opi psram\x00", None),
])
def test_the_drivers_tag_names_the_mode(image: bytes, mode: str | None) -> None:
    assert image_psram_mode(image) == mode


def test_the_octal_tag_is_not_read_as_the_quad_one() -> None:
    """"opi psram" ends in "psram"; only a whole "psram" is the quad tag."""
    assert image_psram_mode(b"\x00opi psram\x00") == "octal"


def test_a_forced_mode_wins(tmp_path: Path) -> None:
    image = tmp_path / "flash.bin"
    image.write_bytes(b"\x00opi psram\x00")
    assert choose_psram_mode("quad", image) == (False, "quad, as asked")
    assert choose_psram_mode("octal", None) == (True, "octal, as asked")


def test_otherwise_the_images_driver_decides(tmp_path: Path) -> None:
    image = tmp_path / "flash.bin"
    image.write_bytes(b"\x00opi psram\x00")
    assert choose_psram_mode(None, image) == (True, "octal, the image's driver")


def test_an_image_naming_no_driver_keeps_quad(tmp_path: Path) -> None:
    image = tmp_path / "flash.bin"
    image.write_bytes(b"nothing")
    octal, why = choose_psram_mode(None, image)
    assert not octal and why.startswith("quad, the default")


@pytest.mark.parametrize("name, mode", [
    ("T-Deck-MAX_V3.1_20260313.bin", "quad"),
    ("firmware-t-deck-tft-2.7.10.94d4bdf.bin", "octal"),
    ("T-Deck_UnitTest_250620.bin", None),
])
def test_the_vendor_images_carry_the_driver_they_enumerate_with(name: str,
                                                                mode: str | None) -> None:
    """The re-test of ADR 0144: the Max's image enumerates only in quad,
    Meshtastic only in octal, and the UnitTest builds name no driver."""
    path = FIRMWARE / name
    if not path.is_file():
        pytest.skip(f"{name} not downloaded")
    assert image_psram_mode(path.read_bytes()) == mode
